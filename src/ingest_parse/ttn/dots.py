"""dots.mocr как источник разметки: запрос к OpenAI-совместимому серверу, разбор ответа, повтор на половинах.

dots на сканах иногда с первого токена решает «вся страница — картинка»; повтор того же кадра это не лечит,
а половина страницы (вдвое больше деталей, меньше похожа на фото) обычно размечается нормально.
"""

from __future__ import annotations

import base64
import io
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable

from PIL import Image

from ingest_parse.ttn.layout import Det, area, merge_tiles, role

PROMPT = (
    "Please output the layout information from this PDF image, including each layout element's bbox, "
    "its category, and the corresponding text content within the bbox.\n"
    "1. Bbox format: [x1, y1, x2, y2].\n"
    "2. Layout Categories: ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', "
    "'Picture', 'Section-header', 'Table', 'Text', 'Title'].\n"
    "3. Text Extraction & Formatting Rules:\n"
    "    - Picture: omit the text field.\n"
    "    - Formula: text as LaTeX.\n"
    "    - Table: text as HTML.\n"
    "    - All others: text as Markdown.\n"
    "4. Constraints: original text from the image, no translation. Sort elements in reading order.\n"
    "5. Final Output: one JSON list."
)
LONG_EDGE = 1600  # 2240 вдвое медленнее и схлопывается чаще
_ITEM = re.compile(r'\{\s*"bbox"\s*:\s*\[([\d.,\s]+)\]\s*,\s*"category"\s*:\s*"([^"]+)"')
_COLLAPSED = 0.4  # не табличная рамка больше этой доли кадра — разметки по сути нет
_HALF = 0.55

Send = Callable[[Image.Image, str], str]  # картинка уже нужного размера, промпт → текст ответа


def sent_size(size: tuple[int, int], long_edge: int) -> tuple[int, int]:
    """Стороны кратны 28 — так режет картинку vision-энкодер Qwen2-VL, координаты ответа — в этих пикселях."""
    w, h = size
    scale = min(1.0, long_edge / max(size))
    return max(28, round(w * scale / 28) * 28), max(28, round(h * scale / 28) * 28)


def _plain(answer: str) -> str:
    """JSON иногда приходит после <think> или в ```json."""
    text = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return re.sub(r"^```[\w-]*\s*|\s*```$", "", text.strip()).strip()


def message_text(data: dict) -> str:
    """Текст ответа dots. Пустой content — частый ответ llama.cpp, буква лежит в reasoning_content."""
    message = data["choices"][0]["message"]
    if not isinstance(message, dict):
        raise TypeError(f"dots unexpected message: {str(data)[:200]}")
    text = message.get("content")
    if not str(text or "").strip():
        text = message.get("reasoning_content") or message.get("reasoning")
    text = str(text or "").strip()
    if not text:
        raise RuntimeError(f"dots empty content; message keys={list(message)}")
    return text


def parse_dots(answer: str, size: tuple[int, int], long_edge: int) -> tuple[list[Det], str]:
    """(рамки в пикселях кадра size, статус ok | salvaged | invalid | collapsed)."""
    answer = _plain(answer)
    sw, sh = sent_size(size, long_edge)
    status = "ok"
    try:
        raw = json.loads(answer)
        items = raw if isinstance(raw, list) else raw.get("layout") or raw.get("elements") or next(
            (v for v in raw.values() if isinstance(v, list)), []
        )
        found = [
            (str(it["category"]), [float(v) for v in it["bbox"]], str(it.get("text") or "").strip())
            for it in items
        ]
    except (ValueError, KeyError, TypeError, AttributeError):
        found = [
            (cat, [float(v) for v in nums.split(",") if v.strip()][:4], "")
            for nums, cat in _ITEM.findall(answer)
        ]
        status = "salvaged" if found else "invalid"
    fx, fy = size[0] / sw, size[1] / sh
    dets = [
        Det(role(cat), 1.0, (round(b[0] * fx), round(b[1] * fy), round(b[2] * fx), round(b[3] * fy)), "dots", cat, text)
        for cat, b, text in found
        if len(b) == 4
    ]
    if any(d.role != "table" and area(d.box) > _COLLAPSED * size[0] * size[1] for d in dets):
        status = "collapsed"
    return dets, status


def _ask(image: Image.Image, send: Send, long_edge: int) -> tuple[list[Det], str, str]:
    sent = image.convert("RGB").resize(sent_size(image.size, long_edge), Image.Resampling.LANCZOS)
    answer = send(sent, PROMPT)
    dets, status = parse_dots(answer, image.size, long_edge)
    if status == "invalid":
        status = "invalid: " + " ".join(str(answer).split())[:160]
    return dets, status, answer


def dots_layout(image: Image.Image, send: Send, long_edge: int = LONG_EDGE) -> tuple[list[Det], str]:
    """Разметка dots; схлопнулась — повтор на верхней и нижней половинах (схлопнувшаяся половина выбрасывается)."""
    dets, status, _ = _ask(image, send, long_edge)
    if not status.startswith("collapsed"):
        return dets, status
    w, h = image.size
    tiles = []
    for box in ((0, 0, w, int(_HALF * h)), (0, int((1 - _HALF) * h), w, h)):
        part, part_status, _ = _ask(image.crop(box), send, long_edge)
        if part_status != "collapsed":
            tiles.append((part, box))
    return merge_tiles([], tiles, (w, h), stitch=True), "halves"


def page_text(dets: list[Det]) -> str:
    """Буквы dots сверху вниз: картинки без подписи пропускаются."""
    parts = [
        d.text.strip()
        for d in sorted(dets, key=lambda d: (d.box[1], d.box[0]))
        if d.role != "picture" and d.text.strip()
    ]
    return "\n\n".join(parts)


def http_sender(url: str, model: str, max_tokens: int = 8192, timeout: float = 600) -> Send:
    def send(image: Image.Image, prompt: str) -> str:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        base = url.rstrip("/").removesuffix("/v1/chat/completions").removesuffix("/v1")
        endpoint = base + "/v1/chat/completions"
        req = urllib.request.Request(endpoint, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return message_text(json.load(resp))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace").replace("\n", " ")
            raise RuntimeError(f"HTTP {exc.code} from {endpoint}: {detail}") from exc

    return send
