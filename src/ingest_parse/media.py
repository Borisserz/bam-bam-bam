"""Картинки документа → img-NNN.<ext>, страницы со схемами → page-NNN.png, сканы PDF → scan-NNN.png."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

_EXT = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/x-emf": "emf",
    "image/emf": "emf",
    "image/x-wmf": "wmf",
    "image/wmf": "wmf",
}
_DATA_URI = re.compile(r"data:(?P<mime>[\w/+.-]+)?(?:;[\w=-]+)*;base64,(?P<data>.*)", re.DOTALL)


class MediaWriter:
    """Пишет картинки в directory в порядке документа; в Markdown ссылка = link_base/имя."""

    def __init__(self, directory: str | Path, link_base: str) -> None:
        self.directory = Path(directory)
        self.link_base = link_base.rstrip("/")
        self._count = 0

    def save(self, picture: Any) -> str | None:
        """Ссылка на записанный файл; None — у картинки нет байтов (фигура, EMF без растра)."""
        image = getattr(picture, "image", None)
        m = _DATA_URI.fullmatch(str(getattr(image, "uri", "") or ""))
        if m is None:
            return None
        mime = (m.group("mime") or getattr(image, "mimetype", "") or "").lower()
        self._count += 1
        name = f"img-{self._count:03d}.{_EXT.get(mime, 'bin')}"
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / name).write_bytes(base64.b64decode(m.group("data")))
        return self._link(name)

    def save_page(self, image: Any, number: int) -> str:
        """PNG страницы PDF со схемой: page-NNN.png, NNN — номер страницы с 1."""
        name = f"page-{number:03d}.png"
        self.directory.mkdir(parents=True, exist_ok=True)
        image.save(self.directory / name)
        return self._link(name)

    def save_scan(self, image: Any, number: int) -> tuple[Path, str]:
        """PNG страницы-скана PDF: scan-NNN.png (не page-NNN — те для схем Word); путь к файлу и ссылка."""
        name = f"scan-{number:03d}.png"
        self.directory.mkdir(parents=True, exist_ok=True)
        image.save(self.directory / name)
        return self.directory / name, self._link(name)

    def _link(self, name: str) -> str:
        return f"{self.link_base}/{name}" if self.link_base else name


def image_markdown(alt: str, link: str) -> str:
    alt = " ".join(re.sub(r"(?<!\w)\*+|\*+(?!\w)", "", alt).split())  # *курсив* из подписи
    alt = re.sub(r"([\[\]\\])", r"\\\1", alt)
    dest = f"<{link}>" if re.search(r"[\s()<>]", link) else link
    return f"![{alt}]({dest})"
