"""Скан страницы → выровненное серое изображение: поворот 90°, наклон, освещение, контраст, шум."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

TARGET_DPI = 300
_WORK_WIDTH = 1200  # оценка поворота и наклона — на уменьшенной копии


@dataclass
class Prepared:
    image: Image.Image  # L, выровненная страница
    dpi: float
    rotation: int = 0  # итоговый поворот против часовой: 0 / 90 / 180 / 270
    skew: float = 0.0  # исправленный наклон, градусы
    steps: list[str] = field(default_factory=list)
    stamps: np.ndarray | None = None  # bool по пикселям image: синие/фиолетовые печати; None — скан серый
    pen: list[tuple[int, int, int, int]] = field(default_factory=list)  # полосы ручки на image
    rgb: Image.Image | None = None  # те же пиксельные размеры, что image; без denoise/levels


def stamp_mask(image: Image.Image) -> np.ndarray | None:
    """Печати по цвету: насыщенные тёмные синие/фиолетовые пятна размером с печать; None — нет цвета.

    Бирюзовый защитный фон бланка бледный, синяя рукопись и подпись вытянуты в строку — их маска не берёт.
    """
    if image.mode not in ("RGB", "RGBA", "P", "CMYK"):
        return None
    rgb = np.asarray(image.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    ink = ((sat >= 70) & (val <= 200) & (hue >= 100) & (hue <= 160)).astype(np.uint8) * 255
    if not ink.any():
        return np.zeros(ink.shape, dtype=bool)
    ink = _without_lines(ink, 30)  # бланк ТН-2 печатают синей краской: его линии — не печать
    w = ink.shape[1]
    k = max(5, w // 100)
    blobs = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(blobs, connectivity=8)
    keep = np.zeros(count, dtype=bool)
    for i in range(1, count):
        _, _, bw, bh, _ = stats[i]
        if bw >= 0.05 * w and bh >= 0.05 * w and 0.4 <= bw / bh <= 2.5:
            keep[i] = True  # круглая / квадратная печать, штамп; строка рукописи сюда не проходит
    return keep[labels] & (ink > 0)


def pen_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Полосы ручки: толстые синие/фиолетовые штрихи. Тонкая печать бланка и круглая печать — нет."""
    mask = pen_mask(image)
    return [] if mask is None else _boxes_from_pen_mask(mask)


