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


def _gray(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.uint8)


def _ink(gray: np.ndarray) -> np.ndarray:
    """Бинарная маска «чернил» (255) на уменьшенной копии."""
    scale = _WORK_WIDTH / max(gray.shape[1], 1)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    return cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)


def _rotate(img: np.ndarray, angle: float, fill: int = 0) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=fill)


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


def is_sideways(gray: np.ndarray, limit: float = 10.0) -> bool:
    """Строки идут вертикально (страница на боку): при лучшем угле профиль по столбцам чётче, чем по строкам."""
    ink = _ink(gray)
    angles = np.arange(-limit, limit + 0.01, 1.0)

    def best(img: np.ndarray) -> float:
        return max(_sharpness(_rotate(img, a)) for a in angles)

    return best(np.ascontiguousarray(ink.T)) > 1.3 * best(ink)


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
    out = Prepared(image, dpi)
    if dpi < TARGET_DPI - 10:
        factor = TARGET_DPI / dpi
        gray = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        out.dpi = TARGET_DPI
        out.steps.append(f"upscale {dpi:.0f}→{TARGET_DPI} dpi")
    if is_sideways(gray):
        gray = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
        out.rotation = 270
        out.steps.append("rotate 90° (sideways)")
    skew = detect_skew(gray)
    if abs(skew) >= 0.1:
        gray = _rotate(gray, skew, fill=255)
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
    if turn in _CV_ROTATE:
        gray = cv2.rotate(gray, _CV_ROTATE[turn])
        out.rotation = (out.rotation + 360 - turn) % 360
        out.steps.append(f"rotate {turn}° clockwise (vision)")
    out.image = Image.fromarray(gray)
    return out
