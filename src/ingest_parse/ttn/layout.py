"""Разметка скана из трёх источников: Heron (целая страница + половины), dots, сетка линий бланка.

Heron быстрый и хорошо видит текст, но на сканах путает таблицы с картинками; dots сильнее в таблицах,
но иногда «схлопывает» страницу в одну картинку; линии бланка — точная рамка разлинованной таблицы.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Any

import cv2
import numpy as np
from PIL import Image

Box = tuple[int, int, int, int]  # l, t, r, b в пикселях

# минимальная уверенность сырого Heron по ролям; у Docling 0.5 для таблиц и картинок — на сканах много пропусков
HERON_MIN = {"table": 0.15, "text": 0.3, "margin": 0.3, "picture": 0.5}
TABLE_SURE = 0.5  # таблица Heron без подтверждения dots или линий
PICTURE_MAX = 0.4  # доля страницы: больше — это рамка скана или «вся страница картинкой», а не рисунок
_TILE = 0.55  # половины страницы с перекрытием 10 %
_TINY = 0.0001  # доля страницы: рамка меньше — точка, соринка
_DARK = 150  # чернила для добора: темнее этого (после выравнивания света бумага ~255)


@dataclass(frozen=True)
class Det:
    role: str  # table | text | margin | picture | container
    score: float
    box: Box
    source: str  # heron | dots | lines
    label: str = ""
    text: str = ""  # буквы блока, если источник их вернул (dots)


def role(label: str) -> str:
    key = label.lower().replace("-", "_")
    if key == "table":
        return "table"
    if key in ("picture", "chart"):
        return "picture"
    if key in ("page_header", "page_footer"):
        return "margin"
    if key in ("form", "key_value_region", "document_index"):
        return "container"
    return "text"


def area(b: Box) -> float:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _cross(a: Box, b: Box) -> float:
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def iou(a: Box, b: Box) -> float:
    inter = _cross(a, b)
    return inter / (area(a) + area(b) - inter) if inter else 0.0


def inside(a: Box, b: Box) -> float:
    """Доля площади a внутри b."""
    return _cross(a, b) / area(a) if area(a) else 0.0


# --- сетка линий ---


def _ink(gray: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 10)


def _line_masks(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Маски горизонтальных и вертикальных линий бланка (плашки и заливки отброшены)."""
    h, w = gray.shape
    ink = _ink(gray)
    horiz = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 40), 1)))
    vert = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, h // 100))))
    thick = max(6, int(0.004 * h))
    return _thin_only(horiz, thick, vertical=False), _thin_only(vert, thick, vertical=True)


def _columns(vert: np.ndarray, box: Box) -> int:
    l, t, r, b = box
    sub = vert[max(0, t) : b, max(0, l) : r]
    return _runs(sub.sum(axis=0) / 255 >= 0.3 * sub.shape[0]) if sub.size else 0