def pen_mask(image: Image.Image) -> np.ndarray | None:
    """Маска пикселей ручки (0/255), до поворотов страницы; None — серый скан."""
    if image.mode not in ("RGB", "RGBA", "P", "CMYK"):
        return None
    rgb = np.asarray(image.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    ink = ((sat >= 40) & (val <= 210) & (hue >= 90) & (hue <= 170)).astype(np.uint8) * 255
    if not ink.any():
        return np.zeros(ink.shape, dtype=np.uint8)
    return ink


def _boxes_from_pen_mask(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Толщина штриха отделяет ручку от печатной буквы; вытянутость — от печати."""
    if not mask.any():
        return []
    dist = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 3)
    thick = np.zeros_like(mask)
    thick[(mask > 0) & (dist >= 2.2)] = 255
    thick = cv2.morphologyEx(thick, cv2.MORPH_CLOSE, np.ones((5, 15), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(thick, connectivity=8)
    h, w = mask.shape
    boxes = []
    for x, y, bw, bh, _ in stats[1:count]:
        if bw < 40 or bh < 8 or bh > 0.2 * h or bw < 2 * bh:
            continue
        if bw >= 0.05 * w and bh >= 0.05 * w and 0.4 <= bw / max(bh, 1) <= 2.5:
            continue
        boxes.append((int(x), int(y), int(x + bw), int(y + bh)))
    boxes.sort(key=lambda b: (b[1], b[0]))
    bands: list[list[int]] = []
    for x0, y0, x1, y1 in boxes:
        if bands and y0 <= bands[-1][3] + 4:
            band = bands[-1]
            band[0], band[1] = min(band[0], x0), min(band[1], y0)
            band[2], band[3] = max(band[2], x1), max(band[3], y1)
        else:
            bands.append([x0, y0, x1, y1])
    pad = 6
    return [
        (max(0, l - pad), max(0, t - pad), min(w, r + pad), min(h, b + pad))
        for l, t, r, b in bands
    ]


def _gray(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.uint8)


def _ink(gray: np.ndarray) -> np.ndarray:
    """Бинарная маска «чернил» (255) на уменьшенной копии."""
    scale = _WORK_WIDTH / max(gray.shape[1], 1)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    return cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)


def _without_lines(ink: np.ndarray, part: int = 10) -> np.ndarray:
    """Маска без длинных прямых (≥ 1/part стороны): линии таблиц, подчёркивания, рамки."""
    h, w = ink.shape
    horiz = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, w // part), 1)))
    vert = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, h // part))))
    lines = cv2.dilate(horiz | vert, np.ones((3, 3), np.uint8))
    return cv2.bitwise_and(ink, cv2.bitwise_not(lines))


def _rotate(img: np.ndarray, angle: float, fill: int = 0) -> np.ndarray:
    """Поворот в том же кадре: так сравнивается наклон. Край картинки обрезается."""
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=fill)


def _unskew(img: np.ndarray, angle: float, fill: int = 0) -> np.ndarray:
    """Тот же угол, но холст растёт: угол страницы и буквы у края не срезаются."""
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += (nw - w) / 2
    m[1, 2] += (nh - h) / 2
    border = fill if img.ndim == 2 else (fill, fill, fill)
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=border)


def _sharpness(ink: np.ndarray) -> float:
    """Чем чётче строки текста лежат горизонтально, тем больше дисперсия построчных сумм."""
    rows = ink.sum(axis=1, dtype=np.float64)
    return float(np.var(np.diff(rows)))


def detect_skew(gray: np.ndarray, limit: float = 10.0) -> float:
    """Угол (градусы), на который повернуть страницу, чтобы строки стали горизонтальными."""
    ink = _ink(gray)

    def best(angles: np.ndarray) -> float:
        return float(max(angles, key=lambda a: _sharpness(_rotate(ink, a))))

    coarse = best(np.arange(-limit, limit + 0.01, 0.5))
    return round(best(np.arange(coarse - 0.5, coarse + 0.51, 0.05)), 2)


def _text_lines(ink: np.ndarray) -> int:
    """Буквы, склеенные вдоль строки: вытянутые по горизонтали куски ≈ строки текста."""
    joined = cv2.dilate(ink, np.ones((1, 9), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    return sum(1 for _, _, w, h, _ in stats[1:count] if w >= 40 and w >= 5 * h and h >= 4)


def is_sideways(gray: np.ndarray) -> bool:
    """Строки идут вертикально (страница на боку): вертикальных строк текста заметно больше горизонтальных.

    Линии бланка — не текст и вычитаются: столбцы пустой таблицы на стоящем листе иначе похожи на строки на
    боку. Профили яркости тут не годятся — защитный фон бланка (гильош) даёт ложную «полосатость».
    На 22 реальных сканах: стоя ≤ 0.9, на боку ≥ 2.
    """
    ink = cv2.medianBlur(_without_lines(_ink(gray)), 3)
    return _text_lines(np.ascontiguousarray(ink.T)) > 1.4 * max(1, _text_lines(ink))


def normalize_light(gray: np.ndarray, dpi: float) -> np.ndarray:
    """Тени, жёлтая бумага, неравномерная засветка → ровный белый фон (деление на оценку фона)."""
    size = max(15, int(dpi / 6) | 1)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
    background = cv2.GaussianBlur(background, (0, 0), size / 4)
    return cv2.divide(gray, np.maximum(background, 1), scale=255)


def levels(gray: np.ndarray) -> np.ndarray:
    """Бумага → белая, самые тёмные штрихи → чёрные; линейно, без бинаризации (бледные печати остаются)."""
    paper = float(np.percentile(gray, 60))
    ink = float(np.percentile(gray, 0.5))
    if paper - ink < 40:
        return gray
    lut = np.clip((np.arange(256) - ink) * 255.0 / (paper * 0.96 - ink), 0, 255).astype(np.uint8)
    return cv2.LUT(gray, lut)


_CV_ROTATE = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def prepare(
    image: Image.Image,
    dpi: float,
    *,
    orient: Callable[[Image.Image], int] | None = None,
    denoise: bool = True,
) -> Prepared:
    """orient(картинка) → на сколько градусов по часовой повернуть (0/90/180/270; спрашиваем VLM); None — не спрашивать."""
    gray = _gray(image)
    color = np.asarray(image.convert("RGB"))
    stamps = stamp_mask(image)
    marks = stamps.astype(np.uint8) * 255 if stamps is not None else None  # повороты — те же, что у gray
    pen = pen_mask(image)
    out = Prepared(image, dpi)
    if dpi < TARGET_DPI - 10:
        factor = TARGET_DPI / dpi
        gray = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        color = cv2.resize(color, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_CUBIC)
        if marks is not None:
            marks = cv2.resize(marks, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_NEAREST)
        if pen is not None:
            pen = cv2.resize(pen, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_NEAREST)
        out.dpi = TARGET_DPI
        out.steps.append(f"upscale {dpi:.0f}→{TARGET_DPI} dpi")
    if is_sideways(gray):
        gray = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
        color = cv2.rotate(color, cv2.ROTATE_90_CLOCKWISE)
        marks = cv2.rotate(marks, cv2.ROTATE_90_CLOCKWISE) if marks is not None else None
        pen = cv2.rotate(pen, cv2.ROTATE_90_CLOCKWISE) if pen is not None else None
        out.rotation = 270
        out.steps.append("rotate 90° (sideways)")
    skew = detect_skew(gray)
    if abs(skew) >= 0.1:
        gray = _unskew(gray, skew, fill=255)
        color = _unskew(color, skew, fill=255)
        marks = _unskew(marks, skew, fill=0) if marks is not None else None
        pen = _unskew(pen, skew, fill=0) if pen is not None else None
        out.skew = skew
        out.steps.append(f"deskew {skew:+.2f}°")
    gray = normalize_light(gray, out.dpi)
    out.steps.append("flatten light")
    if denoise:
        gray = cv2.fastNlMeansDenoising(gray, None, h=9, templateWindowSize=7, searchWindowSize=21)
        out.steps.append("denoise")
    gray = levels(gray)
    out.steps.append("levels")
    turn = orient(Image.fromarray(gray)) % 360 if orient is not None else 0
    if turn == 180:
        gray = cv2.rotate(gray, _CV_ROTATE[180])
        color = cv2.rotate(color, _CV_ROTATE[180])
        marks = cv2.rotate(marks, _CV_ROTATE[180]) if marks is not None else None
        pen = cv2.rotate(pen, _CV_ROTATE[180]) if pen is not None else None
        out.rotation = (out.rotation + 180) % 360
        out.steps.append("rotate 180° clockwise (vision)")
    elif turn:
        # строки уже горизонтальны (геометрия выше); 90/270 от VLM — частая ошибка, лист на бок не кладём
        out.steps.append(f"vision said {turn}°, ignored")
    out.image = Image.fromarray(gray)
    out.rgb = Image.fromarray(color)
    if marks is not None:
        out.stamps = marks > 127
        if out.stamps.any():
            out.steps.append(f"stamps {out.stamps.mean():.2%}")
    if pen is not None:
        out.pen = _boxes_from_pen_mask(pen)
        if out.pen:
            out.steps.append(f"pen {len(out.pen)}")
    return out


def _long(mask: np.ndarray, length: int, *, vertical: bool) -> np.ndarray:
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    along = stats[:, cv2.CC_STAT_HEIGHT] if vertical else stats[:, cv2.CC_STAT_WIDTH]
    keep = along >= length
    keep[0] = False
    return keep[labels].astype(np.uint8) * 255


def without_stamps(gray: np.ndarray, stamps: np.ndarray | None) -> np.ndarray:
    """Серое для поиска линий и чернил: пиксели печатей — бумага (VLM видит страницу как есть).

    Линии бланка не стираются, даже если маска на них попала: синяя краска бланка ТН-2 близка к печати.
    """
    if stamps is None or not stamps.any():
        return gray
    from ingest_parse.ttn.layout import _line_masks

    horiz, vert = _line_masks(gray)
    h, w = gray.shape
    keep = _long(horiz, w // 20, vertical=False) | _long(vert, int(0.03 * h), vertical=True)  # штрихи печати короче
    lines = cv2.dilate(keep, np.ones((5, 5), np.uint8)) > 0
    out = gray.copy()
    out[(cv2.dilate(stamps.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0) & ~lines] = 255
    return out


def upright_pair(image: Image.Image, size: int = 1024) -> Image.Image:
    """Две копии страницы для VLM: слева A — как есть, справа B — повёрнутая на 180°; подписи сверху."""
    from PIL import ImageDraw, ImageFont

    page = image.convert("L")
    page.thumbnail((size, size))
    head, gap = 64, 24
    out = Image.new("L", (page.width * 2 + gap, page.height + head), 255)
    out.paste(page, (0, head))
    out.paste(page.rotate(180), (page.width + gap, head))
    ImageDraw.Draw(out).rectangle((page.width, 0, page.width + gap, out.height), fill=128)
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.load_default(size=48)
    except (TypeError, OSError):
        font = ImageFont.load_default()
    draw.text((page.width // 2 - 16, 6), "A", fill=0, font=font)
    draw.text((page.width + gap + page.width // 2 - 16, 6), "B", fill=0, font=font)
    return out
