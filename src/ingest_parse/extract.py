"""DoclingDocument → текстовые блоки."""

from __future__ import annotations

from typing import Any

from ingest_parse.models import TextBlock, make_block_id

# Docling срезает пробелы на краях run'ов
_NO_SPACE_BEFORE = tuple(",.;:!?)]}»…%")
_NO_SPACE_AFTER = tuple("([{«„")


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


_HEADING_LABELS = {"section_header", "title"}


def _join_runs(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if out and not part.startswith(_NO_SPACE_BEFORE) and not out.endswith(_NO_SPACE_AFTER):
            out += " "
        out += part
    return out


def _collect_inline(group: Any, doc: Any) -> str:
    """Абзац со смешанным форматированием → одна строка."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        for ref in getattr(node, "children", None) or []:
            child = ref.resolve(doc)
            text = getattr(child, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
            walk(child)

    walk(group)
    return _join_runs(parts)


def _parent_label(item: Any, doc: Any) -> str:
    parent = getattr(item, "parent", None)
    return _label_of(parent.resolve(doc)) if parent is not None else ""


def extract_from_docling(doc: Any) -> tuple[str | None, list[TextBlock]]:
    """→ (title, blocks)."""
    title: str | None = None
    blocks: list[TextBlock] = []
    section_stack: list[tuple[int, str]] = []
    # Куски уже склеенных абзацев; iterate_items идёт в pre-order — родитель раньше детей
    absorbed: set[str] = set()

    for item, _level in doc.iterate_items(with_groups=True):
        parent = getattr(item, "parent", None)
        if parent is not None and parent.cref in absorbed:
            absorbed.add(item.self_ref)
            continue
        label = _label_of(item)

        if label == "inline":
            absorbed.add(item.self_ref)
            text = _collect_inline(item, doc)
            label = "list_item" if _parent_label(item, doc) == "list_item" else "text"
        else:
            text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue

        heading_level: int | None = None
        if label in _HEADING_LABELS:
            heading_level = int(getattr(item, "level", 1) or 1)
            if title is None and (label == "title" or not section_stack):
                title = text
            while section_stack and section_stack[-1][0] >= heading_level:
                section_stack.pop()
            section_stack.append((heading_level, text))

        index = len(blocks)
        blocks.append(
            TextBlock(
                block_index=index,
                block_id=make_block_id(index),
                text=text,
                section_path=[t for _, t in section_stack],
                label=label,
                heading_level=heading_level,
            )
        )

    return title, blocks
