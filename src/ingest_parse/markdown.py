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
from ingest_parse.numbering import NUM, finalize, list_bullet, marker_of, plain, render_scripts
from ingest_parse.shapes import TOKEN, Shape
from ingest_parse.tables import table_to_markdown

_BARE_CAPTION = re.compile(r"(таблица|табл\.?|рисунок|рис\.?)\s*(?:[а-яa-z]\.)?\d+(?:\.\d+)*\.?", re.IGNORECASE)
_SCHEME_CAPTION = re.compile(r"схема\s+\S.{0,78}[^.]", re.IGNORECASE)  # «Схема БД», «Схема алгоритма»


_DOCLING_NUMBER = re.compile(r"\s*\d+(?:\.\d+)*\s*")


def _drop_docling_number(text: str) -> str:
    """Нумерованный заголовок: Docling ставит свой счётчик «1.2 » перед меткой номера Word."""
    m = NUM.search(text)
    return text[m.start() :] if m and _DOCLING_NUMBER.fullmatch(text[: m.start()]) else text


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
    titled = prev and not prev.startswith(("|", "!", "- **"))  # между «Таблица N» и картинкой — название, а не сама таблица
    if titled and caption_kind(block(i - 2)) == "table" and not caption_kind(prev):
        return caption(block(i - 2), prev)
    if caption_kind(prev) == "table":
        return prev
    if caption_kind(nxt):
        return caption(nxt, block(i + 2))
    if caption_kind(prev):
        return prev
    return "image"


_PDF_BULLET = re.compile(r"(?:[•◦▪▫‣⁃●○■□►✓✔\uf0a7\uf0a8\uf0b7\uf076\uf0d8\uf0fc]\s*|[-–—]\s+)")
_PDF_NUMBER = re.compile(r"(\d+(?:\.\d+)*[.)]|[a-zа-яё][.)])\s+", re.IGNORECASE)
_SECTION_MARKER = re.compile(r"\d+(?:\.\d+)+")


def _page_of(item: Any) -> int | None:
    """Номер страницы PDF; у элементов из docx provenance нет."""
    prov = getattr(item, "prov", None)
    return prov[0].page_no if prov else None


def _pdf_list_marker(item: Any, text: str, rich: str) -> tuple[str | None, str, str]:
    """В PDF номер Docling кладёт в marker («2.»), а значок остаётся в тексте: «\uf0b7 Пункт»."""
    docling_marker = (getattr(item, "marker", "") or "").strip()
    if _PDF_NUMBER.fullmatch(docling_marker + " "):
        return docling_marker, text, rich
    if m := _PDF_BULLET.match(text):
        return "", text[m.end() :], _PDF_BULLET.sub("", rich, count=1)
    if m := _PDF_NUMBER.match(text):
        return m.group(1), text[m.end() :], _PDF_NUMBER.sub("", rich, count=1)
    return None, text, rich


_FURNITURE = {"page_header", "page_footer", "page-header", "page-footer"}
# Титульный лист в PDF: Docling размечает строки шапки как заголовки
_TITLE_BLOCK = re.compile(
    r"(министерство|учреждение образования|факультет|кафедра)\b.*|\(.*\)|[^a-zа-яё]*(УНИВЕРСИТЕТ|ИНСТИТУТ|АКАДЕМИЯ)[^a-zа-яё]*",
    re.IGNORECASE,
)


def _title_block(text: str) -> bool:
    m = _TITLE_BLOCK.fullmatch(text)
    return bool(m) and (m.group(2) is None or text.isupper() or not any(c.islower() for c in text))


