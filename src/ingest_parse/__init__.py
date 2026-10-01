"""Парсер txt/doc/docx в текстовые блоки."""

from ingest_parse.convert_doc import LibreOfficeNotFoundError
from ingest_parse.detect import UnsupportedFormatError
from ingest_parse.models import ParsedDocument, TextBlock
from ingest_parse.parse import PARSER_VERSION as __version__
from ingest_parse.parse import parse_document

__all__ = [
    "parse_document",
    "ParsedDocument",
    "TextBlock",
    "UnsupportedFormatError",
    "LibreOfficeNotFoundError",
]
