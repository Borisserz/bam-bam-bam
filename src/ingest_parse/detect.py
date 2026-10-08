"""Формат по расширению файла."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

SupportedFormat = Literal["txt", "doc", "docx", "docm", "rtf", "pdf", "image"]

_EXT_MAP: dict[str, SupportedFormat] = {
    ".txt": "txt",
    ".doc": "doc",
    ".docx": "docx",
    ".docm": "docm",
    ".rtf": "rtf",
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
    ".webp": "image",
    ".bmp": "image",
}


class UnsupportedFormatError(ValueError):
    pass


def detect_format(path: str | Path) -> SupportedFormat:
    suffix = Path(path).suffix.lower()
    fmt = _EXT_MAP.get(suffix)
    if fmt is None:
        raise UnsupportedFormatError(
            f"Unsupported format '{suffix or '<none>'}' for {path!s}. "
            "Supported: .txt, .doc, .docx, .docm, .rtf, .pdf, .png, .jpg, .jpeg, .tif, .tiff, .webp, .bmp"
        )
    return fmt
