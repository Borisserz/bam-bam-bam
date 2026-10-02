"""Заголовки, набранные руками (без стиля Word «Заголовок N»)."""

from __future__ import annotations

import re
from collections.abc import Set

MAX_HEADING_LEN = 100

_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
_GLUED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$", re.DOTALL)
# конец первого предложения: точка/!/? + пробел + заглавная или цифра
_SENTENCE_END = re.compile(r"([.!?])\s+(?=[A-ZА-ЯЁ0-9])")
_CODE_WORD = re.compile(r"[a-z_][a-z0-9_]*")
# «Таблица 6», «Рис. 2.1 – Схема», «Рисунок А.1 — …»
_CAPTION = re.compile(
    r"(?P<kind>таблица|табл\.?|рисунок|рис\.?)\s*(?:[а-яa-z]\.)?\d+(?:\.\d+)*(?:[\s.–—:-].*)?",
    re.IGNORECASE | re.DOTALL,
)


def caption_kind(text: str) -> str | None:
    """«table» / «figure» для подписи к таблице или рисунку, иначе None."""
    m = _CAPTION.fullmatch(text.strip())
    if m is None:
        return None
    return "table" if m.group("kind").lower().startswith("табл") else "figure"


def section_number(text: str) -> str | None:
    m = _NUMBERED.match(text)
    return m.group(1) if m else None


def _parent_number(number: str) -> str:
    return number.rpartition(".")[0]


def _level(number: str) -> int:
    return min(len(number.split(".")), 6)


def heading_level(
    text: str, *, bold: bool, in_list: bool, known: Set[str] = frozenset()
) -> int | None:
    """Жирный / КАПС / продолжение нумерации; короткий, без точки в конце."""
    if len(text) > MAX_HEADING_LEN or "\t" in text or "\n" in text or text[-1] in ".,;:":
        return None
    if _CODE_WORD.fullmatch(text) or caption_kind(text):  # жирное `try`, «Таблица 6»
        return None
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return None
    caps = all(c.isupper() for c in letters)
    number = section_number(text)
    if in_list:
        return 1 if caps else None
    # "4.2 Текст" после заголовка "4 ..." — подраздел, даже без жирного
    continues = number is not None and _parent_number(number) in known
    if not (bold or continues or (number and caps)):
        return None
    return _level(number) if number else 1


def split_glued_heading(text: str, *, known: Set[str]) -> tuple[int, str, str] | None:
    """«2.1.1 Тема. Дальше текст…» → (3, "2.1.1 Тема", "Дальше текст…").

    Только если раздел-родитель (2.1) уже был заголовком.
    """
    m = _GLUED.match(text)
    if m is None or _parent_number(m.group(1)) not in known:
        return None
    number, rest = m.groups()
    end = _SENTENCE_END.search(rest)
    if end is None:
        return None
    title = rest[: end.start()] + ("" if end.group(1) == "." else end.group(1))
    body = rest[end.end() :].strip()
    heading = f"{number} {title.strip()}"
    if (
        not body
        or not title[:1].isupper()
        or len(heading) > MAX_HEADING_LEN
        or any(c in heading for c in "\n,:;")  # «Тип данных: в контуре…» — предложение
        or sum(c.isalpha() for c in title) < 3
    ):
        return None
    return _level(number), heading, body
