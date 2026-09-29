"""Публичный API: path|bytes → ParsedDocument (parse-only, без chunk/embed)."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ingest_parse.convert_doc import convert_doc_to_docx
from ingest_parse.detect import SupportedFormat, detect_format
from ingest_parse.extract import extract_from_docling
from ingest_parse.hooks import (
    DEFAULT_IMAGE_PARSER,
    DEFAULT_TABLE_PARSER,
    ImageParser,
    TableParser,
    _call_hook,
)
from ingest_parse.models import ImageBlock, ParsedDocument, TableBlock, TextBlock, make_block_id

CANON_SCHEMA = "ingest_parse.ParsedDocument@0.2"

# Injectable: Protocol-объект ИЛИ callable с теми же kwargs → TableBlock/ImageBlock
TableParserHook = TableParser | Callable[..., TableBlock]
ImageParserHook = ImageParser | Callable[..., ImageBlock]


def _package_version() -> str:
    try:
        return version("ingest-parse")
    except PackageNotFoundError:
        return "0.1.0"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _as_table_parser(hook: TableParserHook | None) -> TableParser:
    """Protocol с .parse_table_block или голый callable → единый интерфейс."""
    if hook is None:
        return DEFAULT_TABLE_PARSER
    if hasattr(hook, "parse_table_block"):
        return hook  # type: ignore[return-value]

    class _CallableTableParser:
        def parse_table_block(self, **kwargs: Any) -> TableBlock:
            return _call_hook(hook, **kwargs)  # type: ignore[arg-type]

    return _CallableTableParser()


def _as_image_parser(hook: ImageParserHook | None) -> ImageParser:
    if hook is None:
        return DEFAULT_IMAGE_PARSER
    if hasattr(hook, "parse_image_block"):
        return hook  # type: ignore[return-value]

    class _CallableImageParser:
        def parse_image_block(self, **kwargs: Any) -> ImageBlock:
            return _call_hook(hook, **kwargs)  # type: ignore[arg-type]

    return _CallableImageParser()


def _parse_txt_utf8(path: Path, *, source_sha256: str, source_path: str) -> ParsedDocument:
    """TXT: UTF-8 абзацы (у Docling нет InputFormat.TXT)."""
    text = path.read_text(encoding="utf-8")
    blocks: list[TextBlock] = []
    for para in text.split("\n\n"):
        cleaned = para.strip()
        if cleaned:
            i = len(blocks)
            blocks.append(
                TextBlock(
                    block_index=i,
                    block_id=make_block_id(i),
                    text=cleaned,
                    section_path=[],
                    label="paragraph",
                )
            )
    if not blocks and text.strip():
        blocks.append(
            TextBlock(
                block_index=0,
                block_id=make_block_id(0),
                text=text.strip(),
                section_path=[],
                label="paragraph",
            )
        )
    return ParsedDocument(
        source_path=source_path,
        source_sha256=source_sha256,
        format="txt",
        title=None,
        blocks=blocks,
        parser="utf8",
        parser_version=_package_version(),
        canon_schema=CANON_SCHEMA,
        warnings=["Parsed as plain UTF-8 (Docling has no InputFormat.TXT)."],
    )


def _convert_with_docling(path: Path):
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter(allowed_formats=[InputFormat.DOCX])
    return converter.convert(str(path)).document


def _parse_docx_path(
    path: Path,
    *,
    source_path: str,
    fmt: SupportedFormat,
    source_sha256: str,
    extra_warnings: list[str] | None = None,
    table_parser: TableParser | None = None,
    image_parser: ImageParser | None = None,
    strict: bool = False,
) -> ParsedDocument:
    doc = _convert_with_docling(path)
    title, blocks, extract_warnings = extract_from_docling(
        doc,
        table_parser=table_parser,
        image_parser=image_parser,
        strict=strict,
    )
    warnings = list(extra_warnings or [])
    warnings.extend(extract_warnings)
    return ParsedDocument(
        source_path=source_path,
        source_sha256=source_sha256,
        format=fmt,
        title=title,
        blocks=blocks,
        parser="docling",
        parser_version=_package_version(),
        canon_schema=CANON_SCHEMA,
        warnings=warnings,
    )


def _parse_path(
    resolved: Path,
    *,
    source_path: str,
    source_sha256: str,
    table_parser: TableParser | None,
    image_parser: ImageParser | None,
    strict: bool,
) -> ParsedDocument:
    fmt = detect_format(resolved)

    if fmt == "txt":
        try:
            return _parse_txt_utf8(
                resolved, source_sha256=source_sha256, source_path=source_path
            )
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"Cannot decode {source_path} as UTF-8. Re-save as UTF-8 or convert to .docx."
            ) from exc

    if fmt == "docx":
        return _parse_docx_path(
            resolved,
            source_path=source_path,
            fmt="docx",
            source_sha256=source_sha256,
            table_parser=table_parser,
            image_parser=image_parser,
            strict=strict,
        )

    with tempfile.TemporaryDirectory(prefix="ingest-parse-doc-") as tmp:
        docx_path = convert_doc_to_docx(resolved, output_dir=tmp)
        return _parse_docx_path(
            docx_path,
            source_path=source_path,
            fmt="doc",
            source_sha256=source_sha256,
            extra_warnings=[
                "Converted .doc → .docx via LibreOffice before Docling parse."
            ],
            table_parser=table_parser,
            image_parser=image_parser,
            strict=strict,
        )


def parse_document(
    source: str | Path | bytes,
    *,
    filename: str | None = None,
    table_parser: TableParserHook | None = None,
    image_parser: ImageParserHook | None = None,
    strict: bool = False,
) -> ParsedDocument:
    """
    Единая точка входа parse-слоя (удобно вшивать в multi-parser микропайплайн).

    Public API (EN)
    ---------------
    Parse txt/doc/docx into a pydantic ``ParsedDocument`` (no chunk/embed).

    Parameters
    ----------
    source:
        File path (``str``/``Path``) **or** raw ``bytes``.
    filename:
        Required for ``bytes`` — name with extension (``.txt``/``.doc``/``.docx``)
        for format detection. Ignored for path input.
    table_parser / image_parser:
        Optional. Protocol with ``parse_*_block(...)`` **or** a callable with the
        same kwargs. ``None`` → defaults from ``ingest_parse.hooks``.
        Image hooks receive ``image_bytes`` (PNG) when Docling yielded pixels —
        upload in your hook; default does **not** persist bytes (``asset_hint=None``).
    strict:
        If ``True``, table/image hook failures and ``block_id`` contract breaks
        raise ``HookExtractError`` / ``HookContractError`` instead of warnings.

    Returns
    -------
    ParsedDocument — use ``model_dump_json()``. Temp files for ``.doc`` convert
    and ``bytes`` input are deleted before return.

    Thread-safety / limits
    ----------------------
    Sync, per-call state only (no globals). Docling/LibreOffice are heavy —
    do not share one converter across threads without your own locking.
    LibreOffice subprocess timeout: 120s (``convert_doc``).

    Параметры (RU)
    --------------
    source:
        Путь к файлу (`str`/`Path`) **или** сырые `bytes` содержимого.
    filename:
        Обязателен для `bytes` — имя с расширением для детекта формата.
    table_parser / image_parser:
        Protocol или callable; `None` → дефолты. Image: `image_bytes` handoff.
    strict:
        `True` → ошибки hook/контракта наружу; иначе → `warnings[]`.

    Пример::

        from ingest_parse import parse_document

        doc = parse_document("report.docx", table_parser=MyTables())
        # или: parse_document(raw_bytes, filename="report.docx", strict=True)
    """
    tp = _as_table_parser(table_parser)
    ip = _as_image_parser(image_parser)

    if isinstance(source, (bytes, bytearray)):
        if not filename:
            raise ValueError(
                "filename=... with extension (.txt/.doc/.docx) is required when source is bytes"
            )
        data = bytes(source)
        source_sha256 = _sha256_bytes(data)
        suffix = Path(filename).suffix.lower() or ".bin"
        # временный файл только чтобы Docling/LibreOffice получили путь; удаляем вместе с tmpdir
        with tempfile.TemporaryDirectory(prefix="ingest-parse-bytes-") as tmp:
            tmp_path = Path(tmp) / Path(filename).name
            if not tmp_path.suffix:
                tmp_path = tmp_path.with_suffix(suffix)
            tmp_path.write_bytes(data)
            return _parse_path(
                tmp_path,
                source_path=f"<bytes:{Path(filename).name}>",
                source_sha256=source_sha256,
                table_parser=tp,
                image_parser=ip,
                strict=strict,
            )

    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(f"File not found: {src}")
    resolved = src.resolve()
    return _parse_path(
        resolved,
        source_path=str(resolved),
        source_sha256=_sha256_file(resolved),
        table_parser=tp,
        image_parser=ip,
        strict=strict,
    )
