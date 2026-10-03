"""Текстовый слой PDF (pypdfium2) поверх элементов Docling: жирный / курсив, ссылки, тире, границы абзацев.

Docling PDF отдаёт только текст: без шрифтов, ссылок, а «–» превращает в «-». Здесь для каждого текстового
элемента берутся символы pypdfium2 внутри его рамки, слова сопоставляются с текстом Docling, и обходчику
отдаётся готовая rich-строка. Склеенные моделью раскладки абзацы режутся по строке, которая кончается
точкой и заметно не доходит до правого края.
"""

from __future__ import annotations

import ctypes
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from ingest_parse.docling_text import label_of

_BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.IGNORECASE)
_ITALIC = re.compile(r"italic|oblique", re.IGNORECASE)
_FORCE_BOLD, _ITALIC_FLAG = 0x40000, 0x40  # флаги шрифта PDF
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-", "\u00ad": None})
_PARAGRAPH_END = tuple(".!?:;")
_SHORT_LINE = 0.12  # строка короче блока на столько ширины — последняя строка абзаца
_MIN_MATCHED = 0.6  # меньше слов совпало — рамка не та, элемент не трогаем
_STYLED_LABELS = {"text", "list_item", "caption", "section_header", "title", "footnote"}
_NO_SPACE_BEFORE = tuple(",.;:!?)]}»…%")


@dataclass(frozen=True)
class Styled:
    text: str  # без разметки, с тире из PDF — для заголовков и подписей
    rich: str  # **жирный**, *курсив*, [ссылка](url)
    bold: bool  # весь абзац жирный — признак заголовка, а не акцент


class _Char(NamedTuple):
    ch: str
    box: tuple[float, float, float, float]  # left, bottom, right, top
    bold: bool
    italic: bool
    url: str | None


class _Word(NamedTuple):
    text: str
    bold: bool | None  # None — в слове нет букв и цифр
    italic: bool | None
    url: str | None
    paragraph_end: bool


def _links(pdf: pdfium.PdfDocument, page: pdfium.PdfPage) -> list[tuple[tuple[float, float, float, float], str]]:
    out = []
    pos, link = ctypes.c_int(0), pdfium_c.FPDF_LINK()
    while pdfium_c.FPDFLink_Enumerate(page.raw, ctypes.byref(pos), ctypes.byref(link)):
        action = pdfium_c.FPDFLink_GetAction(link)
        if not action or pdfium_c.FPDFAction_GetType(action) != pdfium_c.PDFACTION_URI:
            continue
        size = pdfium_c.FPDFAction_GetURIPath(pdf.raw, action, None, 0)
        buf = ctypes.create_string_buffer(size)
        pdfium_c.FPDFAction_GetURIPath(pdf.raw, action, buf, size)
        rect = pdfium_c.FS_RECTF()
        if pdfium_c.FPDFLink_GetAnnotRect(link, ctypes.byref(rect)):
            box = (min(rect.left, rect.right), min(rect.bottom, rect.top), max(rect.left, rect.right), max(rect.bottom, rect.top))
            out.append((box, buf.value.decode("utf-8", "replace")))
    return out


def _page_chars(pdf: pdfium.PdfDocument, index: int) -> tuple[float, list[_Char]]:
    page = pdf[index]
    height = page.get_height()
    textpage = page.get_textpage()
    try:
        links = _links(pdf, page)
        name, flags = ctypes.create_string_buffer(256), ctypes.c_int()
        chars = []
        for i in range(textpage.count_chars()):
            ch = chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, i))
            box = textpage.get_charbox(i)
            pdfium_c.FPDFText_GetFontInfo(textpage.raw, i, name, 256, ctypes.byref(flags))
            font = name.value.decode("latin-1")
            x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            url = next((u for (l, b, r, t), u in links if l <= x <= r and b <= y <= t), None)
            chars.append(
                _Char(
                    ch,
                    box,
                    bool(_BOLD.search(font) or flags.value & _FORCE_BOLD),
                    bool(_ITALIC.search(font) or flags.value & _ITALIC_FLAG),
                    url,
                )
            )
        return height, chars
    finally:
        textpage.close()
        page.close()


