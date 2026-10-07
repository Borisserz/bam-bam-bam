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
_MIN_TABLE_SHARE = 0.02
_MIN_WIDTH = 0.3  # доля ширины страницы
_WIDE = 0.7  # доля ширины самой широкой таблицы: реквизиты шапки уже товарного раздела
_LABEL_COLUMN = 0.03  # метки полей формы начинаются у левого края текста, доля ширины страницы
_LABEL_WIDTH = 0.15
_OVERLAP = 0.02  # доля высоты страницы: шапка и низ заходят на таблицу
_ROWS = 5  # горизонталей меньше — рамка реквизитов, не товарный раздел (шапка, номера, товар, итого)


@dataclass
class Zones:
    size: tuple[int, int]
    header: Box
    table: Box
    footer: Box | None
    lines: list[int] = field(default_factory=list)  # y горизонтальных линий таблицы, по возрастанию
    head_bottom: int = 0  # где кончается шапка таблицы (названия и номера столбцов)
    source: str = "bands"  # layout | heron | bands
    regions: list[tuple[str, float, Box]] = field(default_factory=list)  # разметка страницы, для отладки
    columns: list[int] = field(default_factory=list)  # x вертикальных линий сетки в строках данных

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


def find_columns(gray: np.ndarray, box: Box, top: int, bottom: int) -> list[int]:
    """x вертикалей, проходящих ≥ половину высоты строк данных (top..bottom); выцветший кусок линии не мешает.

    Только в пределах горизонталей строк (рамка листа рядом — не столбец); двойная линия — одна.
    """
    from ingest_parse.ttn.layout import _line_masks

    l, _, r, _ = box
    if bottom - top < 10 or r - l < 10:
        return []
    horiz, vert = _line_masks(gray)
    rows = np.flatnonzero(horiz[top:bottom, l:r].sum(axis=0) / 255 >= 2)
    if rows.size:
        l, r = max(0, l + int(rows[0]) - 4), l + int(rows[-1]) + 5
    on = vert[top:bottom, l:r].sum(axis=0) / 255 >= 0.5 * (bottom - top)
    xs = np.flatnonzero(on)
    if not xs.size:
        return []
    tol = max(3, int(0.01 * (r - l)))
    out = [l + int(np.mean(run)) for run in np.split(xs, np.flatnonzero(np.diff(xs) > tol) + 1)]
    if rows.size:  # бланк без боковых рамок: край строк — граница крайней ячейки (хвост линии за рамкой — нет)
        if out[0] - l > 3 * tol:
            out.insert(0, l + 4)
        if r - out[-1] > 3 * tol:
            out.append(r - 5)
    return out


def _rows_span(lines: list[int], head: int, box: Box) -> tuple[int, int]:
    data = [y for y in lines if y >= head - 2]
    return (data[0], data[-1]) if len(data) >= 2 else (head, box[3])


def with_grid(image: Image.Image, z: Zones) -> Image.Image:
    """Линии сетки в строках данных дорисованы чётко: на бледном скане VLM не путает столбцы."""
    if len(z.columns) < 2:
        return image
    top, bottom = _rows_span(z.lines, z.head_bottom, z.table)
    out = image.convert("L")
    draw = ImageDraw.Draw(out)
    for x in z.columns:
        draw.line((x, top, x, bottom), fill=0, width=2)
    for y in z.lines:
        if top <= y <= bottom:
            draw.line((z.columns[0], y, z.columns[-1], y), fill=0, width=2)
    return out


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
    columns = find_columns(gray, box, *_rows_span(lines, head, box))
    return Zones(image.size, (0, 0, image.width, box[1]), box, None, lines, head, "heron", columns=columns)


def items_table(tables: list[Box], size: tuple[int, int], gray: np.ndarray | None = None) -> Box | None:
    """Товарный раздел — верхняя из широких таблиц.

    Узкие таблицы выше — реквизиты шапки; широкие ниже (ТТН-1: погрузочно-разгрузочные операции) бывают крупнее.
    Рамка реквизитов с плашкой бывает широкой: в ней мало строк — пропускается, если есть таблица со строками.
    """
    w, h = size
    big = [b for b in tables if b[2] - b[0] >= _MIN_WIDTH * w and (b[2] - b[0]) * (b[3] - b[1]) >= _MIN_TABLE_SHARE * w * h]
    widest = max((b[2] - b[0] for b in big), default=0)  # бланк бывает в части листа — ширина от самой широкой
    wide = sorted((b for b in big if b[2] - b[0] >= _WIDE * widest), key=lambda b: b[1])
    if gray is not None and len(wide) > 1:
        ruled = [b for b in wide if len(find_lines(gray, b)) >= _ROWS]
        if ruled:
            wide = ruled
    return wide[0] if wide else None


def find_zones(image: Image.Image, regions: list[tuple[str, float, Box]] | None) -> Zones:
    w, h = image.size
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    overlap = int(_OVERLAP * h)
    found = items_table([box for label, _, box in regions or [] if label == "table"], (w, h), gray)
    if found:
        l, t, r, b = found
        pad = int(0.01 * w)
        table = (max(0, l - pad), max(0, t - overlap // 2), min(w, r + pad), min(h, b + overlap // 2))
        source = "layout"
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
    columns = find_columns(gray, table, *_rows_span(lines, head, table))
    return Zones((w, h), header, table, footer, lines, head, source, list(regions or []), columns)


def form_fields(regions: list[tuple[str, float, Box]], zone: Box, width: int) -> list[tuple[Box, Box]]:
    """Поля формы в zone: (метка, метка + значение). Метка — короткий текст в левой колонке бланка
    («Грузоотправитель», «Основание отпуска»), значение — текст правее на той же строке.
    """
    zl, zt, zr, zb = zone
    texts = [b for label, _, b in regions if label in ("text", "margin") and b[0] >= zl and b[1] >= zt and b[2] <= zr and b[3] <= zb]
    if not texts:
        return []
    left = min(b[0] for b in texts)
    labels = sorted((b for b in texts if b[0] <= left + _LABEL_COLUMN * width and b[2] - b[0] <= _LABEL_WIDTH * width), key=lambda b: b[1])
    out: list[tuple[Box, Box]] = []
    for lab in labels:
        h = lab[3] - lab[1]
        values = [b for b in texts if b[0] >= lab[2] - 5 and _same_line(lab, b) and _beside(lab, b, h)]
        if values:
            out.append((lab, (lab[0], min(b[1] for b in [lab, *values]), max(b[2] for b in values), max(b[3] for b in [lab, *values]))))
    return out


def _beside(label: Box, value: Box, h: int) -> bool:
    """Значение на строке метки (или в две строки, метка у нижней); плашка заголовка ниже «Серия» — нет."""
    cy = (value[1] + value[3]) / 2
    return value[3] - value[1] <= 3 * h and label[1] - h <= cy <= label[3] + h / 2


def _same_line(a: Box, b: Box) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    return overlap >= 0.5 * min(a[3] - a[1], b[3] - b[1])


def fields_image(image: Image.Image, pairs: list[tuple[Box, Box]], scale: float = 1.5) -> Image.Image | None:
    """Поля формы стопкой, крупнее: без печатей, логотипов и соседних колонок бланка."""
    if not pairs:
        return None
    pad = max(4, int(0.003 * image.height))
    parts = []
    for _, (l, t, r, b) in pairs:
        crop = image.crop((max(0, l - pad), max(0, t - pad), min(image.width, r + pad), min(image.height, b + pad))).convert("L")
        parts.append(crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.Resampling.LANCZOS))
    return stack(parts)


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
    image = with_grid(image, z)
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
