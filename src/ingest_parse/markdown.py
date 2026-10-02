"""DoclingDocument → Markdown."""

from __future__ import annotations

import re
from typing import Any

from ingest_parse.docling_text import (
    collect_inline,
    is_bold,
    label_of,
    link_markdown,
    list_depth,
    parent_of,
    unlatex_text,
)
from ingest_parse.headings import caption_kind, heading_level, section_number, split_glued_heading
from ingest_parse.media import MediaWriter, image_markdown
from ingest_parse.shapes import TOKEN, Shape
from ingest_parse.tables import table_to_markdown

_BARE_CAPTION = re.compile(r"(таблица|табл\.?|рисунок|рис\.?)\s*(?:[а-яa-z]\.)?\d+(?:\.\d+)*\.?", re.IGNORECASE)
_SCHEME_CAPTION = re.compile(r"схема\s+\S.{0,78}[^.]", re.IGNORECASE)  # «Схема БД», «Схема алгоритма»


def _caption_kind(text: str) -> str | None:
    return caption_kind(text) or ("figure" if _SCHEME_CAPTION.fullmatch(text) else None)


def _shape_markdown(shape: Shape, caption: str | None) -> str:
    alt = f"{shape.kind} (стр. {shape.page})" + (f" – {caption}" if caption else "")
    return image_markdown(alt, shape.link or "")


def _picture_alt(parts: list[tuple[str, Any]], i: int) -> str:
    """Подпись к таблице обычно над картинкой, к рисунку — под ней."""
    caption_kind = _caption_kind

    def block(j: int) -> str:
        return parts[j][1] if 0 <= j < len(parts) and parts[j][0] == "block" else ""

    def caption(number: str, title: str) -> str:
        # ГОСТ: «Таблица 4» отдельной строкой, название — следующей
        plain = title and not title.startswith(("#", "|", "!", "$", "- ", "1. "))
        if _BARE_CAPTION.fullmatch(number) and plain and len(title) <= 200 and not caption_kind(title):
            return f"{number} – {title}"
        return number

    prev, nxt = block(i - 1), block(i + 1)
    if prev and caption_kind(block(i - 2)) == "table" and not caption_kind(prev):
        return caption(block(i - 2), prev)
    if caption_kind(prev) == "table":
        return prev
    if caption_kind(nxt):
        return caption(nxt, block(i + 2))
    if caption_kind(prev):
        return prev
    return "image"


def docling_to_markdown(
    doc: Any, media: MediaWriter | None = None, shapes: list[Shape] | None = None
) -> str:
    """shapes — фигуры из shapes.mark_shapes: их метки в тексте заменяются ссылками на page-NNN.png."""
    parts: list[tuple[str, Any]] = []  # (kind, markdown | индекс фигуры | PictureItem)
    # Уже выведенные поддеревья (склеенные абзацы, таблицы); iterate_items идёт в pre-order — родитель раньше детей
    absorbed: set[str] = set()
    numbers: set[str] = set()  # номера уже найденных заголовков: "1", "4.1"
    pending: list[str] = []  # фигуры абзаца встают сразу после него

    for item, _level in doc.iterate_items(with_groups=True):
        parts.extend(("shape", index) for index in pending)
        pending = []
        parent_ref = getattr(item, "parent", None)
        if parent_ref is not None and parent_ref.cref in absorbed:
            absorbed.add(item.self_ref)
            continue

        label = label_of(item)
        if label == "table":
            absorbed.add(item.self_ref)
            parts.extend(("block", block) for block in table_to_markdown(item, doc))
            continue
        if label == "picture":
            if media is not None:
                parts.append(("picture", item))  # файл пишется ниже, если это не рендер фигуры
            continue

        list_item = item
        if label == "inline":
            absorbed.add(item.self_ref)
            # заголовки распознаются по тексту без разметки: «**2.1.1** Тема. Текст…»
            text = collect_inline(item, doc, markup=False)
            rich = collect_inline(item, doc)
            label = "text"
            parent = parent_of(item, doc)
            if parent is not None and label_of(parent) == "list_item":
                label, list_item = "list_item", parent
        else:
            text = getattr(item, "text", None)
            rich = link_markdown(text.strip(), item) if isinstance(text, str) else ""
        if shapes is not None and isinstance(text, str) and TOKEN.search(text):
            pending = TOKEN.findall(text)
            text, rich = TOKEN.sub("", text), TOKEN.sub("", rich).strip()
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()

        heading: int | None = None
        if label in ("title", "section_header") and caption_kind(text):
            heading = None  # «Рисунок 3 – Схема» стилем «Заголовок 2»
        elif label == "title":
            heading = 1
        elif label == "section_header":
            heading = min(int(getattr(item, "level", 1) or 1), 6)
        elif label in ("text", "list_item"):
            heading = heading_level(
                text, bold=is_bold(item, doc), in_list=label == "list_item", known=numbers
            )
            if heading is None and label == "text":
                if glued := split_glued_heading(text, known=numbers):
                    level, title, body = glued
                    numbers.add(title.partition(" ")[0])
                    parts.append(("block", f"{'#' * level} {title}"))
                    parts.append(("block", body))
                    continue

        if heading is not None:
            if number := section_number(text):
                numbers.add(number)
            parts.append(("block", f"{'#' * heading} {text}"))
        elif label == "list_item":
            # 3 пробела: минимум для вложенности под "1." в CommonMark
            indent = "   " * (list_depth(list_item, doc) - 1)
            bullet = "1." if getattr(list_item, "enumerated", False) else "-"
            parts.append(("list", f"{indent}{bullet} {rich}"))
        elif label == "formula":
            parts.append(("block", f"$$\n{unlatex_text(text)}\n$$"))
        else:
            parts.append(("block", rich))

    parts.extend(("shape", index) for index in pending)
    shapes = shapes or []

    def shape_of(index: str) -> Shape | None:
        shape = shapes[int(index)] if int(index) < len(shapes) else None
        return shape if shape is not None and shape.link else None

    resolved: list[tuple[str, str]] = []
    last_shape_link = None
    for i, (kind, md) in enumerate(parts):
        if kind == "shape":
            shape = shape_of(md)
            # «палочки» одной схемы подряд → одна ссылка на страницу
            if shape is None or shape.link == last_shape_link:
                continue
            last_shape_link = shape.link
            caption = None if shape.kind == "Формула" else _picture_alt(parts, i)
            resolved.append(("block", _shape_markdown(shape, None if caption == "image" else caption)))
            continue
        last_shape_link = None
        if kind == "picture":
            # Docling сам рисует простые фигуры; сразу за таким рисунком — метка той же фигуры
            next_is_shape = i + 1 < len(parts) and parts[i + 1][0] == "shape"
            if not next_is_shape and media is not None and (link := media.save(md)):
                resolved.append(("block", image_markdown(_picture_alt(parts, i), link)))
        else:
            resolved.append((kind, md))

    out = ""
    prev_kind = None
    for kind, md in resolved:
        if out:
            out += "\n" if kind == prev_kind == "list" else "\n\n"
        out += md
        prev_kind = kind

    def inline_shape(m: re.Match[str]) -> str:  # фигура в ячейке таблицы
        shape = shape_of(m.group(1))
        lead = " " if m.group(0)[0].isspace() else ""
        return lead + _shape_markdown(shape, None) if shape else ""

    out = TOKEN.sub(inline_shape, out)
    return out + "\n" if out else ""
