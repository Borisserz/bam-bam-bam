"""Вид картинки по имени файла, подписи (alt) и размеру → режим промпта."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PIL import Image

PromptMode = Literal["describe_only", "extract_and_describe"]

_LOGO_SHORT_SIDE = 96


def _size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError):
        return None


def image_kind(path: Path, alt: str) -> str:
    """logo | photo | diagram | table_scan | text_scan | chart | equation_img | unknown."""
    head = alt.strip().lower()
    if head.startswith(("формула", "объект")):
        return "equation_img"
    if head.startswith("график"):
        return "chart"
    if path.stem.startswith("page-"):
        return "diagram"
    if path.stem.startswith("scan-"):
        return "text_scan"
    if head.startswith("таблица"):
        return "table_scan"
    if "схем" in head or "диаграмм" in head:
        return "diagram"
    if "график" in head:
        return "chart"
    if "формул" in head:
        return "equation_img"
    size = _size(path)
    if size is not None and min(size) < _LOGO_SHORT_SIDE:
        return "logo"
    if path.suffix.lower() in (".jpg", ".jpeg"):
        return "photo"
    return "unknown"


def prompt_mode(kind: str) -> PromptMode:
    return "describe_only" if kind in ("logo", "photo") else "extract_and_describe"
