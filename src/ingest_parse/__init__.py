"""Парсер txt/doc/docx/docm/rtf/pdf в Markdown."""

from ingest_parse.convert_doc import LibreOfficeNotFoundError
from ingest_parse.detect import UnsupportedFormatError
from ingest_parse.parse import parse_document
from ingest_parse.pdf_triage import PdfPageWarning
from ingest_parse.vision.client import VisionConfigError

__all__ = [
    "parse_document",
    "UnsupportedFormatError",
    "LibreOfficeNotFoundError",
    "PdfPageWarning",
    "VisionConfigError",
]
