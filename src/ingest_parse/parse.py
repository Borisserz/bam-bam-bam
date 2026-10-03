"""Публичный вход: parse_document → Markdown."""

from __future__ import annotations

import codecs
import functools
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ingest_parse.convert_doc import convert_doc_to_docx, docm_to_docx, docx_to_pdf, find_soffice
from ingest_parse.detect import detect_format
from ingest_parse.markdown import docling_to_markdown
from ingest_parse.media import MediaWriter
from ingest_parse.pdf_scan import ScanOptions, fill_scans
from ingest_parse.pdf_text import apply_text_layer
from ingest_parse.pdf_triage import triage_pdf
from ingest_parse.prepare import prepare_docx
from ingest_parse.shapes import mark_shapes, render_pages

_BLANK_LINE = re.compile(r"\n[ \t]*\n")


def _decode_txt(raw: bytes) -> str:
    """Порядок: BOM → UTF-8 → cp1251."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251")


def _txt_to_markdown(path: Path) -> str:
    """Абзацы по пустым строкам."""
    try:
        text = _decode_txt(path.read_bytes())
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Cannot decode {path} as UTF-8/UTF-16/cp1251. Re-save as UTF-8 or convert to .docx."
        ) from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p.strip() for p in _BLANK_LINE.split(text) if p.strip()]
    return "\n\n".join(paragraphs) + "\n" if paragraphs else ""


def _docx_to_markdown(path: Path, media: MediaWriter | None) -> str:
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter(allowed_formats=[InputFormat.DOCX])
    render = media is not None and find_soffice() is not None
    with tempfile.TemporaryDirectory(prefix="ingest-parse-docx-", ignore_cleanup_errors=True) as tmp:
        prepared = Path(tmp) / "prepared" / f"{path.stem}.docx"
        prepared.parent.mkdir()
        changed, shapes = prepare_docx(path, prepared, shapes=render)
        source = prepared if changed else path
        if shapes:
            # в PDF — копия только с невидимыми метками фигур, без меток номеров и индексов
            for_pdf = Path(tmp) / "pdf-src" / f"{path.stem}.docx"
            for_pdf.parent.mkdir()
            mark_shapes(path, for_pdf)
            pdf = docx_to_pdf(for_pdf, output_dir=Path(tmp) / "pdf")
            shapes = render_pages(pdf, shapes, media)
            return docling_to_markdown(converter.convert(str(source)).document, media, shapes)
        return docling_to_markdown(converter.convert(str(source)).document, media)


@functools.lru_cache(maxsize=1)
def _pdf_converter() -> Any:
    """Модели раскладки и таблиц грузятся один раз на процесс; OCR выключен — только текстовый слой."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(do_ocr=False, generate_picture_images=True, images_scale=2.0)
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )


def _pdf_to_markdown(path: Path, media: MediaWriter | None, scan: ScanOptions) -> str:
    triage = triage_pdf(path)
    triage.warn()
    if triage.usable:
        doc = _pdf_converter().convert(str(path)).document
        overrides = apply_text_layer(doc, path)
        md = docling_to_markdown(doc, media, skipped=triage.skipped, overrides=overrides)
    else:
        md = triage.stub()  # одни сканы и пустые страницы — Docling не нужен
    scans = [p.number for p in triage.pages if p.kind == "full_scan"]
    return fill_scans(md, path, scans, media, scan)


def _parse_path(path: Path, media: MediaWriter | None, scan: ScanOptions) -> str:
    fmt = detect_format(path)
    if fmt == "txt":
        return _txt_to_markdown(path)
    if fmt == "docx":
        return _docx_to_markdown(path, media)
    if fmt == "pdf":
        return _pdf_to_markdown(path, media, scan)
    with tempfile.TemporaryDirectory(prefix="ingest-parse-doc-", ignore_cleanup_errors=True) as tmp:
        convert = docm_to_docx if fmt == "docm" else convert_doc_to_docx
        return _docx_to_markdown(convert(path, output_dir=tmp), media)


def parse_to_markdown(
    source: str | Path | bytes,
    *,
    filename: str | None = None,
    media_dir: str | Path | None = None,
    media_link: str | None = None,
    vision: bool = False,
    vision_force: bool = False,
    links_base: str | Path | None = None,
    ocr_fallback: bool | None = None,
) -> str:
    """parse_document + media_link: префикс ссылок на картинки (CLI — относительно out.md).

    links_base: папка, от которой считаются ссылки на картинки (для vision; по умолчанию cwd).
    ocr_fallback: None — из INGEST_PDF_OCR_FALLBACK=1.
    """
    if ocr_fallback is None:
        ocr_fallback = os.environ.get("INGEST_PDF_OCR_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")
    if not (vision or vision_force):
        return _parse_source(source, filename, media_dir, media_link, ScanOptions(ocr=ocr_fallback))

    from ingest_parse.vision import VisionClient, VisionConfig, enrich_markdown

    config = VisionConfig.from_env()  # без адреса API — ошибка до разбора
    client = VisionClient(config)
    scan = ScanOptions(client, vision_force, config.max_long_edge, ocr_fallback)
    md = _parse_source(source, filename, media_dir, media_link, scan)
    return enrich_markdown(
        md,
        base_dir=Path(links_base) if links_base is not None else Path.cwd(),
        client=client,
        force=vision_force,
        max_long_edge=config.max_long_edge,
    )


def _parse_source(
    source: str | Path | bytes,
    filename: str | None,
    media_dir: str | Path | None,
    media_link: str | None,
    scan: ScanOptions,
) -> str:
    media = None
    if media_dir is not None:
        link = Path(media_dir).as_posix() if media_link is None else media_link
        media = MediaWriter(media_dir, link)
    if isinstance(source, (bytes, bytearray)):
        if not filename:
            raise ValueError(
                "filename=... with extension (.txt/.doc/.docx/.docm/.rtf/.pdf) is required when source is bytes"
            )
        with tempfile.TemporaryDirectory(prefix="ingest-parse-bytes-", ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp) / Path(filename).name
            tmp_path.write_bytes(bytes(source))
            return _parse_path(tmp_path, media, scan)

    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(f"File not found: {src}")
    return _parse_path(src.resolve(), media, scan)


def parse_document(
    source: str | Path | bytes,
    *,
    filename: str | None = None,
    media_dir: str | Path | None = None,
    vision: bool = False,
    ocr_fallback: bool | None = None,
) -> str:
    """txt/doc/docx/docm/rtf/pdf → Markdown. Для bytes нужен filename с расширением.

    pdf: текст — из текстового слоя (без OCR). Страница-скан → scan-NNN.png и блок
    <!-- pdf-scan:begin … --> с текстом от VLM (vision) или OCR (ocr_fallback), иначе заглушка;
    сканы и пустые страницы → PdfPageWarning.

    media_dir: картинки пишутся туда как img-NNN.<ext>, в Markdown — ![подпись](media_dir/img-NNN.<ext>).
    Без media_dir файлы не создаются, от картинок остаётся только текст подписи.
    vision: над каждой картинкой — описание от VLM, страницы-сканы — текст от VLM (нужен
    VISION_API_BASE_URL; картинки уходят на этот API). Без адреса API — VisionConfigError.
    ocr_fallback: сканы без vision или при ошибке VLM — RapidOCR (extra ocr); None — из INGEST_PDF_OCR_FALLBACK.
    """
    return parse_to_markdown(
        source, filename=filename, media_dir=media_dir, vision=vision, ocr_fallback=ocr_fallback
    )