def docling_to_markdown(
    doc: Any,
    media: MediaWriter | None = None,
    shapes: list[Shape] | None = None,
    skipped: dict[int, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> str:
    """shapes — фигуры из shapes.mark_shapes: их метки в тексте заменяются ссылками на page-NNN.png.

    skipped — страницы PDF без текстового слоя: их элементы выбрасываются, на их месте — комментарий.
    overrides — self_ref → pdf_text.Styled: текст, разметка и жирность из текстового слоя PDF.
    """
    parts: list[tuple[str, Any]] = []  # (kind, markdown | индекс фигуры | PictureItem)
    # Уже выведенные поддеревья (склеенные абзацы, таблицы); iterate_items идёт в pre-order — родитель раньше детей
    absorbed: set[str] = set()
    numbers: set[str] = set()  # номера уже найденных заголовков: "1", "4.1"
    pending: list[str] = []  # фигуры абзаца встают сразу после него
    placeholders = sorted((skipped or {}).items())

    def flush_placeholders(before: float) -> None:
        while placeholders and placeholders[0][0] < before:
            parts.append(("block", placeholders.pop(0)[1]))

    for item, _level in doc.iterate_items(with_groups=True):
        parts.extend(("shape", index) for index in pending)
        pending = []
        parent_ref = getattr(item, "parent", None)
        if parent_ref is not None and parent_ref.cref in absorbed:
            absorbed.add(item.self_ref)
            continue

        label = label_of(item)
        page = _page_of(item)
        if page is not None:
            flush_placeholders(page)
        if label in _FURNITURE or (skipped and page in skipped):
            absorbed.add(item.self_ref)
            continue
        if label == "table":
            absorbed.add(item.self_ref)
            parts.extend(("block", block) for block in table_to_markdown(item, doc))
            continue
        if label in ("picture", "chart"):
            if media is not None:
                parts.append(("picture", item))  # файл пишется ниже, если это не рендер фигуры
            continue

        list_item = item
        styled = overrides.get(item.self_ref) if overrides else None
        if label == "inline":
            absorbed.add(item.self_ref)
            # заголовки распознаются по тексту без разметки: «**2.1.1** Тема. Текст…»
            text = collect_inline(item, doc, markup=False)
            rich = collect_inline(item, doc)
            label = "text"
            parent = parent_of(item, doc)
            if parent is not None and label_of(parent) == "list_item":
                label, list_item = "list_item", parent
        elif styled is not None:
            text, rich = styled.text, styled.rich
        else:
            text = getattr(item, "text", None)
            if label == "formula" and not (text or "").strip() and page is not None:
                orig = getattr(item, "orig", None)  # PDF: LaTeX нет, остаётся текстовый слой
                text = " ".join(orig.split()) if isinstance(orig, str) else None
            rich = link_markdown(text.strip(), item) if isinstance(text, str) else ""
        if shapes is not None and isinstance(text, str) and TOKEN.search(text):
            pending = TOKEN.findall(text)
            text, rich = TOKEN.sub("", text), TOKEN.sub("", rich).strip()
        if not isinstance(text, str):
            continue
        marker, text = marker_of(_drop_docling_number(text))
        _, rich = marker_of(_drop_docling_number(rich))
        if marker is None and label == "list_item" and _page_of(list_item) is not None:
            number = (getattr(list_item, "marker", "") or "").strip()
            if _SECTION_MARKER.fullmatch(number):  # «1.1 Компоненты» с отступом Docling принял за пункт списка
                text, rich, label = f"{number} {text.strip()}", f"{number} {rich}", "text"
            else:
                marker, text, rich = _pdf_list_marker(list_item, text.strip(), rich)
        shown = render_scripts(text).strip()  # для заголовков: без **…**, но с <sup>/<sub>
        text, rich = plain(text).strip(), render_scripts(rich).strip()
        if not text:
            continue
        if marker and label not in ("list_item", "title", "section_header"):
            text, rich, shown = f"{marker} {text}", f"{marker} {rich}", f"{marker} {shown}"

        heading: int | None = None
        if label in ("title", "section_header") and caption_kind(text):
            heading = None  # «Рисунок 3 – Схема» стилем «Заголовок 2»
        elif label in ("title", "section_header") and page is not None and _title_block(text):
            heading = None
        elif label == "section_header":
            heading = min(int(getattr(item, "level", 1) or 1), 6)
            if _page_of(item) is not None and (number := section_number(text)):
                heading = min(number.count(".") + 1, 6)  # в PDF у всех заголовков level 1
        elif label == "title":
            heading = 1
        elif label in ("text", "list_item"):
            bold = is_bold(item, doc) or (styled is not None and styled.bold)
            heading = heading_level(text, bold=bold, in_list=label == "list_item", known=numbers)
            if heading is None and label == "text":
                if glued := split_glued_heading(text, known=numbers):
                    level, title, body = glued
                    numbers.add(title.partition(" ")[0])
                    parts.append(("block", f"{'#' * level} {title}"))
                    parts.append(("block", body))
                    continue

        if heading is not None:
            if marker and label in ("list_item", "title", "section_header"):
                text, shown = f"{marker} {text}", f"{marker} {shown}"
            if number := section_number(text):
                numbers.add(number)
            parts.append(("block", f"{'#' * heading} {shown}"))
        elif label == "list_item":
            # 3 пробела: минимум для вложенности под "1." в CommonMark
            indent = "   " * (list_depth(list_item, doc) - 1)
            if marker is not None:
                bullet = list_bullet(marker)
            else:
                bullet = "1." if getattr(list_item, "enumerated", False) else "-"
            parts.append(("list", f"{indent}{bullet} {rich}"))
        elif label == "formula":
            parts.append(("block", f"$$\n{unlatex_text(text)}\n$$"))
        else:
            parts.append(("block", rich))

    parts.extend(("shape", index) for index in pending)
    flush_placeholders(float("inf"))
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

    out = finalize(TOKEN.sub(inline_shape, out))
    return out + "\n" if out else ""
