"""Подсказка текста для промпта Qwen. Без флага слот молчит."""

from __future__ import annotations

import os
import re
import warnings
from collections.abc import Callable

from PIL import Image

_CHANDRA = False
_OLM = False
_CHANDRA_MODEL = "chandra"
_OLM_MODEL = "olmocr"
_OLM_EDGE = 1288  # длинная сторона, на которой учили olmOCR
_CHANDRA_PROMPT = """
OCR this image to HTML, arranged as layout blocks. Each layout block should be a div with the data-bbox attribute representing the bounding box of the block in x0 y0 x1 y1 format. Bboxes are normalized 0-1000. The data-label attribute is the label for the block.

Use the following labels:
- Caption
- Footnote
- Equation-Block
- List-Group
- Page-Header
- Page-Footer
- Image
- Section-Header
- Table
- Text
- Complex-Block
- Code-Block
- Form
- Table-Of-Contents
- Figure
- Chemical-Block
- Diagram
- Bibliography
- Blank-Page

Only use these tags: math, br, i, b, u, del, sup, sub, table, tr, td, p, th, div, pre, h1, h2, h3, h4, h5, ul, ol, li, input, a, span, img, hr, tbody, small, caption, strong, thead, big, code, chem.
Attributes: class, colspan, rowspan, display, checked, type, border, value, style, href, alt, align, data-bbox, data-label.

Keep the text accurate and in reading order. Inline math goes in math tags as KaTeX. Tables use colspan and rowspan. Do not fill img src. Describe images in the alt attribute and in the div.
""".strip()
_OLM_PROMPT = (
    "OCR this document image to markdown. Keep the reading order. "
    "Do not add commentary. Do not invent text that is not visible."
)
Box = tuple[int, int, int, int]
_Ask = Callable[[Image.Image], str]


class PageLetters:
    """Текст страницы и, у Chandra, блоки с рамками. Рамки в разметку страницы не входят."""

    def __init__(
        self,
        text: str = "",
        source: str = "",
        blocks: list[tuple[Box, str]] | None = None,
        image: Image.Image | None = None,
        ask: _Ask | None = None,
    ) -> None:
        self.text = text
        self.source = source
        self.blocks = blocks or []
        self._image = image
        self._ask = ask
        self._cache: dict[Box, str] = {}

    def for_box(self, box: Box | None) -> str:
        """Текст, который лежит в рамке. Вся страница — полный ответ. Чужой блок в вырез не кладётся."""
        if box is None or self._covers(box):
            return self.text
        if self.blocks:
            picked = [text for block, text in self.blocks if _overlaps(block, box)]
            return "\n".join(picked)
        if self._ask is None or self._image is None:
            return ""
        if box in self._cache:
            return self._cache[box]
        crop = self._image.crop(box)
        if self.source == "olm":
            crop = _fit(crop, _OLM_EDGE)
        try:
            text = self._ask(crop)
        except Exception as exc:  # noqa: BLE001 — сбой выреза не роняет страницу
            _off(f"{self.source} crop FAILED {type(exc).__name__}: {exc}")
            text = ""
        self._cache[box] = text
        if text:
            print(f"  {self.source} crop {crop.size[0]}x{crop.size[1]} chars={len(text)}", flush=True)
        return text

    def dump(self) -> str:
        lines = [f"source: {self.source or 'off'}", f"chars: {len(self.text)}", f"blocks: {len(self.blocks)}", ""]
        if self.blocks:
            for box, text in self.blocks:
                lines.append(",".join(str(v) for v in box))
                lines.append(text)
                lines.append("")
        else:
            lines.append(self.text)
        return "\n".join(lines).rstrip() + "\n"

    def _covers(self, box: Box) -> bool:
        if self._image is None:
            return False
        width, height = self._image.size
        area = max(1, width * height)
        common = max(0, min(box[2], width) - max(box[0], 0)) * max(0, min(box[3], height) - max(box[1], 0))
        return common >= 0.9 * area


def set_chandra(enabled: bool) -> None:
    """Включает локальную подсказку Chandra. Без вызова из CLI слот не ходит в сеть."""
    global _CHANDRA
    _CHANDRA = bool(enabled)


def set_olm(enabled: bool) -> None:
    """Включает локальную подсказку olmOCR. Без вызова из CLI слот не ходит в сеть."""
    global _OLM
    _OLM = bool(enabled)


def read_page_letters(image: Image.Image) -> str:
    """Полный текст страницы для хвоста промпта. Без флага всегда пусто."""
    return load_letters(image).text