def _words(chars: list[_Char], bbox: tuple[float, float, float, float]) -> list[_Word]:
    """Слова внутри рамки элемента; paragraph_end — после слова кончается абзац."""
    left, bottom, right, top = bbox
    pad = 1.5

    def inside(c: _Char) -> bool:
        x, y = (c.box[0] + c.box[2]) / 2, (c.box[1] + c.box[3]) / 2
        return c.box[2] > c.box[0] and left - pad <= x <= right + pad and bottom - pad <= y <= top + pad

    picked = [i for i, c in enumerate(chars) if inside(c)]
    if not picked:
        return []
    lines: list[list[list[_Char]]] = [[[]]]  # строки → слова → символы
    for i in range(picked[0], picked[-1] + 1):
        c = chars[i]
        if c.ch in "\r\n":
            if c.ch == "\n" and any(lines[-1]):
                lines.append([[]])
            continue
        if c.ch.isspace() or not inside(c):
            if lines[-1][-1]:
                lines[-1].append([])
            continue
        lines[-1][-1].append(c)
    lines = [[w for w in line if w] for line in lines]
    lines = [line for line in lines if line]

    edges = [max(c.box[2] for w in line for c in w) for line in lines]
    starts = [min(c.box[0] for w in line for c in w) for line in lines]
    bottoms = [sorted(c.box[1] for w in line for c in w)[len([c for w in line for c in w]) // 2] for line in lines]
    heights = sorted(c.box[3] - c.box[1] for line in lines for w in line for c in w)
    em = heights[len(heights) // 2] if heights else 0.0
    pitches = sorted(bottoms[n] - bottoms[n + 1] for n in range(len(lines) - 1))
    pitch = pitches[len(pitches) // 2] if pitches else 0.0
    block_right, width, left_edge = max(edges), max(edges) - min(starts), min(starts)

    def paragraph_break(n: int) -> bool:
        """После строки n начинается новый абзац: короткая строка, красная строка или увеличенный интервал."""
        short = width > 0 and edges[n] < block_right - _SHORT_LINE * width
        indent = em > 0 and starts[n + 1] > left_edge + em and starts[n] <= starts[n + 1] - em
        gap = len(pitches) >= 2 and pitch > 0 and bottoms[n] - bottoms[n + 1] > 1.4 * pitch
        return short or indent or gap

    out = []
    for n, line in enumerate(lines):
        last_line = n == len(lines) - 1
        breaks = not last_line and paragraph_break(n)
        for k, word in enumerate(line):
            text = "".join(c.ch for c in word)
            letters = [c for c in word if c.ch.isalnum()]
            ends = k == len(line) - 1 and breaks and text.endswith(_PARAGRAPH_END)
            out.append(
                _Word(
                    text,
                    all(c.bold for c in letters) if letters else None,
                    all(c.italic for c in letters) if letters else None,
                    next((c.url for c in word if c.url), None),
                    ends,
                )
            )
    return out


def _key(token: str) -> str:
    return token.translate(_DASHES)


def _render(tokens: list[tuple[str, bool | None, bool | None, str | None]], whole_bold: bool, whole_italic: bool) -> str:
    """Подряд идущие слова одного стиля → один **…** / *…* / [..](url); знаки препинания — снаружи."""
    runs: list[tuple[list[str], tuple[bool, bool, str | None]]] = []
    for text, bold, italic, url in tokens:
        style = (bool(bold) and not whole_bold, bool(italic) and not whole_italic, url)
        if bold is None and runs and runs[-1][1][2] == url:
            style = runs[-1][1]  # «–», «(1)» без букв — к соседнему отрезку
        if runs and runs[-1][1] == style:
            runs[-1][0].append(text)
        else:
            runs.append(([text], style))

    parts = []
    for words, (bold, italic, url) in runs:
        text = " ".join(words)
        core = text.rstrip(".,;:!?") or text
        tail = text[len(core) :]
        if url:
            core = f"[{core}]({url})"
        mark = "*" * (bold * 2 + italic)
        parts.append(f"{mark}{core}{mark}{tail}" if mark else f"{core}{tail}")
    out = ""
    for part in parts:
        out += ("" if not out or part.startswith(_NO_SPACE_BEFORE) else " ") + part
    return out


def _style(text: str, words: list[_Word], split: bool) -> list[Styled] | None:
    tokens = text.split()
    if not tokens or not words:
        return None
    matcher = difflib.SequenceMatcher(None, [_key(t) for t in tokens], [_key(w.text) for w in words], autojunk=False)
    mapped: dict[int, _Word] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            mapped[block.a + k] = words[block.b + k]
    if len(mapped) < _MIN_MATCHED * len(tokens):
        return None

    letters = [mapped[i] for i in mapped if mapped[i].bold is not None]
    whole_bold = bool(letters) and all(w.bold for w in letters) and len(mapped) == len(tokens)
    whole_italic = bool(letters) and all(w.italic for w in letters) and len(mapped) == len(tokens)
    paragraphs: list[list[tuple[str, bool | None, bool | None, str | None]]] = [[]]
    for i, token in enumerate(tokens):
        word = mapped.get(i)
        if word is None:
            paragraphs[-1].append((token, False, False, None))
            continue
        paragraphs[-1].append((word.text, word.bold, word.italic, word.url))
        if split and word.paragraph_end and i < len(tokens) - 1:
            paragraphs.append([])
    return [
        Styled(" ".join(t[0] for t in p), _render(p, whole_bold, whole_italic), whole_bold)
        for p in paragraphs
        if p
    ]


def _in_table(item: Any, doc: Any) -> bool:
    ref = getattr(item, "parent", None)
    while ref is not None:
        node = ref.resolve(doc)
        if label_of(node) in ("table", "picture", "chart"):
            return True
        ref = getattr(node, "parent", None)
    return False


def apply_text_layer(doc: Any, path: str | Path) -> dict[str, Styled]:
    """self_ref элемента → Styled. Склеенные абзацы разрезаются: в doc добавляются элементы-соседи."""
    from docling_core.types.doc import DocItemLabel

    pdf = pdfium.PdfDocument(str(path))
    pages: dict[int, tuple[float, list[_Char]]] = {}
    overrides: dict[str, Styled] = {}
    try:
        items = [item for item, _ in doc.iterate_items()]
        for item in items:
            label = label_of(item)
            prov = getattr(item, "prov", None)
            text = getattr(item, "text", None)
            if label not in _STYLED_LABELS or not prov or not isinstance(text, str) or _in_table(item, doc):
                continue
            words: list[_Word] = []
            for p in prov:
                if p.page_no not in pages:
                    pages[p.page_no] = _page_chars(pdf, p.page_no - 1)
                height, chars = pages[p.page_no]
                box = p.bbox.to_bottom_left_origin(page_height=height)
                words += _words(chars, (box.l, box.b, box.r, box.t))
            styled = _style(text, words, split=label == "text")
            if not styled:
                continue
            overrides[item.self_ref] = styled[0]
            sibling = item
            for extra in styled[1:]:
                sibling = doc.insert_text(sibling, DocItemLabel.TEXT, extra.text, orig=extra.text, prov=prov[0])
                overrides[sibling.self_ref] = extra
    finally:
        pdf.close()
    return overrides
