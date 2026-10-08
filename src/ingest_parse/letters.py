"""Подсказка текста для промпта Qwen. Без флага --chandra слот молчит."""

from __future__ import annotations

import os
import re
import warnings

from PIL import Image

_ENABLED = False
_MODEL = "chandra"
_PROMPT = """
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


def set_chandra(enabled: bool) -> None:
    """Включает локальную подсказку. Без вызова из CLI слот не ходит в сеть."""
    global _ENABLED
    _ENABLED = bool(enabled)


def read_page_letters(image: Image.Image) -> str:
    """Текст страницы для хвоста промпта Qwen. Без флага всегда пусто.

    С флагом и SCAN_CHANDRA_URL это markdown локального Chandra 2.
    Картинка главнее подсказки. Пустую клетку из подсказки не заполнять.
    Рамки ответа в разметку страницы не входят.
    """
    if not _ENABLED:
        return ""
    url = os.environ.get("SCAN_CHANDRA_URL", "").strip()
    if not url:
        return _off("chandra off (SCAN_CHANDRA_URL is empty)")
    if not url.startswith(("http://", "https://")):
        return _off(f"chandra off (SCAN_CHANDRA_URL must start with http): {url}")
    from ingest_parse.ttn.dots import http_sender

    model = os.environ.get("SCAN_CHANDRA_MODEL", "").strip() or _MODEL
    try:
        raw = http_sender(url, model)(image, _PROMPT)
        text = _without_boxes(raw)
    except Exception as exc:  # noqa: BLE001 — сервер недоступен, Qwen идёт без подсказки
        return _off(f"chandra FAILED {type(exc).__name__}: {exc}")
    if not text:
        return _off("chandra FAILED empty answer")
    print(f"  chandra chars={len(text)}", flush=True)
    return text


def page_hint(pdf_text: str, image: Image.Image) -> str:
    """Текстовый слой PDF и слот букв. Буквы Dots сюда не входят."""
    extra = read_page_letters(image)
    parts = [p.strip() for p in (pdf_text, extra) if p and p.strip()]
    return "\n\n".join(parts)


def _off(reason: str) -> str:
    print(f"  {reason}", flush=True)
    warnings.warn(reason, stacklevel=3)
    return ""


def _without_boxes(html: str) -> str:
    """Текст блоков без координат. Атрибут data-bbox в подсказку не попадает."""
    text = re.sub(r"""\sdata-bbox\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", "", html, flags=re.IGNORECASE)
    text = re.sub(r"</(div|p|h[1-6]|tr|li|table)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("data-bbox", "")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()