def load_letters(image: Image.Image) -> PageLetters:
    """Один ответ модели на страницу. Chandra режется по рамкам, olmOCR — по вырезу."""
    if _CHANDRA and _OLM:
        _off("olm ignored because --chandra is set")
    if _CHANDRA:
        return _fetch(image, "chandra", "SCAN_CHANDRA_URL", "SCAN_CHANDRA_MODEL", _CHANDRA_MODEL, _CHANDRA_PROMPT, None)
    if _OLM:
        return _fetch(image, "olm", "SCAN_OLM_URL", "SCAN_OLM_MODEL", _OLM_MODEL, _OLM_PROMPT, _OLM_EDGE)
    return PageLetters(image=image)


def page_hint(pdf_text: str, image: Image.Image) -> str:
    """Текстовый слой PDF и слот букв. Буквы Dots сюда не входят."""
    extra = read_page_letters(image)
    parts = [part.strip() for part in (pdf_text, extra) if part and part.strip()]
    return "\n\n".join(parts)


def _fetch(
    image: Image.Image, source: str, url_key: str, model_key: str, default_model: str, prompt: str,
    long_edge: int | None,
) -> PageLetters:
    url = os.environ.get(url_key, "").strip()
    if not url:
        _off(f"{source} off ({url_key} is empty)")
        return PageLetters(image=image)
    if not url.startswith(("http://", "https://")):
        _off(f"{source} off ({url_key} must start with http): {url}")
        return PageLetters(image=image)
    from ingest_parse.ttn.dots import http_sender

    model = os.environ.get(model_key, "").strip() or default_model
    send = http_sender(url, model)

    def ask(crop: Image.Image) -> str:
        raw = send(crop, prompt)
        return _blocks(raw, crop.size)[0] if source == "chandra" else _without_boxes(raw)

    sent = _fit(image, long_edge) if long_edge else image
    try:
        raw = send(sent, prompt)
    except Exception as exc:  # noqa: BLE001 — сервер недоступен, Qwen идёт без подсказки
        _off(f"{source} FAILED {type(exc).__name__}: {exc}")
        return PageLetters(source=source, image=image, ask=ask)
    if source == "chandra":
        text, blocks = _blocks(raw, image.size)
    else:
        text, blocks = _without_boxes(raw), []
    if not text:
        _off(f"{source} FAILED empty answer")
        return PageLetters(source=source, image=image, ask=ask)
    print(f"  {source} chars={len(text)} blocks={len(blocks)}", flush=True)
    return PageLetters(text, source, blocks, image, ask)


def _off(reason: str) -> str:
    print(f"  {reason}", flush=True)
    warnings.warn(reason, stacklevel=3)
    return ""


def _fit(image: Image.Image, long_edge: int) -> Image.Image:
    width, height = image.size
    scale = min(1.0, long_edge / max(width, height, 1))
    if scale >= 1:
        return image
    return image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)


def _without_boxes(html: str) -> str:
    """Текст без координат. Атрибут data-bbox в подсказку не попадает."""
    text = re.sub(r"""\sdata-bbox\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", "", html, flags=re.IGNORECASE)
    text = re.sub(r"</(div|p|h[1-6]|tr|li|table)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("data-bbox", "")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _blocks(html: str, size: tuple[int, int]) -> tuple[str, list[tuple[Box, str]]]:
    """(текст сверху вниз, рамки в пикселях страницы). Координаты ответа — 0–1000."""
    width, height = size
    blocks: list[tuple[Box, str]] = []
    for match in re.finditer(r"<div\b([^>]*)>", html, flags=re.IGNORECASE):
        attrs = match.group(1)
        if re.search(r"""data-label\s*=\s*["']Blank-Page["']""", attrs, flags=re.IGNORECASE):
            continue
        bbox = re.search(r"""data-bbox\s*=\s*["']([^"']+)["']""", attrs, flags=re.IGNORECASE)
        if bbox is None:
            continue
        nums = [float(part) for part in bbox.group(1).replace(",", " ").split() if part]
        if len(nums) != 4:
            continue
        nxt = html.find("<div", match.end())
        chunk = html[match.end() : nxt if nxt != -1 else len(html)]
        text = _without_boxes(chunk)
        if not text:
            continue
        box = (
            round(nums[0] / 1000 * width), round(nums[1] / 1000 * height),
            round(nums[2] / 1000 * width), round(nums[3] / 1000 * height),
        )
        blocks.append((box, text))
    leaves = [item for item in blocks if not any(_inside(other, item[0]) for other, _ in blocks)]
    return "\n".join(text for _, text in leaves), leaves


def _inside(inner: Box, outer: Box) -> bool:
    return inner != outer and inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _overlaps(block: Box, crop: Box) -> bool:
    left, top = max(block[0], crop[0]), max(block[1], crop[1])
    right, bottom = min(block[2], crop[2]), min(block[3], crop[3])
    if right <= left or bottom <= top:
        return False
    area = max(1, (block[2] - block[0]) * (block[3] - block[1]))
    return (right - left) * (bottom - top) / area >= 0.3
