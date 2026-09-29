"""Определение формата по расширению файла."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

SupportedFormat = Literal["txt", "doc", "docx"]

_EXT_MAP: dict[str, SupportedFormat] = {
    ".txt": "txt",
    ".doc": "doc",
    ".docx": "docx",
}


class UnsupportedFormatError(ValueError):
    """Формат вне whitelist stage-1 parse (txt/doc/docx)."""


def detect_format(path: str | Path) -> SupportedFormat:
    """Вернуть канонический формат по суффиксу; иначе понятная ошибка."""
    suffix = Path(path).suffix.lower()
    fmt = _EXT_MAP.get(suffix)
    if fmt is None:
        raise UnsupportedFormatError(
            f"Unsupported format '{suffix or '<none>'}' for {path!s}. "
            "Supported: .txt, .doc, .docx"
        )
    return fmt
