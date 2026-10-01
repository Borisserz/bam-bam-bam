"""Модели результата парсинга."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CANON_SCHEMA = "ingest_parse.ParsedDocument@0.3"


def make_block_id(block_index: int) -> str:
    return f"b{block_index:04d}"


class TextBlock(BaseModel):
    block_index: int = Field(ge=0)
    block_id: str
    text: str
    section_path: list[str] = Field(default_factory=list)
    label: str | None = None  # title | section_header | text | paragraph | list_item
    heading_level: int | None = None


class ParsedDocument(BaseModel):
    source_path: str
    source_sha256: str
    format: Literal["txt", "doc", "docx"]
    title: str | None = None
    blocks: list[TextBlock] = Field(default_factory=list)
    parser: str
    parser_version: str
    canon_schema: str = CANON_SCHEMA
    warnings: list[str] = Field(default_factory=list)

    def plain_text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())
