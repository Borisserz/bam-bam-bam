"""Heron (модель раскладки Docling) на страницах-сканах PDF: какие области где лежат."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Region:
    label: str  # метка Heron: text, section_header, table, picture, page_footer, …
    confidence: float
    box: tuple[float, float, float, float]  # l, t, r, b в пунктах PDF; начало — левый верхний угол


@functools.lru_cache(maxsize=1)
def _converter() -> Any:
    """Без OCR и TableFormer; keep_empty_clusters — иначе Docling выбросит все области без текстового слоя."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    layout = LayoutObjectDetectionOptions.from_preset("layout_heron_default")
    layout.keep_empty_clusters = True
    options = PdfPipelineOptions(do_ocr=False, do_table_structure=False, layout_options=layout)
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )


def detect_layout(path: Path, numbers: list[int]) -> dict[int, list[Region]]:
    """{номер страницы (с 1): области}; Docling — только на диапазоне min..max(numbers)."""
    if not numbers:
        return {}
    result = _converter().convert(str(path), page_range=(min(numbers), max(numbers)))
    wanted = set(numbers)
    out: dict[int, list[Region]] = {}
    for page in result.pages:
        if page.page_no not in wanted:
            continue
        layout = page.predictions.layout
        height = page.size.height
        out[page.page_no] = [
            Region(c.label.value, float(c.confidence), _box(c.bbox.to_top_left_origin(page_height=height)))
            for c in (layout.clusters if layout else [])
        ]
    return out


def _box(b: Any) -> tuple[float, float, float, float]:
    return (float(b.l), float(b.t), float(b.r), float(b.b))
