"""Heron против dots.mocr на одних и тех же выровненных страницах: таблицы, покрытие чернил, время.

    uv run python scripts/compare_layout.py examples/ttn_public/raw -o examples/ttn_public/compare \
        --dots-url http://127.0.0.1:8091

dots поднимается отдельно (mlx_vlm.server на Mac, vLLM / llama.cpp на ПК) — OpenAI-совместимый API.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ingest_parse.ttn.preprocess import prepare
from ingest_parse.ttn.raster import rasterize
from ingest_parse.ttn.zones import heron_regions

DOTS_LAYOUT = (
    "Please output the layout information from this PDF image, including each layout's bbox and its category. "
    "The bbox should be in the format [x1, y1, x2, y2]. The layout categories for the PDF document include "
    "['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', "
    "'Table', 'Text', 'Title']. Do not output the corresponding text. The layout result should be in JSON format."
)
DOTS_ALL = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""
_ITEM = re.compile(r'\{\s*"bbox"\s*:\s*\[([\d.,\s]+)\]\s*,\s*"category"\s*:\s*"([^"]+)"')
_COLOR = {"table": (0, 160, 0), "picture": (0, 90, 255), "margin": (150, 150, 150), "text": (220, 0, 0), "container": (255, 150, 0)}
_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}


def role(label: str) -> str:
    key = label.lower().replace("-", "_")
    if key == "table":
        return "table"
    if key in ("picture", "chart"):
        return "picture"
    if key in ("page_header", "page_footer"):
        return "margin"
    if key in ("form", "key_value_region", "document_index"):
        return "container"
    return "text"


def pages(path: Path):
    """(номер, картинка, dpi) — PDF через растеризатор ТТН, картинка — dpi по длинной стороне A4."""
    if path.suffix.lower() == ".pdf":
        for p in rasterize(path):
            yield p.number, p.image, p.dpi
        return
    with Image.open(path) as im:
        frames = getattr(im, "n_frames", 1)
        for i in range(frames):
            im.seek(i)
            image = im.convert("RGB")
            dpi = min(600.0, max(72.0, max(image.size) / 11.69))
            yield i + 1, image, dpi


def ask_dots(image: Image.Image, url: str, model: str, prompt: str, max_tokens: int, long_edge: int):
    """Рамки dots в пикселях image; картинка уходит кратной 28 по сторонам (так режет vision-энкодер Qwen2-VL)."""
    scale = min(1.0, long_edge / max(image.size))
    w, h = (max(28, round(image.width * scale / 28) * 28), max(28, round(image.height * scale / 28) * 28))
    sent = image.convert("RGB").resize((w, h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    sent.save(buf, format="PNG")
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
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    answer = json.load(urllib.request.urlopen(req, timeout=1800))["choices"][0]["message"]["content"]
    took = time.time() - t
    status = "ok"
    try:
        raw = json.loads(answer)
        items = raw if isinstance(raw, list) else raw.get("layout") or raw.get("elements") or next(
            (v for v in raw.values() if isinstance(v, list)), []
        )
        found = [(str(it["category"]), [float(v) for v in it["bbox"]], it.get("text", "")) for it in items]
    except (ValueError, KeyError, TypeError, AttributeError):
        found = [(cat, [float(v) for v in nums.split(",")[:4]], "") for nums, cat in _ITEM.findall(answer)]
        status = "salvaged" if found else "invalid"
    fx, fy = image.width / w, image.height / h
    boxes = [(cat, 1.0, (b[0] * fx, b[1] * fy, b[2] * fx, b[3] * fy), text) for cat, b, text in found if len(b) == 4]
    return boxes, took, status, answer


def area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def iou(a, b) -> float:
    inter = area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))
    union = area(a) + area(b) - inter
    return inter / union if union else 0.0


def match_tables(xs, ys) -> list[float]:
    """Жадное сопоставление таблиц по IoU; для каждой таблицы Heron — лучший IoU с dots (0 — не нашёл)."""
    left = list(ys)
    out = []
    for a in sorted(xs, key=area, reverse=True):
        best = max(left, key=lambda b: iou(a, b), default=None)
        score = iou(a, best) if best is not None else 0.0
        if best is not None and score > 0.1:
            left.remove(best)
        out.append(score)
    return out


def ink_coverage(gray: np.ndarray, boxes) -> float:
    ink = gray < 128
    total = int(ink.sum())
    if not total:
        return 1.0
    mask = np.zeros_like(ink)
    for b in boxes:
        l, t, r, btm = (int(max(0, b[0])), int(max(0, b[1])), int(min(gray.shape[1], b[2])), int(min(gray.shape[0], b[3])))
        mask[t:btm, l:r] = True
    return float((ink & mask).sum() / total)


def overlay(image: Image.Image, regions, title: str) -> Image.Image:
    scale = 1000 / max(image.size)
    out = image.convert("RGB").resize((int(image.width * scale), int(image.height * scale)))
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default(size=14)
    for label, _, b, *_ in regions:
        color = _COLOR[role(label)]
        box = [v * scale for v in b]
        draw.rectangle(box, outline=color, width=3)
        draw.text((box[0] + 3, box[1] + 2), label, fill=color, font=font)
    head = Image.new("RGB", (out.width, 36), "white")
    ImageDraw.Draw(head).text((8, 8), title, fill="black", font=ImageFont.load_default(size=20))
    full = Image.new("RGB", (out.width, out.height + 36), "white")
    full.paste(head, (0, 0))
    full.paste(out, (0, 36))
    return full


def side_by_side(a: Image.Image, b: Image.Image) -> Image.Image:
    out = Image.new("RGB", (a.width + b.width + 12, max(a.height, b.height)), (40, 40, 40))
    out.paste(a, (0, 0))
    out.paste(b, (a.width + 12, 0))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--dots-url", default="http://127.0.0.1:8091")
    ap.add_argument("--dots-model", default="mlx-community/dots.mocr-8bit")
    ap.add_argument("--dots-mode", choices=("layout", "all"), default="layout")
    ap.add_argument("--dots-edge", type=int, default=1600, help="длинная сторона картинки для dots, px")
    ap.add_argument("--max-pages", type=int, default=2, help="страниц с одного файла")
    args = ap.parse_args(argv)

    files = sorted(p for i in args.inputs for p in ([i] if i.is_file() else i.iterdir()) if p.suffix.lower() in _SUFFIXES)
    args.out.mkdir(parents=True, exist_ok=True)
    results_path = args.out / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {}
    prompt, max_tokens = (DOTS_LAYOUT, 3000) if args.dots_mode == "layout" else (DOTS_ALL, 8000)
    for path in files:
        for number, image, dpi in pages(path):
            if number > args.max_pages:
                break
            key = f"{path.name}#{number}"
            if key in results and results[key].get("dots_mode") == args.dots_mode:
                print(f"skip {key} (готово)")
                continue
            prep = prepare(image, dpi)
            page = prep.image
            gray = np.asarray(page.convert("L"), dtype=np.uint8)
            t = time.time()
            heron = heron_regions(page, prep.dpi)
            heron_s = time.time() - t
            try:
                dots, dots_s, status, answer = ask_dots(page, args.dots_url, args.dots_model, prompt, max_tokens, args.dots_edge)
            except Exception as exc:  # сервер упал / таймаут — страница всё равно в отчёте
                dots, dots_s, status, answer = [], 0.0, f"error: {exc}", ""
            stem = f"{path.stem}-p{number}"
            (args.out / f"{stem}-dots.txt").write_text(answer, encoding="utf-8")
            h_tables = [b for label, _, b in heron if role(label) == "table"]
            d_tables = [b for label, _, b, _ in dots if role(label) == "table"]
            row = {
                "dots_mode": args.dots_mode,
                "size": list(page.size),
                "dpi": round(prep.dpi),
                "preprocess": prep.steps,
                "heron": {"n": len(heron), "tables": len(h_tables), "s": round(heron_s, 1),
                          "ink": round(ink_coverage(gray, [b for _, _, b in heron]), 3)},
                "dots": {"n": len(dots), "tables": len(d_tables), "s": round(dots_s, 1), "status": status,
                         "ink": round(ink_coverage(gray, [b for _, _, b, _ in dots]), 3)},
                "table_iou": [round(v, 3) for v in match_tables(h_tables, d_tables)],
            }
            results[key] = row
            results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            pic = side_by_side(
                overlay(page, heron, f"Heron: {len(heron)} обл., таблиц {len(h_tables)}, {heron_s:.0f} c"),
                overlay(page, dots, f"dots: {len(dots)} обл., таблиц {len(d_tables)}, {dots_s:.0f} c, {status}"),
            )
            pic.save(args.out / f"{stem}.png")
            print(f"{key}: heron {row['heron']} | dots {row['dots']} | iou {row['table_iou']}", flush=True)
    write_summary(results, args.out / "summary.md")
    return 0


def write_summary(results: dict, path: Path) -> None:
    lines = [
        "| страница | Heron: обл. / табл. / покрытие / с | dots: обл. / табл. / покрытие / с | IoU таблиц | dots статус |",
        "|---|---|---|---|---|",
    ]
    for key, r in results.items():
        h, d = r["heron"], r["dots"]
        lines.append(
            f"| {key} | {h['n']} / {h['tables']} / {h['ink']:.0%} / {h['s']} | {d['n']} / {d['tables']} / {d['ink']:.0%} / {d['s']} "
            f"| {', '.join(f'{v:.2f}' for v in r['table_iou']) or '—'} | {d['status']} |"
        )
    rows = list(results.values())
    if rows:
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
        ious = [v for r in rows for v in r["table_iou"]]
        lines += [
            "",
            f"Страниц: {len(rows)}. Покрытие чернил: Heron {mean([r['heron']['ink'] for r in rows]):.0%}, "
            f"dots {mean([r['dots']['ink'] for r in rows]):.0%}. "
            f"Время: Heron {mean([r['heron']['s'] for r in rows]):.1f} c, dots {mean([r['dots']['s'] for r in rows]):.1f} c на страницу. "
            f"Таблиц Heron нашёл {sum(r['heron']['tables'] for r in rows)}, dots {sum(r['dots']['tables'] for r in rows)}; "
            f"средний IoU совпавших {mean([v for v in ious if v > 0.1]):.2f}, без пары у dots {sum(v <= 0.1 for v in ious)}. "
            f"Ответов dots не JSON: {sum(r['dots']['status'] != 'ok' for r in rows)}.",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
