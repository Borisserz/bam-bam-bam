"""Страницы PDF до Docling: digital / hybrid / full_scan / blank. Только pypdfium2, без OCR."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

PageKind = Literal["digital", "hybrid", "full_scan", "blank"]

MIN_CHARS = 20  # меньше видимых символов — текстового слоя, считай, нет
SCAN_COVERAGE = 0.5  # доля площади страницы под растровыми картинками
GARBAGE_SHARE = 0.3  # доля U+FFFD: текстовый слой есть, но нечитаемый
INVISIBLE_SHARE = 0.8  # доля невидимых текстовых объектов (render mode 3) — OCR-слой поверх скана

SCAN_PLACEHOLDER = "<!-- page {n}: scanned / no usable text layer; skipped -->"  # pdf_scan заменяет блоком
_PLACEHOLDER = {
    "full_scan": SCAN_PLACEHOLDER,
    "blank": "<!-- page {n}: blank; skipped -->",
}
_WARNING = {  # full_scan предупреждает pdf_scan — с тем, чем кончилось: vision / ocr / заглушка
    "blank": "pdf page {n} classified as blank; skipped",
    "hybrid": "pdf page {n} classified as hybrid (scan with OCR text layer); text layer used as is",
}
_VECTOR_ONLY = "pdf page {n} has only vector graphics, no text; kept only if Docling detects a picture there"


class PdfPageWarning(UserWarning):
    """Страница PDF без пригодного текста: скан (vision / OCR / заглушка), пустая, только векторная графика."""


@dataclass(frozen=True)
class PageInfo:
    number: int  # с 1
    kind: PageKind
    chars: int
    image_coverage: float
    vector_only: bool = False  # ни текста, ни растра — только линии и фигуры


@dataclass(frozen=True)
class TriageResult:
    pages: list[PageInfo]

    @property
    def usable(self) -> bool:
        """Есть хоть одна страница с текстовым слоем — Docling нужен."""
        return any(p.kind in ("digital", "hybrid") for p in self.pages)

    @property
    def skipped(self) -> dict[int, str]:
        """Страницы-сканы → HTML-комментарий на их месте; пустые страницы — только предупреждение."""
        return {p.number: _PLACEHOLDER[p.kind].format(n=p.number) for p in self.pages if p.kind == "full_scan"}

    def stub(self) -> str:
        """Markdown файла без единой страницы с текстом: заглушки вместо пустого «успеха»."""
        return "\n\n".join(_PLACEHOLDER[p.kind].format(n=p.number) for p in self.pages if p.kind in _PLACEHOLDER) + "\n"

    def warn(self) -> None:
        for p in self.pages:
            if p.kind in _WARNING:
                warnings.warn(_WARNING[p.kind].format(n=p.number), PdfPageWarning, stacklevel=2)
            elif p.vector_only:
                warnings.warn(_VECTOR_ONLY.format(n=p.number), PdfPageWarning, stacklevel=2)


def _classify(page: pdfium.PdfPage) -> tuple[PageKind, int, float]:
    width, height = page.get_size()
    area = max(width * height, 1.0)
    covered, drawn = 0.0, False
    texts = invisible = 0
    for obj in page.get_objects():
        if obj.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
            texts += 1
            mode = pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw)
            invisible += mode == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE
            continue
        drawn = True
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            left, bottom, right, top = obj.get_bounds()
            w = min(right, width) - max(left, 0.0)
            h = min(top, height) - max(bottom, 0.0)
            covered += max(w, 0.0) * max(h, 0.0)
    coverage = min(covered / area, 1.0)

    textpage = page.get_textpage()
    try:
        text = textpage.get_text_range()
    finally:
        textpage.close()
    chars = sum(not c.isspace() for c in text)
    garbage = text.count("\ufffd")

    if chars >= MIN_CHARS:
        if garbage / chars >= GARBAGE_SHARE:
            return "full_scan", chars, coverage
        ocr_layer = coverage >= SCAN_COVERAGE and invisible >= INVISIBLE_SHARE * texts
        return ("hybrid" if ocr_layer else "digital"), chars, coverage
    if coverage >= SCAN_COVERAGE:
        return "full_scan", chars, coverage
    if chars == 0 and not drawn:
        return "blank", chars, coverage
    return "digital", chars, coverage  # рисунок или схема почти без текста — пусть разбирает Docling


def triage_pdf(path: str | Path) -> TriageResult:
    """Классификация каждой страницы; ValueError для запароленного или битого PDF."""
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ValueError(f"{path}: PDF is password-protected; remove the password and retry") from exc
        raise ValueError(f"Cannot open PDF {path}: {exc}") from exc
    try:
        pages = []
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                kind, chars, coverage = _classify(page)
            finally:
                page.close()
            vector_only = kind == "digital" and chars == 0 and coverage == 0
            pages.append(PageInfo(index + 1, kind, chars, round(coverage, 3), vector_only))
        return TriageResult(pages)
    finally:
        pdf.close()
