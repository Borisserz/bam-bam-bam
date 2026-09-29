"""ingest_parse — parse-only: txt/doc/docx → ParsedDocument (+ table/image hooks)."""

from ingest_parse.convert_doc import LibreOfficeNotFoundError, convert_doc_to_docx, find_soffice
from ingest_parse.detect import UnsupportedFormatError, detect_format
from ingest_parse.errors import HookContractError, HookExtractError, IngestParseError
from ingest_parse.extract import extract_from_docling
from ingest_parse.hooks import (
    DEFAULT_IMAGE_PARSER,
    DEFAULT_TABLE_PARSER,
    DefaultImageParser,
    DefaultTableParser,
    ImageParser,
    TableParser,
    load_image_bytes,
    parse_image_block,
    parse_table_block,
)
from ingest_parse.models import ImageBlock, ParsedDocument, TableBlock, TextBlock, make_block_id
from ingest_parse.parse import parse_document

__all__ = [
    "parse_document",
    "ParsedDocument",
    "TextBlock",
    "TableBlock",
    "ImageBlock",
    "make_block_id",
    "TableParser",
    "ImageParser",
    "DefaultTableParser",
    "DefaultImageParser",
    "DEFAULT_TABLE_PARSER",
    "DEFAULT_IMAGE_PARSER",
    "load_image_bytes",
    "IngestParseError",
    "HookExtractError",
    "HookContractError",
    "UnsupportedFormatError",
    "LibreOfficeNotFoundError",
    # advanced / tests
    "parse_table_block",
    "parse_image_block",
    "detect_format",
    "convert_doc_to_docx",
    "find_soffice",
    "extract_from_docling",
]

__version__ = "0.1.0"
