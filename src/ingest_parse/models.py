"""Канонические блоки parse (колонка 2 схемы Ильи) — без chunk/embed."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


def make_block_id(block_index: int) -> str:
    """Стабильный id блока внутри документа (для будущих chunk seq / cite)."""
    return f"b{block_index:04d}"


class TextBlock(BaseModel):
    """Обычный текст / заголовок / элемент списка."""

    type: Literal["text"] = "text"
    block_index: int = Field(ge=0)
    block_id: str
    text: str
    page: int | None = None
    section_path: list[str] = Field(default_factory=list)
    label: str | None = None  # section_header | paragraph | list_item | …
    heading_level: int | None = None


class TableBlock(BaseModel):
    """Таблица: матрица ячеек (не только flattened text)."""

    type: Literal["table"] = "table"
    block_index: int = Field(ge=0)
    block_id: str
    rows: list[list[str]]
    text: str | None = None  # снимок для отладки/цитат; источник правды — rows
    page: int | None = None
    section_path: list[str] = Field(default_factory=list)
    label: str = "table"
    has_header_row: bool = False
    caption: str | None = None
    # команда допишет TableNormalizer / LLM-паспорт через TableParser
    parser_hook: str = "default_table_parser"


class ImageBlock(BaseModel):
    """Картинка: метаданные + байты/путь; vision/OCR — через ImageParser."""

    type: Literal["image"] = "image"
    block_index: int = Field(ge=0)
    block_id: str
    page: int | None = None
    section_path: list[str] = Field(default_factory=list)
    label: str = "picture"
    caption: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    # sha256 сырых байтов, если удалось извлечь; иначе None
    content_sha256: str | None = None
    # куда команда положит asset в S3 (заполняет оркестратор; здесь подсказка)
    asset_hint: str | None = None
    # pending = ждём vision/OCR ветку; skipped = байтов нет
    vision_status: Literal["pending", "skipped", "done"] = "pending"
    parser_hook: str = "default_image_parser"


ContentBlock = TextBlock | TableBlock | ImageBlock


class ParsedDocument(BaseModel):
    """Результат parse-only: упорядоченные блоки с type discriminator."""

    source_path: str
    source_sha256: str
    format: Literal["txt", "doc", "docx"]
    title: str | None = None
    blocks: list[ContentBlock] = Field(default_factory=list)
    parser: str = "docling"
    parser_version: str = "0.1.0"
    canon_schema: str = "ingest_parse.ParsedDocument@0.2"
    warnings: list[str] = Field(default_factory=list)

    def plain_text(self) -> str:
        """Текстовая склейка (таблицы → TSV; картинки → [image:…])."""
        parts: list[str] = []
        for block in self.blocks:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    parts.append(block.text)
            elif isinstance(block, TableBlock):
                if block.text and block.text.strip():
                    parts.append(block.text)
                elif block.rows:
                    parts.append("\n".join("\t".join(c for c in row) for row in block.rows))
            elif isinstance(block, ImageBlock):
                cap = block.caption or block.content_sha256 or block.block_id
                parts.append(f"[image:{cap}]")
        return "\n\n".join(parts)