def _full_rows(horiz: np.ndarray, box: Box) -> int:
    """Горизонтали почти во всю ширину рамки; у блока подписей подчёркивания — по половине."""
    l, t, r, b = box
    sub = horiz[max(0, t) : b, max(0, l) : r]
    if not sub.size:
        return 0
    joined = cv2.dilate(sub, np.ones((3, 9), np.uint8))  # рваная линия скана — одна линия
    count, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    ys = sorted(int(y + hh // 2) for _, y, ww, hh, _ in stats[1:count] if ww >= 0.7 * sub.shape[1])
    return sum(1 for i, y in enumerate(ys) if i == 0 or y - ys[i - 1] > 6)


def ruled_tables(gray: np.ndarray) -> list[Box]:
    """Рамки разлинованных таблиц: связные сетки из горизонтальных и вертикальных линий бланка."""
    h, w = gray.shape
    horiz, vert = _line_masks(gray)
    grid = cv2.dilate(horiz | vert, np.ones((7, 7), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(grid, connectivity=8)
    out: list[Box] = []
    for x, y, bw, bh, _ in stats[1:count]:
        if bw >= 0.15 * w and bh >= 0.015 * h:
            out += _tables_in(horiz[y : y + bh, x : x + bw], vert[y : y + bh, x : x + bw], (int(x), int(y)), (w, h), depth=2)
    out.sort(key=area, reverse=True)
    return [b for i, b in enumerate(out) if not any(inside(b, a) >= 0.8 for a in out[:i])]


def _tables_in(horiz: np.ndarray, vert: np.ndarray, at: tuple[int, int], page: tuple[int, int], depth: int) -> list[Box]:
    """Таблицы в куске сетки: полосы со столбцами; полоса не таблица — без вертикалей во всю её высоту (рамка
    бланка склеивает соседние блоки) ищем ещё раз внутри."""
    w, h = page
    out: list[Box] = []
    for y0, y1 in _bands(vert, gap=int(0.012 * h), min_len=int(0.01 * h), horiz=horiz, page_h=h):
        band = horiz[y0:y1]
        xs = np.flatnonzero(band.any(axis=0))
        if xs.size == 0:
            continue
        l, r = int(xs[0]), int(xs[-1]) + 1
        l, r = _row_span(band, vert[y0:y1], l, r)  # подчёркивания и куски рамки сбоку на уровне строк — не таблица
        if r - l < 0.15 * w:
            continue
        rows = _runs(band[:, l:r].sum(axis=1) / 255 >= 0.4 * (r - l))
        # столбец таблицы идёт через всю её высоту; штрихи печати и перегородка в подписях — нет
        cols = _runs(vert[y0:y1, l:r].sum(axis=0) / 255 >= 0.5 * (y1 - y0))
        frame = y1 - y0 > 0.5 * h and cols < 6  # кайма скана / рамка листа: всё внутри — одна «таблица»
        # ТТН-1: строк без линий, только столбцы во всю высоту (штрихкод в плашке короче)
        tall = _runs(vert[y0:y1, l:r].sum(axis=0) / 255 >= 0.9 * (y1 - y0)) if rows < 3 else 0
        ruled = rows >= 3 and cols >= 3 or rows >= 1 and tall >= 6
        if ruled and not frame:
            t, b, l, r = _trim_banner(band, vert[y0:y1], l, r)
            out.append((at[0] + l, at[1] + y0 + t, at[0] + r, at[1] + y0 + b))
        elif depth > 0:
            inner = _drop_long(vert[y0:y1], 0.85 * (y1 - y0))
            if inner is not None:
                out += _tables_in(band, inner, (at[0], at[1] + y0), page, depth - 1)
    return out


def _trim_banner(horiz: np.ndarray, vert: np.ndarray, l: int, r: int) -> tuple[int, int, int, int]:
    """Крайний ряд выше остальных вдвое и с ≤ 3 столбцами — плашка заголовка вплотную к таблице: отрезать.

    Высокая шапка товарного раздела не режется — в ней столбцов столько же, сколько в таблице.
    Возвращает (верх, низ) внутри полосы и края по x.
    """
    height = horiz.shape[0]
    on = horiz[:, l:r].sum(axis=1) / 255 >= 0.4 * (r - l)
    ys = [int(np.mean(run)) for run in np.split(np.flatnonzero(on), np.flatnonzero(np.diff(np.flatnonzero(on)) > 1) + 1) if run.size]
    if len(ys) < 3:
        return 0, height, l, r
    segs = list(pairwise(ys))
    first, last = segs[0], segs[-1]

    def cols(a: int, b: int) -> int:
        return _runs(vert[a:b, l:r].sum(axis=0) / 255 >= 0.8 * (b - a))

    def banner(i: int) -> bool:
        i %= len(segs)
        a, b = segs[i]
        rest = [y1 - y0 for j, (y0, y1) in enumerate(segs) if j != i]
        return b - a > 2 * float(np.median(rest)) and cols(a, b) <= 3

    while len(segs) >= 2 and (banner(0) or banner(-1)):
        segs = segs[1:] if banner(0) else segs[:-1]
    if segs[0] == first and segs[-1] == last:
        return 0, height, l, r
    # необрезанный край — как был: верхняя линия таблицы бывает рваной и в ряды не попадает
    t = 0 if segs[0] == first else max(0, segs[0][0] - 4)
    b = height if segs[-1] == last else min(height, segs[-1][1] + 5)
    xs = np.flatnonzero(vert[t:b, l:r].sum(axis=0) / 255 >= 0.5 * (b - t))
    if xs.size:
        l, r = l + int(xs[0]), l + int(xs[-1]) + 1
    return t, b, l, r


def _row_span(band: np.ndarray, vert: np.ndarray, l: int, r: int) -> tuple[int, int]:
    """Края по x по длинным горизонталям (≥ 40 % ширины полосы), но не уже крайних столбцов: справа бывает
    колонка отметок с рваными строками; нет длинных горизонталей — исходные l, r."""
    count, _, stats, _ = cv2.connectedComponentsWithStats(band, connectivity=8)
    long_ = [(x, x + ww) for x, _, ww, _, _ in stats[1:count] if ww >= 0.4 * (r - l)]
    if not long_:
        return l, r
    a, b = min(x for x, _ in long_), max(x for _, x in long_)
    cols = np.flatnonzero(vert[:, l:r].sum(axis=0) / 255 >= 0.5 * vert.shape[0])
    if cols.size:
        a, b = min(a, l + int(cols[0])), max(b, l + int(cols[-1]) + 1)
    return int(a), int(b)


def _drop_long(vert: np.ndarray, limit: float) -> np.ndarray | None:
    """Вертикали короче limit; None — длинных не было (повторный поиск ничего не изменит)."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(vert, connectivity=8)
    long_ = stats[:, cv2.CC_STAT_HEIGHT] >= limit
    long_[0] = False
    if not long_[1:count].any():
        return None
    return np.where(long_[labels], 0, vert).astype(np.uint8)


def _bands(
    vert: np.ndarray, gap: int, min_len: int, horiz: np.ndarray | None = None, page_h: int = 0
) -> list[tuple[int, int]]:
    """Полосы по y, которые пересекают ≥ 3 столбца; разрыв короче gap склеивается, если столбцы по обе его
    стороны общие (иначе это два соседних блока, а не одна таблица с рваной строкой).

    Столбец — вертикаль, у которой есть пара с теми же концами (линии одной таблицы начинаются и кончаются
    вместе); одиночная перегородка в блоке подписей столбцом не считается. Буквы заголовка, штрихкод и
    штрихи печати короче 2 % страницы и не упираются в горизонтали — тоже не столбцы.
    """
    joined = cv2.dilate(vert, np.ones((9, 1), np.uint8))  # рваные линии скана
    count, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    page_h = page_h or vert.shape[0]
    tol = max(10, int(0.01 * page_h))
    spans = [(int(t), int(t + hh), int(x), int(x + ww)) for x, t, ww, hh, _ in stats[1:count] if hh - 8 >= 0.02 * page_h]
    if horiz is not None:
        spans = [s for s in spans if _capped(horiz, s)]
    cols = [
        s for i, s in enumerate(spans)
        if any(j != i and abs(s[0] - o[0]) <= tol and abs(s[1] - o[1]) <= tol for j, o in enumerate(spans))
    ]
    n = np.zeros(vert.shape[0] + 1, dtype=np.int32)
    for t, b, _, _ in cols:
        n[t] += 1
        n[b] -= 1
    on = np.cumsum(n)[:-1] >= 3

    def xs_at(y: int) -> list[int]:
        return [x for t, b, x, _ in cols if t <= y < b]

    bands: list[list[int]] = []
    for y in np.flatnonzero(on):
        step = y - bands[-1][1] if bands else gap + 1
        if step <= 1 or (step <= gap and _shared(xs_at(bands[-1][1]), xs_at(int(y))) >= 2):
            bands[-1][1] = int(y)
        else:
            bands.append([int(y), int(y)])
    pad = 4  # горизонтальная граница таблицы — там, где кончаются вертикали
    return [(max(0, a - pad), min(vert.shape[0], b + pad + 1)) for a, b in bands if b - a >= min_len]


def _capped(horiz: np.ndarray, span: tuple[int, int, int, int]) -> bool:
    """Вертикаль упирается концом в горизонталь (граница таблицы или строки) или пересекает её (линия шапки
    у таблицы без линий строк, где концы столбцов на скане не доходят до рамки); у обрезанного фрагмента
    бланка низ таблицы может быть оторван, поэтому хватает одного конца."""
    t, b, l, r = span
    reach = max(8, int(0.004 * horiz.shape[0]))
    x0, x1 = max(0, l - 8), r + 8

    def hit(y: int) -> bool:
        return bool(horiz[max(0, y - reach) : y + reach, x0:x1].any())

    def crossed() -> bool:
        rows = horiz[t:b]
        return bool((rows[:, max(0, l - 3)].astype(bool) & rows[:, min(rows.shape[1] - 1, r + 2)].astype(bool)).any())

    return hit(t) or hit(b - 1) or crossed()


def _shared(a: list[int], b: list[int], tol: int = 10) -> int:
    return sum(1 for x in a if any(abs(x - y) <= tol for y in b))


def _thin_only(mask: np.ndarray, thick: int, vertical: bool) -> np.ndarray:
    """Убирает плашки и заливки: средняя толщина линии (площадь / длина) ≤ thick; наклон скана её не раздувает."""
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    along = stats[:, cv2.CC_STAT_HEIGHT] if vertical else stats[:, cv2.CC_STAT_WIDTH]
    keep = stats[:, cv2.CC_STAT_AREA] / np.maximum(along, 1) <= thick
    keep[0] = False
    return np.where(keep[labels], 255, 0).astype(np.uint8)


def _runs(mask: np.ndarray) -> int:
    """Число непрерывных серий True (одна линия толщиной в несколько пикселей — одна серия)."""
    m = mask.astype(np.int8)
    return int(((m[1:] - m[:-1]) == 1).sum() + m[0])


# --- Heron ---


_ENGINE_ERROR: Exception | None = None  # без сети загрузка ждёт таймауты — на каждой странице не повторяем


@functools.lru_cache(maxsize=1)
def _engine() -> tuple[Any, dict[int, str]]:
    global _ENGINE_ERROR
    if _ENGINE_ERROR is not None:
        raise _ENGINE_ERROR
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions
    from docling.datamodel.settings import settings
    from docling.models.inference_engines import object_detection

    options = LayoutObjectDetectionOptions.from_preset("layout_heron_default")
    options.engine_options.score_threshold = min(HERON_MIN.values())
    try:
        engine = object_detection.create_object_detection_engine(
            options=options.engine_options,
            model_spec=options.model_spec,
            accelerator_options=AcceleratorOptions(),
            artifacts_path=settings.artifacts_path,  # DOCLING_ARTIFACTS_PATH: модели, скачанные docling-tools
        )
        engine.initialize()
    except Exception as exc:
        _ENGINE_ERROR = exc
        raise
    return engine, engine.get_label_mapping()


def _heron_once(image: Image.Image) -> list[Det]:
    from docling.models.inference_engines.object_detection import (
        ObjectDetectionEngineInput,
    )

    engine, names = _engine()
    out = engine.predict_batch([ObjectDetectionEngineInput(image=image.convert("RGB"), metadata={})])[0]
    dets = []
    for label_id, score, b in zip(out.label_ids, out.scores, out.bboxes, strict=False):
        label = names.get(int(label_id), str(label_id))
        r = role(label)
        if r in HERON_MIN and float(score) >= HERON_MIN[r]:
            dets.append(Det(r, float(score), tuple(int(v) for v in b), "heron", label))  # type: ignore[arg-type]
    return dets


def heron_layout(image: Image.Image, tiles: bool = True) -> list[Det]:
    """Сырой Heron (без постобработки Docling) на целой странице и, если tiles, на верхней/нижней половинах.

    Docling подаёт Heron страницу в 72 dpi; половина страницы — вдвое больше деталей на тот же вход модели.
    """
    w, h = image.size
    full = _heron_once(image)
    parts = []
    if tiles:
        for box in ((0, 0, w, int(_TILE * h)), (0, int((1 - _TILE) * h), w, h)):
            parts.append((_heron_once(image.crop(box)), box))
    return merge_tiles(full, parts, (w, h))


def merge_tiles(
    full: list[Det], tiles: list[tuple[list[Det], Box]], size: tuple[int, int], stitch: bool = False
) -> list[Det]:
    """Рамки плиток в координаты страницы; дубли — прочь (крупная рамка поглощает мелкую).

    Обрезанные краем плитки рамки выбрасываются (их видно целиком на full); со stitch (целой страницы нет)
    куски одной области из соседних половин склеиваются.
    """
    w, h = size
    edge = int(0.01 * h)
    pool = list(full)
    lower: list[Det] = []  # обрезаны низом верхней плитки
    upper: list[Det] = []  # обрезаны верхом нижней плитки
    for dets, (tl, tt, tr, tb) in tiles:
        for d in dets:
            l, t, r, b = d.box[0] + tl, d.box[1] + tt, d.box[2] + tl, d.box[3] + tt
            at_top, at_bottom = tt > 0 and t - tt <= edge, tb < h and tb - b <= edge
            cut = at_top or at_bottom or (tl > 0 and l - tl <= edge) or (tr < w and tr - r <= edge)
            moved = replace(d, box=(l, t, r, b))
            if not cut:
                pool.append(moved)
            elif stitch and at_bottom != at_top:
                (lower if at_bottom else upper).append(moved)
    used: set[int] = set()
    for a in lower:
        for i, b in enumerate(upper):
            xs = min(a.box[2], b.box[2]) - max(a.box[0], b.box[0])
            if i not in used and a.role == b.role and xs >= 0.6 * max(a.box[2] - a.box[0], b.box[2] - b.box[0]) and b.box[1] <= a.box[3]:
                box = (min(a.box[0], b.box[0]), a.box[1], max(a.box[2], b.box[2]), b.box[3])
                pool.append(Det(
                    a.role, min(a.score, b.score), box, a.source, a.label,
                    "\n\n".join(part for part in (a.text, b.text) if part.strip()),
                ))
                used.add(i)
                break
        else:
            pool.append(a)  # продолжения нет — кусок лучше, чем ничего
    pool += [b for i, b in enumerate(upper) if i not in used]
    kept: list[Det] = []
    for d in sorted(pool, key=lambda d: -d.score):
        if not any(k.role == d.role and iou(k.box, d.box) >= 0.5 for k in kept):
            kept.append(d)
    return [d for d in kept if not any(k is not d and k.role == d.role and area(k.box) > area(d.box) and inside(d.box, k.box) >= 0.9 for k in kept)]


# --- слияние ---


def fuse(
    size: tuple[int, int], heron: list[Det], dots: list[Det], ruled: list[Box], gray: np.ndarray | None = None
) -> list[Det]:
    """Итоговая разметка: таблицы по линиям или по согласию источников, текст Heron + пробелы из dots, мелкие картинки.

    С gray (сам скан): таблица моделей без единой вертикали — это поля формы, а не таблица; чернила вне всех
    областей добираются текстовыми блоками (source="ink").
    """
    w, h = size
    tiny = _TINY * w * h
    heron = [d for d in heron if area(d.box) >= tiny]
    dots = [d for d in dots if area(d.box) >= tiny]
    tables = _tables(heron, dots, ruled)
    vert = None
    if gray is not None:
        horiz, vert = _line_masks(gray)
        tables = [t for t in tables if t.source == "lines" or (_columns(vert, t.box) >= 2 and _full_rows(horiz, t.box) >= 2)]
    tboxes = [t.box for t in tables]

    def free(d: Det) -> bool:
        return all(inside(d.box, t) < 0.7 for t in tboxes)

    text: list[Det] = []
    for d in sorted((d for d in heron if d.role in ("text", "margin") and free(d)), key=lambda d: -d.score):
        if not any(iou(d.box, k.box) >= 0.5 for k in text):  # колонтитул и текст на одном месте — одна рамка
            text.append(d)
    marks = _marks(gray, horiz | vert) if gray is not None else None
    for d in dots:
        if d.role not in ("text", "margin") or not free(d):
            continue
        if marks is None:
            if min(1.0, sum(_cross(d.box, k.box) for k in text) / max(1.0, area(d.box))) < 0.3:
                text.append(d)
        elif _new_ink(marks, d.box, [k.box for k in text]) >= 0.3:
            text.append(d)  # dots нашёл чернила, которых нет в рамках Heron
    pics: list[Det] = []
    for d in [*heron, *dots]:
        if d.role != "picture" or not free(d) or any(iou(d.box, p.box) >= 0.5 for p in pics):
            continue
        if area(d.box) > PICTURE_MAX * w * h:
            continue  # «вся страница / полстраницы — картинка» — рамка скана или схлопнутая разметка
        if any(inside(t, d.box) >= 0.5 for t in tboxes) or sum(inside(x.box, d.box) >= 0.8 for x in text) >= 3:
            continue  # накрыла таблицу или блок формы
        pics.append(d)
    out = [*tables, *text, *pics]
    if gray is not None and vert is not None:
        out += [Det("text", 0.0, b, "ink") for b in ink_blocks(gray, [d.box for d in out], horiz | vert)]
    return sorted(out, key=lambda d: (d.box[1], d.box[0]))


def _marks(gray: np.ndarray, lines: np.ndarray) -> np.ndarray:
    """Тёмные чернила без линий бланка, крапа и бледного фона — то, что считается текстом."""
    ink = cv2.medianBlur(_ink(gray), 3)  # крап и зерно скана
    ink[gray >= _DARK] = 0  # бледный защитный фон бланка и серый шум — не текст
    ink[cv2.dilate(lines, np.ones((5, 5), np.uint8)) > 0] = 0
    return ink


def _new_ink(marks: np.ndarray, box: Box, boxes: list[Box]) -> float:
    """Доля чернил box вне boxes; пустая рамка — 0."""
    l, t, r, b = (max(0, v) for v in box)
    sub = marks[t:b, l:r] > 0
    total = int(sub.sum())
    if total == 0:
        return 0.0
    free = sub.copy()
    for bl, bt, br, bb in boxes:
        free[max(0, bt - t) : max(0, bb - t), max(0, bl - l) : max(0, br - l)] = False
    return float(free.sum()) / total


def ink_blocks(gray: np.ndarray, covered: list[Box], lines: np.ndarray | None = None) -> list[Box]:
    """Строки чернил вне covered: то, что не разметил никто (линии бланка и крап скана не в счёт)."""
    h, w = gray.shape
    if lines is None:
        horiz, vert = _line_masks(gray)
        lines = horiz | vert
    ink = _marks(gray, lines)
    for l, t, r, b in covered:
        ink[max(0, t) : b, max(0, l) : r] = 0
    kx, ky = max(9, w // 60), max(3, h // 400)
    blobs = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(blobs, connectivity=8)
    out: list[Box] = []
    for i in range(1, count):
        x, y, bw, bh, _ = stats[i]
        if bw < 0.02 * w or bh < 0.005 * h or bh > 0.04 * h:
            continue  # строка или подпись, не пятно и не тень
        edge = 0.01 * w
        if x <= edge or y <= edge or x + bw >= w - edge or y + bh >= h - edge:
            continue  # тень края листа, дырки, полоса сканера
        ys, xs = np.nonzero((labels[y : y + bh, x : x + bw] == i) & (ink[y : y + bh, x : x + bw] > 0))
        if xs.size < 0.05 * bw * bh:
            continue  # редкий мусор, растянутый дилатацией
        box = (int(x + xs.min()), int(y + ys.min()), int(x + xs.max()) + 1, int(y + ys.max()) + 1)
        if box[2] - box[0] < 2 * (box[3] - box[1]):
            continue  # строка вытянута вдоль; росчерк подписи, кусок печати, номер в кружке — нет
        out.append(box)
    return out


def _tables(heron: list[Det], dots: list[Det], ruled: list[Box]) -> list[Det]:
    out = [Det("table", 1.0, b, "lines") for b in ruled]
    rest = [d for d in [*heron, *dots] if d.role == "table" and all(_cross(d.box, b) < 0.5 * min(area(d.box), area(b)) for b in ruled)]
    clusters: list[list[Det]] = []
    for d in sorted(rest, key=lambda d: -area(d.box)):
        for c in clusters:
            if any(iou(d.box, k.box) >= 0.5 or inside(d.box, k.box) >= 0.8 for k in c):
                c.append(d)
                break
        else:
            clusters.append([d])
    for c in clusters:
        best = {s: max((d for d in c if d.source == s), key=lambda d: d.score, default=None) for s in ("heron", "dots")}
        hd, dd = best["heron"], best["dots"]
        if hd and dd:
            box = tuple(round((a + b) / 2) for a, b in zip(hd.box, dd.box, strict=True))
            out.append(Det("table", max(hd.score, dd.score), box, "heron+dots"))  # type: ignore[arg-type]
        elif dd:
            out.append(dd)
        elif hd and hd.score >= TABLE_SURE:
            out.append(hd)
    return out
