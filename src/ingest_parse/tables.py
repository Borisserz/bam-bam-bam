"""Таблица Docling → Markdown: сетка (GFM), «поле — значение» (список) или разметка страницы (абзацы)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ingest_parse.docling_text import collect_inline, is_bold, label_of, unlatex_text

MAX_HEADER_ROWS = 3
MAX_SHORT_CELL = 40
_HEADER_STEMS = (
    "атрибут", "вариант", "действи", "значени", "единиц", "команд", "комментари", "критери",
    "метод", "наименовани", "названи", "назначени", "описани", "определени", "опци", "параметр",
    "показател", "поле", "пояснени", "пример", "примечани", "результат", "свойств", "содержани",
    "термин", "требовани", "тип", "формул", "функци", "характеристик", "что", "как", "зачем",
    "name", "value", "description", "type", "parameter", "option", "example", "field",
)
_NUMERIC = re.compile(r"[-+−–]?\d[\d\s.,]*%?")
_FORMULA = re.compile(r"\$[^$]+\$")


@dataclass(frozen=True)
class _Cell:
    lines: tuple[str, ...]
    bold: bool
    key: tuple[int, int]  # (строка, столбец) верхнего левого угла объединения
    h_cont: bool  # продолжение объединения по горизонтали
    v_cont: bool  # продолжение объединения по вертикали

    @property
    def text(self) -> str:
        return " ".join(self.lines)

    @property
    def empty(self) -> bool:
        return not self.lines


def _clean(line: str) -> str:
    line = _FORMULA.sub(lambda m: unlatex_text(m.group(0)), line)
    return re.sub(r"[ \t\u00a0]+", " ", line).strip()


def _walk_lines(node: Any, doc: Any, out: list[str]) -> None:
    for ref in getattr(node, "children", None) or []:
        item = ref.resolve(doc)
        label = label_of(item)
        if label in ("picture", "chart"):
            continue
        if label == "inline":
            out.append(collect_inline(item, doc))
            continue
        if label == "table":  # вложенная таблица → строка на строку, ячейки через «; »
            for row in item.data.grid:
                cells = list(dict.fromkeys(c.text.strip() for c in row if c.text.strip()))
                out.append("; ".join(cells))
            continue
        text = getattr(item, "text", None)
        if label == "formula" and text:
            out.append(f"${text.strip()}$")
            continue
        prefix = "- " if label == "list_item" else ""
        if isinstance(text, str) and text.strip():
            out.append(prefix + text.strip())
        elif prefix:  # текст пункта лежит во вложенном inline
            sub: list[str] = []
            _walk_lines(item, doc, sub)
            out.extend([prefix + sub[0], *sub[1:]] if sub else [])
            continue
        _walk_lines(item, doc, out)


def _list_texts(node: Any, doc: Any, out: set[str]) -> None:
    for ref in getattr(node, "children", None) or []:
        item = ref.resolve(doc)
        if label_of(item) == "list_item":
            sub: list[str] = []
            if getattr(item, "text", ""):
                sub.append(item.text)
            else:
                _walk_lines(item, doc, sub)
            out.update(_clean(s) for s in sub)
        _list_texts(item, doc, out)


def _nested_rows(node: Any, doc: Any, out: list[str]) -> None:
    for ref in getattr(node, "children", None) or []:
        item = ref.resolve(doc)
        if label_of(item) == "table":
            for row in item.data.grid:
                cells = list(dict.fromkeys(_clean(c.text) for c in row if c.text.strip()))
                if cells:
                    out.append("; ".join(cells))
        else:
            _nested_rows(item, doc, out)


def _cell_lines(cell: Any, doc: Any) -> tuple[list[str], bool]:
    # Абзацы ячейки разделены «\n» только в cell.text; дети RichTableCell — это прогоны
    # (runs) с разным форматированием внутри абзаца, по ним строки не делятся.
    lines = [ln for ln in map(_clean, (cell.text or "").splitlines()) if ln]
    ref = getattr(cell, "ref", None)
    if ref is None:
        return lines, False
    group = ref.resolve(doc)
    if not lines:
        walked: list[str] = []
        _walk_lines(group, doc, walked)
        lines = [ln for ln in map(_clean, walked) if ln]
    else:
        _nested_rows(group, doc, lines)  # вложенных таблиц в cell.text нет
    items: set[str] = set()
    _list_texts(group, doc, items)
    lines = [f"- {ln}" if ln in items else ln for ln in lines]
    return lines, is_bold(group, doc)


def _build_grid(table: Any, doc: Any) -> list[list[_Cell]]:
    cache: dict[tuple[int, int], tuple[list[str], bool]] = {}
    grid: list[list[_Cell]] = []
    for r, row in enumerate(table.data.grid):
        out_row = []
        for c, cell in enumerate(row):
            key = (cell.start_row_offset_idx, cell.start_col_offset_idx)
            if key not in cache:
                cache[key] = _cell_lines(cell, doc)
            lines, bold = cache[key]
            out_row.append(
                _Cell(tuple(lines), bold, key, h_cont=key[1] != c, v_cont=key[0] != r)
            )
        grid.append(out_row)
    return _drop_empty(grid)


def _drop_empty(grid: list[list[_Cell]]) -> list[list[_Cell]]:
    def own_empty(cell: _Cell) -> bool:
        return cell.empty or cell.h_cont

    grid = [row for row in grid if not all(cell.empty for cell in row)]
    if not grid:
        return []
    keep = [c for c in range(len(grid[0])) if not all(own_empty(row[c]) for row in grid)]
    return [[row[c] for c in keep] for row in grid]


def _origins(grid: list[list[_Cell]]) -> list[_Cell]:
    seen: dict[tuple[int, int], _Cell] = {}
    for row in grid:
        for cell in row:
            seen.setdefault(cell.key, cell)
    return list(seen.values())


def _is_numeric(text: str) -> bool:
    return bool(_NUMERIC.fullmatch(text.strip()))


def _full_width(row: list[_Cell]) -> bool:
    """Строка-раздел: одна ячейка, объединённая на всю ширину."""
    return len(row) > 1 and len({cell.key for cell in row}) == 1


def _short_row(row: list[_Cell]) -> bool:
    """Строка коротких однострочных ячеек без пропусков: «Север | Юг | Запад»."""
    return all(len(cell.lines) == 1 and len(cell.text) <= MAX_SHORT_CELL for cell in row)


def _is_signature(grid: list[list[_Cell]]) -> bool:
    """«Выполнил: | Проверил:», линии «______» под подпись."""
    if len(grid) > 4:
        return False
    if any("___" in cell.text for cell in _origins(grid)):
        return True
    labels = [cell.text for cell in grid[0] if not cell.empty and not cell.h_cont]
    return len(labels) >= 2 and all(t.endswith(":") for t in labels)


def _is_layout(grid: list[list[_Cell]]) -> bool:
    rows, cols = len(grid), len(grid[0])
    if cols == 1:
        return True
    if rows == 1:
        return not (cols >= 3 and _short_row(grid[0]))
    if _is_signature(grid):
        return True
    if cols >= 3 and all(not cell.empty for cell in grid[0]):
        return False  # шапка на месте, тело ещё не заполнено — это шаблон таблицы
    origins = _origins(grid)
    return sum(cell.empty for cell in origins) / len(origins) >= 0.5


def _is_key_value(grid: list[list[_Cell]]) -> bool:
    if len(grid[0]) != 2 or len(grid) < 2:
        return False
    rows = [row for row in grid if not _full_width(row)]
    if len(rows) < 2:
        return False
    if _header_by_bold(rows) or _header_by_words(rows[0]):
        return False
    left = [row[0].text for row in rows]
    right = [row[1].text for row in rows]
    filled = [t for t in left if t]
    if len(filled) < 0.8 * len(rows) or not filled:
        return False
    if max(map(len, filled)) > 80 or sum(map(len, filled)) / len(filled) > 40:
        return False
    if sum(map(_is_numeric, filled)) >= 0.5 * len(filled):  # «№ | Требование» — это сетка
        return False
    if sum(t.endswith(":") for t in right if t) >= 0.5 * max(1, sum(map(bool, right))):
        return False  # «Проверила: | Выполнил:» — подписи
    return len(set(filled)) >= 0.8 * len(filled)


def _header_by_words(row: list[_Cell]) -> bool:
    """«Опция меню | Назначение», «Тип данных | Допустимые значения» — шапка, а не пара."""

    def header_like(cell: _Cell) -> bool:
        text = cell.text.lower()
        return (
            0 < len(text) <= MAX_SHORT_CELL
            and not re.search(r"\d|:$", text)
            and any(word.startswith(_HEADER_STEMS) for word in re.findall(r"\w+", text))
        )

    return all(header_like(cell) for cell in row)


def _header_by_bold(grid: list[list[_Cell]]) -> bool:
    """Первая строка целиком жирная, а тело — нет."""
    first = [cell for cell in grid[0] if not cell.empty]
    if not first or not all(cell.bold for cell in first):
        return False
    body = [cell for row in grid[1:] for cell in row if not cell.empty]
    return bool(body) and not all(cell.bold for cell in body)


def _header_depth(grid: list[list[_Cell]]) -> int:
    rows = len(grid)
    first = [cell for cell in grid[0] if not cell.empty]
    if not _header_by_bold(grid) and first and sum(_is_numeric(c.text) for c in first) >= 0.5 * len(first):
        return 0  # первая строка — уже данные
    # «№» на две строки шапки тянет шапку вниз; группа «Выручка» над «2024 | 2025» требует
    # строку под собой. Пустой угол перекрёстной таблицы шапку не удлиняет.
    depth, changed = 1, True
    while changed and depth < rows:
        changed = False
        for r in range(depth):
            for c, cell in enumerate(grid[r]):
                if cell.empty or cell.v_cont or cell.h_cont:
                    continue
                end = max(rr for rr in range(rows) if grid[rr][c].key == cell.key) + 1
                wide = any(other.key == cell.key for other in grid[r][c + 1 :])
                need = end + 1 if wide and not _full_width(grid[r]) else end
                if need > depth:
                    depth, changed = need, True
    while depth < rows and all(cell.bold for cell in grid[depth] if not cell.empty) and any(
        not cell.empty for cell in grid[depth]
    ) and _header_by_bold(grid) and depth < MAX_HEADER_ROWS:
        depth += 1  # несколько жирных строк шапки без объединений
    return min(depth, MAX_HEADER_ROWS, rows - 1) if rows > 1 else 0


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _render_grid(grid: list[list[_Cell]]) -> str:
    depth = _header_depth(grid) if len(grid) > 1 else 1
    cols = len(grid[0])
    header = []
    for c in range(cols):
        parts: list[str] = []
        for r in range(depth):
            text = grid[r][c].text
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        header.append(" / ".join(parts))

    body: list[list[str]] = []
    for row in grid[depth:]:
        if _full_width(row):
            body.append([f"**{row[0].text}**"] + [""] * (cols - 1))
            continue
        cells = ["" if cell.h_cont else "<br>".join(cell.lines) for cell in row]
        if [cell.text for cell in row] == header:
            continue  # повтор шапки внутри таблицы
        body.append(cells)

    lines = [
        "| " + " | ".join(map(_md_cell, header)) + " |",
        "|" + "|".join(["---"] * cols) + "|",
    ]
    lines += ["| " + " | ".join(map(_md_cell, row)) + " |" for row in body]
    return "\n".join(lines)


def _render_key_value(grid: list[list[_Cell]]) -> list[str]:
    blocks: list[str] = []
    items: list[str] = []
    for row in grid:
        if _full_width(row):
            if items:
                blocks.append("\n".join(items))
                items = []
            blocks.append(f"**{row[0].text}**")
            continue
        key = row[0].text.rstrip(":").strip()
        value = "<br>".join(row[1].lines)
        if key and value:
            items.append(f"- **{key}:** {value}")
        elif key:
            items.append(f"- **{key}**")
        elif value:
            items.append(f"- {value}")
    if items:
        blocks.append("\n".join(items))
    return blocks


def _render_layout(grid: list[list[_Cell]]) -> list[str]:
    # Подписи — по столбцам: «Проверила: / Герман Ю. О.» и «Выполнил: / Студент …» не
    # перемешиваются. Остальная вёрстка (титульный лист) читается сверху вниз.
    if _is_signature(grid):
        cells = [row[c] for c in range(len(grid[0])) for row in grid]
    else:
        cells = [cell for row in grid for cell in row]
    seen: set[tuple[int, int]] = set()
    blocks: list[str] = []
    for cell in cells:
        if cell.key in seen or cell.empty:
            continue
        seen.add(cell.key)
        blocks.append("\n".join(cell.lines))
    return blocks


def table_to_markdown(table: Any, doc: Any) -> list[str]:
    """Блоки Markdown для одной таблицы (пустой список — нечего выводить)."""
    grid = _build_grid(table, doc)
    if not grid:
        return []
    caption: list[str] = []
    if len(grid) >= 3 and _full_width(grid[0]):  # «Вариант 1» на всю ширину над таблицей
        caption = [f"**{grid[0][0].text}**"]
        grid = _drop_empty(grid[1:])
    if _is_layout(grid):
        return caption + _render_layout(grid)
    if _is_key_value(grid):
        return caption + _render_key_value(grid)
    return caption + [_render_grid(grid)]
