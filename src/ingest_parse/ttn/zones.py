"""Зоны накладной на выровненной странице: шапка, товарный раздел (строки по линиям бланка), низ."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from ingest_parse.pdf_layout import Region

Box = tuple[int, int, int, int]  # l, t, r, b в пикселях

MAX_ROWS = 10  # строк товара в одном куске для VLM
_MIN_TABLE_SHARE = 0.08
_OVERLAP = 0.02  # доля высоты страницы: шапка и низ заходят на таблицу


@dataclass
class Zones:
    size: tuple[int, int]
    header: Box
    table: Box
    footer: Box | None
    lines: list[int] = field(default_factory=list)  # y горизонтальных линий таблицы, по возрастанию
    head_bottom: int = 0  # где кончается шапка таблицы (названия и номера столбцов)
    source: str = "bands"  # heron | bands
    regions: list[tuple[str, float, Box]] = field(default_factory=list)  # Heron, для отладки

    @property
    def rows(self) -> list[tuple[int, int]]:
        """Полосы строк данных (между линиями ниже шапки таблицы)."""
        ys = [y for y in self.lines if y >= self.head_bottom - 2]
        return [(a, b) for a, b in zip(ys, ys[1:], strict=False) if b - a > 4]


@dataclass
class Chunk:
    image: Image.Image  # шапка таблицы + строки
    rows: list[tuple[int, int]]  # полосы строк, попавшие в кусок (пусто — кусок без линий)


def heron_regions(image: Image.Image, dpi: float) -> list[tuple[str, float, Box]]:
    """Heron на картинке (через временный PDF); рамки — в пикселях картинки."""
    from ingest_parse.pdf_layout import detect_layout

    with tempfile.TemporaryDirectory(prefix="ingest-ttn-") as tmp:
        path = Path(tmp) / "page.pdf"
        image.convert("RGB").save(path, resolution=dpi)
        regions: list[Region] = detect_layout(path, [1]).get(1, [])
    k = dpi / 72
    return [(r.label, r.confidence, tuple(int(v * k) for v in r.box)) for r in regions]  # type: ignore[misc]


def find_lines(gray: np.ndarray, box: Box) -> list[int]:
    """y горизонтальных линий длиной ≥ 30 % ширины таблицы (абсолютные координаты)."""
    l, t, r, b = box
    crop = gray[t:b, l:r]
    if crop.size == 0:
        return []
    ink = cv2.adaptiveThreshold(crop, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 10)
    width = crop.shape[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, int(0.25 * width)), 1))
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
    profile = lines.sum(axis=1) / 255
    ys = np.flatnonzero(profile > 0.3 * width)
    out: list[int] = []
    group: list[int] = []
    for y in ys:
        if group and y - group[-1] > 3:
            out.append(t + int(np.mean(group)))
            group = []
        group.append(int(y))
    if group:
        out.append(t + int(np.mean(group)))
    return [y for y in out if _thin(gray, y, l, r)]


def _thin(gray: np.ndarray, y: int, l: int, r: int) -> bool:
    """Линия, а не край заливки/плашки: тёмный участок поперёк неё тонкий (медиана по столбцам)."""
    gap = max(8, int(0.004 * gray.shape[0]))
    top, bottom = max(0, y - gap - 1), min(gray.shape[0], y + gap + 2)
    dark = gray[top:bottom, l:r] < 128
    centre = y - top
    up = np.cumprod(dark[centre::-1], axis=0).sum(axis=0)
    down = np.cumprod(dark[centre:], axis=0).sum(axis=0)
    run = np.maximum(up + down - 1, 0)
    hit = run[run > 0]
    return hit.size == 0 or float(np.median(hit)) <= gap


def _head_bottom(lines: list[int]) -> int:
    """Шапка таблицы — высокие полосы сверху (+ узкая строка номеров столбцов под ними)."""
    bands = np.diff(lines)
    if len(bands) < 3:
        return lines[0] if lines else 0
    median = float(np.median(bands))
    i = 0
    while i < len(bands) - 1 and bands[i] > 1.3 * median:
        i += 1
    if i == 0 and bands[0] >= median:
        i = 1  # первая полоса — названия столбцов, даже если она невысокая
    if i < len(bands) - 1 and bands[i] < 0.75 * median:
        i += 1  # строка «1 2 3 …» под названиями столбцов
    return lines[i]


def _ruled_block(gray: np.ndarray) -> Box | None:
    """Без Heron: самая длинная серия частых горизонтальных линий на всей странице — это таблица."""
    h, w = gray.shape
    lines = find_lines(gray, (0, 0, w, h))
    runs: list[list[int]] = []
    for y in lines:
        if runs and y - runs[-1][-1] <= 0.12 * h:
            runs[-1].append(y)
        else:
            runs.append([y])
    best = max(runs, key=len, default=[])
    if len(best) < 4:
        return None
    pad = int(0.004 * h)
    return (0, max(0, best[0] - pad), w, min(h, best[-1] + pad))


def table_zones(image: Image.Image, box: Box) -> Zones:
    """Зоны одной таблицы (рамка от Heron): линии строк и конец шапки — для нарезки на куски."""
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    lines = find_lines(gray, box)
    if len(lines) >= 4:
        head = _head_bottom(lines)
    else:
        lines, head = [], box[1] + int(0.12 * (box[3] - box[1]))
    return Zones(image.size, (0, 0, image.width, box[1]), box, None, lines, head, "heron")


def find_zones(image: Image.Image, regions: list[tuple[str, float, Box]] | None) -> Zones:
    w, h = image.size
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    overlap = int(_OVERLAP * h)
    tables = [box for label, _, box in regions or [] if label == "table"]
    tables = [b for b in tables if (b[2] - b[0]) * (b[3] - b[1]) >= _MIN_TABLE_SHARE * w * h]
    if tables:
        l, t, r, b = max(tables, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        pad = int(0.01 * w)
        table = (max(0, l - pad), max(0, t - overlap // 2), min(w, r + pad), min(h, b + overlap // 2))
        source = "heron"
    else:
        table = _ruled_block(gray) or (0, int(0.30 * h), w, int(0.88 * h))
        source = "bands"
    lines = find_lines(gray, table)
    header = (0, 0, w, min(h, table[1] + overlap))
    footer = (0, max(0, table[3] - overlap), w, h) if table[3] < 0.97 * h else None
    if len(lines) >= 4:
        head = _head_bottom(lines)
    else:
        lines, head = [], table[1] + int(0.15 * (table[3] - table[1]))
    return Zones((w, h), header, table, footer, lines, head, source, list(regions or []))


def stack(parts: list[Image.Image]) -> Image.Image:
    width = max(p.width for p in parts)
    out = Image.new("L", (width, sum(p.height for p in parts) + 6 * (len(parts) - 1)), 255)
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height
        if y < out.height:
            ImageDraw.Draw(out).rectangle((0, y, width, y + 5), fill=160)  # видимая граница склейки
            y += 6
    return out


def table_head(image: Image.Image, z: Zones) -> Image.Image:
    l, t, r, _ = z.table
    return image.crop((l, t, r, max(t + 1, z.head_bottom + 2)))


def row_image(image: Image.Image, z: Zones, rows: list[tuple[int, int]]) -> Image.Image:
    """Шапка таблицы + указанные полосы строк."""
    l, _, r, _ = z.table
    top, bottom = rows[0][0], rows[-1][1]
    return stack([table_head(image, z), image.crop((l, max(0, top - 3), r, bottom + 3))])


def table_chunks(image: Image.Image, z: Zones, max_rows: int = MAX_ROWS) -> list[Chunk]:
    rows = z.rows
    if len(rows) >= 2:
        return [Chunk(row_image(image, z, rows[i : i + max_rows]), rows[i : i + max_rows]) for i in range(0, len(rows), max_rows)]
    # линий нет — куски по высоте с перекрытием; дубли строк убирает склейка
    l, _, r, b = z.table
    top = z.head_bottom
    step = max(1, int(0.28 * (b - top)))
    chunks = []
    y = top
    while y < b:
        end = min(b, y + step + int(0.06 * (b - top)))
        chunks.append(Chunk(stack([table_head(image, z), image.crop((l, y, r, end))]), []))
        y += step
    return chunks


def draw_zones(image: Image.Image, z: Zones) -> Image.Image:
    out = image.convert("RGB")
    draw = ImageDraw.Draw(out)
    for label, conf, box in z.regions:
        draw.rectangle(box, outline=(170, 170, 170), width=2)
        draw.text((box[0] + 3, box[1] + 3), f"{label} {conf:.2f}", fill=(120, 120, 120))
    draw.rectangle(z.header, outline=(0, 90, 255), width=6)
    draw.rectangle(z.table, outline=(0, 170, 0), width=6)
    if z.footer:
        draw.rectangle(z.footer, outline=(255, 140, 0), width=6)
    for y in z.lines:
        draw.line((z.table[0], y, z.table[2], y), fill=(0, 220, 120), width=2)
    draw.line((z.table[0], z.head_bottom, z.table[2], z.head_bottom), fill=(220, 0, 0), width=4)
    return out
