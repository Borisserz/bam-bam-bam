"""Общие помощники над элементами DoclingDocument: текст абзаца, жирность, формулы."""

from __future__ import annotations

import re
from typing import Any

from pylatexenc.latexencode import unicode_to_latex

# Docling срезает пробелы на краях run'ов
_NO_SPACE_BEFORE = tuple(",.;:!?)]}»…%")
_NO_SPACE_AFTER = tuple("([{«„")

# Docling кодирует буквы в формулах text-макросами pylatexenc: "Сумма" → \CYRS \cyru \cyrm \cyrm \cyra
_TEXT_MACROS: dict[str, str] = {"\\~": " "}  # \~ — неразрывный пробел
for _ch in [chr(c) for c in range(0x400, 0x460)] + list("…–—«»№·°"):
    _enc = unicode_to_latex(_ch).strip("{}")
    if re.fullmatch(r"\\[A-Za-z]+", _enc):
        _TEXT_MACROS[_enc] = _ch
_MACRO = "|".join(re.escape(m) for m in sorted(_TEXT_MACROS, key=len, reverse=True))
_MACRO_RUN = re.compile(rf"(?:(?:{_MACRO})(?![A-Za-z])\s*)+")
_TEXT_SYMBOL = re.compile(r" ?\\text\{\s*([^\sA-Za-z0-9\\{}])\s*\} ?")  # a \text{ · } b → a·b
_BRACE_PAD = re.compile(r"(?<!\\)\{ | (?<!\\)\}")


def unlatex_text(s: str) -> str:
    def repl(m: re.Match[str]) -> str:
        out = ""
        # pylatexenc обкладывает каждую букву пробелами: одиночный — между буквами, двойной — между словами
        for tok in re.split(r"(\s+)", m.group(0).strip()):
            out += (" " if len(tok) >= 2 else "") if tok.isspace() else _TEXT_MACROS[tok]
        trailing = len(m.group(0)) - len(m.group(0).rstrip())
        return out + (" " if trailing >= 2 else "")

    s = _TEXT_SYMBOL.sub(r"\1", _MACRO_RUN.sub(repl, s))
    s = re.sub(r" {2,}", " ", s)
    return _BRACE_PAD.sub(lambda m: m.group(0).strip(), s)


def label_of(item: Any) -> str:
    raw = getattr(item, "label", None)
    return str(getattr(raw, "value", raw) or "").lower()


def parent_of(item: Any, doc: Any) -> Any | None:
    ref = getattr(item, "parent", None)
    return ref.resolve(doc) if ref is not None else None


def join_runs(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if out and not part.startswith(_NO_SPACE_BEFORE) and not out.endswith(_NO_SPACE_AFTER):
            out += " "
        out += part
    return out


def link_markdown(text: str, item: Any) -> str:
    url = getattr(item, "hyperlink", None)
    return f"[{text}]({url})" if url else text


def collect_inline(group: Any, doc: Any, *, markup: bool = True) -> str:
    """Абзац со смешанным форматированием → одна строка: **жирный**, *курсив*, [ссылка](url), $формула$.

    markup=False — только текст и формулы (для распознавания заголовков).
    """
    runs: list[Any] = []

    def walk(node: Any) -> None:
        for ref in getattr(node, "children", None) or []:
            child = ref.resolve(doc)
            text = getattr(child, "text", None)
            if isinstance(text, str) and text.strip():
                runs.append(child)
            walk(child)

    walk(group)
    plain = [r for r in runs if label_of(r) != "formula"]

    def styled(attr: str) -> set[int]:
        marked = {id(r) for r in plain if getattr(getattr(r, "formatting", None), attr, False)}
        return set() if len(marked) == len(plain) else marked  # весь абзац жирный — это не акцент

    bold, italic = styled("bold"), styled("italic")
    parts: list[str] = []
    for run in runs:
        text = run.text.strip()
        if label_of(run) == "formula":
            parts.append(f"${unlatex_text(text).strip()}$")
            continue
        if not markup:
            parts.append(text)
            continue
        text = link_markdown(text, run)
        mark = "*" * ((id(run) in bold) * 2 + (id(run) in italic))
        if mark and any(c.isalnum() for c in text):
            core = text.rstrip(".,;:!?")
            text = f"{mark}{core}{mark}{text[len(core):]}"
        parts.append(text)
    return join_runs(parts)


def is_bold(item: Any, doc: Any) -> bool:
    fmt = getattr(item, "formatting", None)
    if fmt is not None:
        return bool(fmt.bold)
    runs = [
        child
        for ref in getattr(item, "children", None) or []
        if (child := ref.resolve(doc)) is not None
        and str(getattr(child, "text", "") or "").strip()
        and label_of(child) != "formula"
    ]
    return bool(runs) and all(is_bold(r, doc) for r in runs)


def list_depth(item: Any, doc: Any) -> int:
    depth = 0
    node = parent_of(item, doc)
    while node is not None:
        if label_of(node) == "list":
            depth += 1
        node = parent_of(node, doc)
    return max(depth, 1)
