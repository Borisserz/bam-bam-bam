"""Обход DoclingDocument → упорядоченные блоки (text / table / image)."""

from __future__ import annotations

from typing import Any

from ingest_parse.docling_types import DoclingDocumentLike
from ingest_parse.errors import HookContractError, HookExtractError
from ingest_parse.hooks import (
    DEFAULT_IMAGE_PARSER,
    DEFAULT_TABLE_PARSER,
    ImageParser,
    TableParser,
    load_image_bytes,
    parse_image_block,
    parse_table_block,
)
from ingest_parse.models import ContentBlock, ImageBlock, TableBlock, TextBlock, make_block_id


def _page_from_prov(item: Any) -> int | None:
    """page_no из provenance; у Office часто None — цитата через section_path."""
    for p in getattr(item, "prov", None) or []:
        page_no = getattr(p, "page_no", None)
        if page_no is not None:
            return int(page_no)
    return None


def _label_of(item: Any) -> str:
    raw = getattr(item, "label", None)
    if raw is None:
        return ""
    value = getattr(raw, "value", None)
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    text = str(raw).strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _is_table(item: Any, label: str) -> bool:
    return type(item).__name__ == "TableItem" or label == "table"


def _is_image(item: Any, label: str) -> bool:
    name = type(item).__name__
    return name in {"PictureItem", "ImageRef"} or label in {"picture", "image"}


def _is_heading(item: Any, label: str) -> bool:
    return type(item).__name__ == "SectionHeaderItem" or label in {
        "section_header",
        "title",
    }


def _enforce_block_ids(
    block: ContentBlock,
    *,
    expected_index: int,
    branch: str,
    strict: bool,
    warnings: list[str],
) -> ContentBlock:
    """Инвариант: block_index == expected и block_id == make_block_id(index)."""
    expected_id = make_block_id(expected_index)
    bad_index = block.block_index != expected_index
    bad_id = block.block_id != expected_id
    if not bad_index and not bad_id:
        return block

    detail = (
        f"{branch} hook returned block_index={block.block_index!r} "
        f"block_id={block.block_id!r}; expected index={expected_index} id={expected_id}"
    )
    if strict:
        raise HookContractError(detail, branch=branch, block_index=expected_index)

    warnings.append(f"{detail}; normalized")
    return block.model_copy(update={"block_index": expected_index, "block_id": expected_id})


def extract_from_docling(
    doc: DoclingDocumentLike | Any,
    *,
    table_parser: TableParser | None = None,
    image_parser: ImageParser | None = None,
    strict: bool = False,
) -> tuple[str | None, list[ContentBlock], list[str]]:
    """
    Порядок чтения Docling → (title, blocks, warnings).

    Роутинг:
      heading/text → TextBlock
      table        → TableParser (структура rows)
      image        → ImageParser (мета + bytes handoff)

    Сбои hook → warning (+ skip блока) или HookExtractError при strict=True.
    """
    # `is None` — не `or`, чтобы не съесть валидный falsey-hook.
    if table_parser is None:
        table_parser = DEFAULT_TABLE_PARSER
    if image_parser is None:
        image_parser = DEFAULT_IMAGE_PARSER

    title: str | None = None
    blocks: list[ContentBlock] = []
    section_stack: list[tuple[int, str]] = []
    warnings: list[str] = []
    index = 0

    for item, _level in doc.iterate_items():
        label = _label_of(item)
        page = _page_from_prov(item)
        path = [t for _, t in section_stack]

        # --- table branch ---
        if _is_table(item, label):
            hook_notes: list[str] = []
            try:
                table_block = parse_table_block(
                    block_index=index,
                    page=page,
                    section_path=path,
                    raw_item=item,
                    doc=doc,
                    parser=table_parser,
                    warnings_out=hook_notes,
                )
            except Exception as exc:
                msg = (
                    f"table at index {index}: extract failed "
                    f"({type(exc).__name__}: {exc})"
                )
                if strict:
                    raise HookExtractError(
                        msg, branch="table", block_index=index
                    ) from exc
                warnings.append(f"{msg}; skipped")
                continue

            for note in hook_notes:
                warnings.append(f"table b{index:04d}: {note}")
            table_block = _enforce_block_ids(
                table_block,
                expected_index=index,
                branch="table",
                strict=strict,
                warnings=warnings,
            )
            assert isinstance(table_block, TableBlock)
            if not table_block.rows:
                warnings.append(
                    f"table {table_block.block_id}: empty rows after TableParser"
                )
            blocks.append(table_block)
            index += 1
            continue

        # --- image branch ---
        if _is_image(item, label):
            # Bytes handoff ДО hook: Docling/temp могут исчезнуть после parse.
            image_bytes, _meta, load_notes = load_image_bytes(item, doc)
            for note in load_notes:
                warnings.append(f"image b{index:04d}: {note}")

            hook_notes = []
            try:
                image_block = parse_image_block(
                    block_index=index,
                    page=page,
                    section_path=path,
                    raw_item=item,
                    doc=doc,
                    parser=image_parser,
                    image_bytes=image_bytes,
                    warnings_out=hook_notes,
                )
            except Exception as exc:
                msg = (
                    f"image at index {index}: extract failed "
                    f"({type(exc).__name__}: {exc})"
                )
                if strict:
                    raise HookExtractError(
                        msg, branch="image", block_index=index
                    ) from exc
                warnings.append(f"{msg}; skipped")
                continue

            for note in hook_notes:
                warnings.append(f"image b{index:04d}: {note}")
            image_block = _enforce_block_ids(
                image_block,
                expected_index=index,
                branch="image",
                strict=strict,
                warnings=warnings,
            )
            assert isinstance(image_block, ImageBlock)
            if image_block.vision_status == "skipped":
                warnings.append(
                    f"image {image_block.block_id}: no pixel bytes (vision_status=skipped)"
                )
            blocks.append(image_block)
            index += 1
            continue

        # --- text / heading ---
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue

        if _is_heading(item, label):
            hdr_level = int(getattr(item, "level", 1) or 1)
            if title is None and (label == "title" or not section_stack):
                title = text
            while section_stack and section_stack[-1][0] >= hdr_level:
                section_stack.pop()
            section_stack.append((hdr_level, text))
            path = [t for _, t in section_stack]
            blocks.append(
                TextBlock(
                    block_index=index,
                    block_id=make_block_id(index),
                    text=text,
                    page=page,
                    section_path=path,
                    label=label or "section_header",
                    heading_level=hdr_level,
                )
            )
            index += 1
            continue

        item_label = label or "text"
        if "list" in item_label:
            item_label = "list_item"
        blocks.append(
            TextBlock(
                block_index=index,
                block_id=make_block_id(index),
                text=text,
                page=page,
                section_path=path,
                label=item_label,
                heading_level=None,
            )
        )
        index += 1

    return title, blocks, warnings
