"""PDF → картинки страниц в родном разрешении скана (300–450 dpi) + текстовый слой, если он есть."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image

MIN_DPI = 300
MAX_DPI = 450
_SCAN_SHARE = 0.5  # картинка на ≥ половину страницы — это скан


@dataclass
class PageImage:
    number: int  # с 1
    image: Image.Image
    dpi: float
    native_dpi: float | None  # разрешение встроенного скана; None — цифровая страница
    text: str  # текстовый слой ("" у чистого скана)


def _native_dpi(page: pdfium.PdfPage) -> float | None:
    width, height = page.get_size()
    best = None
    for obj in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=3):
        left, bottom, right, top = obj.get_bounds()
        w_pt, h_pt = right - left, top - bottom
        if w_pt <= 0 or h_pt <= 0 or w_pt * h_pt < _SCAN_SHARE * width * height:
            continue
        px_w, px_h = obj.get_px_size()
        dpi = max(px_w, px_h) / (max(w_pt, h_pt) / 72)  # длинная сторона — устойчиво к повороту картинки
        best = max(best or 0.0, dpi)
    return best


def _text(page: pdfium.PdfPage) -> str:
    textpage = page.get_textpage()
    try:
        return textpage.get_text_range().strip()
    finally:
        textpage.close()


def rasterize(path: Path) -> Iterator[PageImage]:
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        raise ValueError(f"cannot open PDF {path}: {exc} (password-protected or damaged?)") from exc
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                native = _native_dpi(page)
                dpi = min(MAX_DPI, max(MIN_DPI, native or MIN_DPI))
                image = page.render(scale=dpi / 72).to_pil()
                yield PageImage(index + 1, image, dpi, native, _text(page))
            finally:
                page.close()
    finally:
        pdf.close()
