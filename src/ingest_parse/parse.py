"""Публичный вход: parse_document."""

from __future__ import annotations

import codecs
import hashlib
import re
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ingest_parse.convert_doc import convert_doc_to_docx
from ingest_parse.detect import SupportedFormat, detect_format
from ingest_parse.extract import extract_from_docling
from ingest_parse.models import ParsedDocument, TextBlock, make_block_id

_BLANK_LINE = re.compile(r"\n[ \t]*\n")

try:
    PARSER_VERSION = version("ingest-parse")
except PackageNotFoundError:
    PARSER_VERSION = "unknown"


def _decode_txt(raw: bytes) -> tuple[str, str]:
    """Порядок: BOM → UTF-8 → cp1251."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1251"), "cp1251"


def _parse_txt(raw: bytes, *, source_sha256: str, source_path: str) -> ParsedDocument:
    """Абзацы по пустым строкам."""
    try:
        text, encoding = _decode_txt(raw)
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Cannot decode {source_path} as UTF-8/UTF-16/cp1251. "
            "Re-save as UTF-8 or convert to .docx."
        ) from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[TextBlock] = []
    for para in _BLANK_LINE.split(text):
        cleaned = para.strip()
        if cleaned:
            i = len(blocks)
            blocks.append(
                TextBlock(block_index=i, block_id=make_block_id(i), text=cleaned, label="paragraph")
            )
    return ParsedDocument(
        source_path=source_path,
        source_sha256=source_sha256,
        format="txt",
        blocks=blocks,
        parser="utf8",
        parser_version=PARSER_VERSION,
        warnings=[f"Parsed as plain text, encoding={encoding} (Docling has no InputFormat.TXT)."],
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
    warnings: list[str] | None = None,
) -> ParsedDocument:
    title, blocks = extract_from_docling(_convert_with_docling(path))
    return ParsedDocument(
        source_path=source_path,
        source_sha256=source_sha256,
        format=fmt,
        title=title,
        blocks=blocks,
        parser="docling",
        parser_version=PARSER_VERSION,
        warnings=warnings or [],
    )


def _parse_path(path: Path, *, source_path: str) -> ParsedDocument:
    fmt = detect_format(path)
    raw = path.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()

    if fmt == "txt":
        return _parse_txt(raw, source_sha256=source_sha256, source_path=source_path)

    if fmt == "docx":
        return _parse_docx_path(
            path, source_path=source_path, fmt="docx", source_sha256=source_sha256
        )

    with tempfile.TemporaryDirectory(prefix="ingest-parse-doc-") as tmp:
        return _parse_docx_path(
            convert_doc_to_docx(path, output_dir=tmp),
            source_path=source_path,
            fmt="doc",
            source_sha256=source_sha256,
            warnings=["Converted .doc → .docx via LibreOffice before Docling parse."],
        )


def parse_document(
    source: str | Path | bytes,
    *,
    filename: str | None = None,
) -> ParsedDocument:
    """txt/doc/docx → текстовые блоки. Для bytes нужен filename с расширением."""
    if isinstance(source, (bytes, bytearray)):
        if not filename:
            raise ValueError(
                "filename=... with extension (.txt/.doc/.docx) is required when source is bytes"
            )
        name = Path(filename).name
        with tempfile.TemporaryDirectory(prefix="ingest-parse-bytes-") as tmp:
            tmp_path = Path(tmp) / name
            tmp_path.write_bytes(bytes(source))
            return _parse_path(tmp_path, source_path=f"<bytes:{name}>")

    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(f"File not found: {src}")
    resolved = src.resolve()
    return _parse_path(resolved, source_path=str(resolved))
