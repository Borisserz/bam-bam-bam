"""Картинка скана (jpeg/png/tiff/…) → одностраничный PDF, который уже умеет пайплайн.

Телефон кладёт поворот в EXIF, а не в пиксели. Без разворота страница ложится на бок.
dpi из файла часто врёт «72»: тогда страница раздувается вчетверо и буквы мылятся.
Если метки нет или она 72 — считаем короткую сторону листом A4.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

_A4_SHORT_IN = 210 / 25.4


def frame_dpi(image: Image.Image) -> float:
    tagged = 0.0
    dpi = image.info.get("dpi")
    if isinstance(dpi, tuple) and dpi and dpi[0]:
        tagged = float(dpi[0])
    short = min(image.size) or 1
    estimated = short / _A4_SHORT_IN
    if tagged > 96:
        return tagged
    return max(tagged, estimated)


def load_frames(path: Path) -> list[Image.Image]:
    image = Image.open(path)
    frames: list[Image.Image] = []
    for index in range(getattr(image, "n_frames", 1)):
        image.seek(index)
        frames.append(ImageOps.exif_transpose(image).convert("RGB"))
    return frames


def write_scan_pdf(path: Path, dest: Path) -> None:
    frames = load_frames(path)
    if not frames:
        raise ValueError(f"no pages in {path}")
    dpi = frame_dpi(Image.open(path))
    frames[0].save(dest, "PDF", save_all=True, append_images=frames[1:], resolution=dpi)
