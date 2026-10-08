"""Страницы-сканы PDF: PNG (pypdfium2) → VLM (--vision), иначе заглушка со ссылкой на PNG.

Предобработка (по умолчанию): родное разрешение скана, лист на боку, наклон, освещение, шум;
на сколько повернуть (0/90/180/270) — спрашиваем VLM.
С layout (--scan-layout): Heron размечает выровненную страницу; рисунки и таблицы вырезаются и уходят
в VLM отдельно (длинная таблица — кусками по строкам с её шапкой), на копии страницы они закрашены и
помечены [FIGURE K] / [TABLE K]; высокая страница читается полосами по промежуткам между областями.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import warnings
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

from ingest_parse.media import MediaWriter, image_markdown
from ingest_parse.pdf_layout import Region
from ingest_parse.pdf_triage import SCAN_PLACEHOLDER, PdfPageWarning

_TEXT = "Текст с изображения"
_REASON = {
    "vision_off": "vision is off",
    "vision_error": "vision API failed: {detail}",
    "vision_empty": "vision API returned no text",
    "page_error": "page processing failed: {detail}",
}
_ROLE = {"picture": "figure", "chart": "figure", "table": "table"}
_MASKED = {"page_header", "page_footer"}
_MIN_CONFIDENCE = 0.5
_PAGE_SHARE = 0.6  # рисунок больше — страница-схема, читается целиком (таблицы режутся при любом размере)
_INSIDE_SHARE = 0.8
_PAD_PT = 4.0  # поле вокруг выреза, пункты
_MAX_ROWS = 10  # строк таблицы в одном куске для VLM
_TILE_HEIGHT = 1600  # px: таблица без линий выше этого — куски по высоте
_BAND_FACTOR = 1.5  # страница длиннее 1.5 × max_long_edge читается полосами
_MARK = re.compile(r"\**\\?\[(FIGURE|TABLE|HAND)\s+(\d+)\\?\]\**")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
_COLOR = {"figure": "blue", "table": "green", "masked": "gray", "text": "red"}
_OVER_TEXT = " over-text"  # рисунок поверх текста: на странице не закрашивается


@dataclass(frozen=True)
class ScanOptions:
    client: Any = None  # VisionClient; None — без --vision в сеть не ходим
    force: bool = False
    max_long_edge: int = 2048
    layout: bool = False  # разметка страниц-сканов: Heron (плитки) + сетка линий (+ dots)
    preprocess: bool = True  # поворот, наклон, свет, шум; False — простой рендер ×2
    dots: Any = None  # ttn.dots.Send — второй источник разметки; None — только Heron и линии


@dataclass
class _Page:
    image: Any  # PIL, выровненная страница
    dpi: float
    steps: list[str]
    stamps: Any = None  # bool-маска печатей по пикселям image (ttn.preprocess.stamp_mask) или None
    pen: list[tuple[int, int, int, int]] | None = None  # полосы ручки на image


def _safe(text: str) -> str:
    return " ".join(str(text).split()).replace("-->", "–>").replace('"', "'")


def _warn(number: int, outcome: str) -> None:
    warnings.warn(f"pdf page {number} classified as full_scan; {outcome}", PdfPageWarning, stacklevel=4)


def fill_scans(md: str, pdf_path: Path, numbers: list[int], media: MediaWriter | None, options: ScanOptions) -> str:
    """Каждую заглушку <!-- page N: scanned … --> заменить блоком pdf-scan; ошибка одной страницы не роняет файл."""
    if not numbers:
        return md
    with tempfile.TemporaryDirectory(prefix="ingest-parse-scan-", ignore_cleanup_errors=True) as tmp:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            for number in numbers:
                try:
                    block = _page_block(pdf, number, media, Path(tmp), options)
                except Exception as exc:  # сбой разметки/кэша на одной странице — остальные страницы пишутся
                    block = _failure(number, "page_error", f"{type(exc).__name__}: {exc}", None)
                md = md.replace(SCAN_PLACEHOLDER.format(n=number), block, 1)
        finally:
            pdf.close()
    return md


# --- VLM ---


def _complete(path: Path, prompt: str, mode: str, options: ScanOptions, cache_dir: Path) -> str:
    from ingest_parse.vision.cache import VisionCache
    from ingest_parse.vision.enrich import image_payload

    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    model = getattr(options.client, "model", None)
    mode = f"{mode}-{hashlib.sha1(prompt.encode()).hexdigest()[:8]}"  # правка промпта не отдаёт старый ответ
    cache = VisionCache(cache_dir / ".vision-cache")
    answer = None if options.force else cache.get(sha, mode, model)
    if answer is None:
        answer = options.client.complete(prompt, image_payload(path, max_long_edge=options.max_long_edge))
        cache.put(sha, mode, model, answer)
    return answer


def _vision(
    path: Path,
    number: int,
    options: ScanOptions,
    *,
    kind: str = "text_scan",
    mode: str = "page_extract",
    markers: bool = False,
    part: tuple[int, int] | None = None,
    cache_dir: Path | None = None,
) -> dict[str, str]:
    from ingest_parse.vision.prompts import build_page_prompt, parse_sections

    prompt = build_page_prompt(kind, number, markers=markers, part=part)
    return parse_sections(_complete(path, prompt, mode, options, cache_dir or path.parent))


def _orienter(options: ScanOptions, number: int, work: Path, cache_dir: Path):
    """VLM сравнивает страницу и её поворот на 180° (A/B) и говорит, где текст не вверх ногами; сбой — не крутим."""
    from ingest_parse.ttn.preprocess import upright_pair
    from ingest_parse.ttn.prompts import UPRIGHT, parse_upright_answer
    from ingest_parse.vision import VisionError

    def ask(image: Any) -> int:
        path = work / f"scan-{number:03d}-upright.png"
        upright_pair(image).save(path)
        try:
            answer = _complete(path, UPRIGHT, "upright", options, cache_dir)
        except VisionError as exc:
            print(f"  page {number}: orientation failed: {exc}", flush=True)
            return 0
        turn = parse_upright_answer(answer)
        print(f"  page {number}: orientation {turn}° ← {' '.join(answer.split())[:80]}", flush=True)
        return turn

    return ask


# --- страница ---


def _load(pdf: Any, number: int, options: ScanOptions, work: Path, cache_dir: Path) -> _Page:
    from ingest_parse.ttn.raster import MAX_DPI, MIN_DPI, _native_dpi

    page = pdf[number - 1]
    try:
        if not options.preprocess:
            return _Page(page.render(scale=2).to_pil(), 144.0, [], None, [])
        native = _native_dpi(page)
        dpi = min(MAX_DPI, max(MIN_DPI, native or MIN_DPI))
        image = page.render(scale=dpi / 72).to_pil()
    finally:
        page.close()
    from ingest_parse.ttn.preprocess import prepare

    orient = _orienter(options, number, work, cache_dir) if options.client is not None else None
    prep = prepare(image, dpi, orient=orient)
    return _Page(prep.image, prep.dpi, prep.steps, prep.stamps, prep.pen)


def _regions(page: _Page, number: int, options: ScanOptions) -> tuple[list[Region] | None, str]:
    """Итоговая разметка и текст dots. Heron упал — рамок нет, буквы dots всё равно возвращаются."""
    from ingest_parse.ttn.dots import page_text
    from ingest_parse.ttn.layout import fuse, heron_layout, ruled_tables

    heron_ok = True
    try:
        heron = heron_layout(page.image)
    except Exception as exc:  # модель не скачана, сбой Docling — страница идёт без раскладки
        warnings.warn(f"heron layout failed: {exc}; page {number} goes without layout", PdfPageWarning, stacklevel=4)
        heron, heron_ok = [], False
    dots = []
    if options.dots is None:
        print(f"  page {number}: dots off (SCAN_DOTS_URL is empty)", flush=True)
    else:
        from ingest_parse.ttn.dots import dots_layout

        try:
            dots, status = dots_layout(page.image, options.dots)
            tables = sum(d.role == "table" for d in dots)
            print(
                f"  page {number}: dots {status}; regions={len(dots)}; tables={tables}; chars={sum(len(d.text) for d in dots)}",
                flush=True,
            )
        except Exception as exc:  # сервер dots недоступен — хватит Heron и линий
            print(f"  page {number}: dots FAILED {type(exc).__name__}: {exc}", flush=True)
            warnings.warn(f"dots layout failed: {exc}; page {number} uses heron + lines", PdfPageWarning, stacklevel=4)
    text = page_text(dots)
    if not heron_ok:
        return None, text
    from ingest_parse.ttn.preprocess import without_stamps

    gray = without_stamps(np.asarray(page.image.convert("L"), dtype=np.uint8), page.stamps)
    regions = [Region(_label(d), d.score, d.box) for d in fuse(page.image.size, heron, dots, ruled_tables(gray), gray)]
    return regions, text


def _label(d: Any) -> str:
    if d.role in ("table", "picture"):
        return d.role
    return d.label.lower().replace("-", "_") if d.label else "text"


def _page_block(pdf: Any, number: int, media: MediaWriter | None, tmp: Path, options: ScanOptions) -> str:
    cache_dir = media.directory if media is not None else tmp
    try:
        page = _load(pdf, number, options, tmp, cache_dir)
    except (pdfium.PdfiumError, OSError, ValueError) as exc:
        _warn(number, f"placeholder emitted (render failed: {exc})")
        return f"<!-- page {number}: scanned; render failed: {_safe(exc)} -->"
    image = page.image
    if media is not None:
        path, link = media.save_scan(image, number)
    else:
        path, link = tmp / f"scan-{number:03d}.png", None
        image.save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    plan = None
    ocr = ""
    if options.layout:
        regions, ocr = _regions(page, number, options)
        plan = _plan(regions, image.size) if regions is not None else None
    if plan is not None:
        masked = _masked(image, plan, page.dpi)
        if media is not None:
            _save_debug(media, number, page, masked, plan)

    if options.client is None:
        if ocr.strip():
            _warn(number, "rendered + dots text")
            return _block(number, sha, ocr.strip(), {}, link, "dots.mocr", plan is not None)
        return _failure(number, "vision_off", "", link)
    from ingest_parse.vision import VisionError

    hands = [
        (Region("hand", 1.0, box), f"hand {i}") for i, box in enumerate(page.pen or [], 1)
    ]
    if hands:
        print(f"  page {number}: pen lines={len(hands)}", flush=True)
        masked = _masked(image if plan is None else masked, hands, page.dpi)
    crops = [(use, r) for r, use in plan or [] if use.startswith(("figure ", "table "))]
    try:
        if plan is None and not hands:
            sections = _vision(path, number, options)
        else:
            sections = _page_text(masked, plan or hands, number, options, tmp, path.parent, markers=bool(crops or hands))
    except VisionError as exc:
        return _failure(number, "vision_error", str(exc), link)
    body = sections.pop(_TEXT, "").strip()
    parts: dict[str, str] = {}
    if crops:
        parts.update(_crop_parts(page, number, crops, media, tmp, options))
    if hands:
        parts.update(_hand_parts(page, number, hands, tmp, options, path.parent))
    if parts:
        body = _merge(body, parts)
    if not (body or sections):
        return _failure(number, "vision_empty", "", link)
    _warn(number, "rendered + heron + vision" if plan is not None else "rendered + vision")
    return _block(number, sha, body, sections, link, getattr(options.client, "model", None), plan is not None)


def _page_text(
    masked: Any, plan: list[tuple[Region, str]], number: int, options: ScanOptions, tmp: Path, cache_dir: Path,
    *, markers: bool,
) -> dict[str, str]:
    """Текст закрашенной страницы; высокая страница — полосами, разрезы только между областями Heron."""
    bands = _bands(masked, plan, options.max_long_edge)
    texts: list[str] = []
    sections: dict[str, str] = {}
    for i, (top, bottom) in enumerate(bands, 1):
        path = tmp / f"scan-{number:03d}-band-{i}.png"
        masked.crop((0, top, masked.width, bottom)).save(path)
        part = (i, len(bands)) if len(bands) > 1 else None
        mode = "page_layout" if part is None else "page_band"
        got = _vision(path, number, options, mode=mode, markers=markers, part=part, cache_dir=cache_dir)
        texts.append(got.pop(_TEXT, "").strip())
        for key, value in got.items():
            sections.setdefault(key, value)
    sections[_TEXT] = "\n\n".join(t for t in texts if t)
    return sections


def _bands(image: Any, plan: list[tuple[Region, str]], max_edge: int) -> list[tuple[int, int]]:
    height = image.height
    if height <= _BAND_FACTOR * max_edge:
        return [(0, height)]
    n = math.ceil(height / (max_edge * 1.1))
    busy = sorted((int(r.box[1]), int(r.box[3])) for r, _ in plan)
    merged: list[list[int]] = []
    for top, bottom in busy:
        if merged and top <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], bottom)
        else:
            merged.append([top, bottom])
    gaps = [(a[1] + b[0]) // 2 for a, b in zip(merged, merged[1:], strict=False) if b[0] - a[1] > 4]
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    blank = np.flatnonzero((gray < 128).sum(axis=1) < max(2, 0.002 * image.width))
    cuts = [0]
    for k in range(1, n):
        target, window = height * k / n, 0.2 * height
        options = [y for y in gaps if abs(y - target) <= window and not any(t < y < b for t, b in merged)]
        options = options or [int(y) for y in blank if abs(y - target) <= window and not any(t < y < b for t, b in merged)]
        if options:
            cut = min(options, key=lambda y: abs(y - target))
            if cut - cuts[-1] > 0.15 * height:
                cuts.append(cut)
    cuts.append(height)
    return list(zip(cuts, cuts[1:], strict=False))


# --- Heron: отбор областей, отладка, вырезы, слияние ---


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _inside(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> float:
    """Доля inner, лежащая внутри outer."""
    common = (max(inner[0], outer[0]), max(inner[1], outer[1]), min(inner[2], outer[2]), min(inner[3], outer[3]))
    return _area(common) / _area(inner) if _area(inner) else 0.0


def _plan(regions: list[Region], size: tuple[float, float]) -> list[tuple[Region, str]]:
    """Каждой области — роль: «figure K» / «table K» (вырез), masked, text или skipped: причина."""
    page = size[0] * size[1]
    taken: list[Region] = []
    count = {"figure": 0, "table": 0}
    plan = []
    for r in sorted(regions, key=lambda r: (r.box[1], r.box[0])):
        role = _ROLE.get(r.label)
        if r.label in _MASKED:
            # колонтитул внутри другой области (форма, итоги) — это содержимое, не номер страницы
            inner = any(o is not r and o.label not in _MASKED and _inside(r.box, o.box) >= 0.5 for o in regions)
            use = "text" if inner else "masked"
        elif role is None:
            use = "text"
        elif r.confidence < _MIN_CONFIDENCE:
            use = "skipped: low confidence"
        elif role == "figure" and _area(r.box) >= _PAGE_SHARE * page:
            use = "skipped: page-sized"
        elif any(_inside(r.box, t.box) >= _INSIDE_SHARE for t in taken):
            use = "skipped: inside another region"
        else:
            count[role] += 1
            use = f"{role} {count[role]}"
            if role == "figure" and _over_text(r, regions):
                use += _OVER_TEXT  # печать на подписях, «ОБРАЗЕЦ», плашка с заголовком: вырез есть, закраски нет
            taken.append(r)
        plan.append((r, use))
    return plan


def _over_text(figure: Region, regions: list[Region]) -> bool:
    return any(
        o.label not in _MASKED and _ROLE.get(o.label) is None and _inside(o.box, figure.box) >= 0.3
        for o in regions
    )


def _px(box: tuple[float, float, float, float], limit: tuple[int, int], pad: float) -> tuple[int, int, int, int]:
    l, t, r, b = box
    return (max(0, int(l - pad)), max(0, int(t - pad)), min(limit[0], int(r + pad + 0.5)), min(limit[1], int(b + pad + 0.5)))


def _font(size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # Pillow без FreeType
        return ImageFont.load_default()


def _masked(image: Any, plan: list[tuple[Region, str]], dpi: float) -> Any:
    """Копия страницы для VLM: колонтитулы, рисунки и таблицы закрашены; на рисунках и таблицах — метка."""
    out = image.copy()
    draw = ImageDraw.Draw(out)
    for r, use in plan:
        if use != "masked" and not use.startswith(("figure ", "table ", "hand ")) or use.endswith(_OVER_TEXT):
            continue
        box = _px(r.box, image.size, dpi / 72)
        draw.rectangle(box, fill="white")
        if use != "masked":
            role, k = use.split()[:2]
            draw.text(
                ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2),
                f"[{role.upper()} {k}]",
                fill="black",
                anchor="mm",
                font=_font(max(24, min(96, (box[3] - box[1]) // 3))),
            )
    return out


def _save_debug(media: MediaWriter, number: int, page: _Page, masked: Any, plan: list[tuple[Region, str]]) -> None:
    """media/debug/: рамки Heron, JSON областей и то, что увидит VLM."""
    boxes = page.image.convert("RGB")
    draw = ImageDraw.Draw(boxes)
    font = _font(28)
    for r, use in plan:
        color = _COLOR.get(use.split()[0], "orange")
        box = _px(r.box, boxes.size, 0.0)
        draw.rectangle(box, outline=color, width=4)
        draw.text((box[0] + 4, max(0, box[1] - 30)), f"{r.label} {r.confidence:.2f} → {use}", fill=color, font=font)
    stem = f"debug/scan-{number:03d}"
    media.save_named(boxes, f"{stem}-heron.png")
    media.save_named(masked, f"{stem}-masked.png")
    data = {
        "page": number,
        "dpi": round(page.dpi),
        "preprocess": page.steps,
        "size_px": list(page.image.size),
        "regions": [
            {"label": r.label, "confidence": round(r.confidence, 3), "box_px": [round(v) for v in r.box], "use": use}
            for r, use in plan
        ],
    }
    (media.directory / f"{stem}-heron.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _save(image: Any, name: str, media: MediaWriter | None, tmp: Path) -> tuple[Path, str | None]:
    if media is not None:
        return media.save_named(image, name)
    image.save(tmp / name)
    return tmp / name, None


def _hand_parts(
    page: _Page, number: int, hands: list[tuple[Region, str]], tmp: Path, options: ScanOptions, cache_dir: Path,
) -> dict[str, str]:
    """Каждая полоса ручки — отдельный увеличенный вырез, чтобы буквы читались крупнее."""
    from ingest_parse.vision.prompts import build_hand_prompt

    prompt = build_hand_prompt()
    parts = {}
    pad = _PAD_PT * page.dpi / 72
    for use, region in hands:
        k = use.split()[1]
        box = _px(region.box, page.image.size, pad)
        crop = page.image.crop(box)
        if crop.height < 180:
            crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
        path = tmp / f"scan-{number:03d}-hand-{k}.png"
        crop.save(path)
        parts[f"HAND {k}"] = _complete(path, prompt, "hand", options, cache_dir).strip()
    return parts


def _crop_parts(
    page: _Page, number: int, crops: list[tuple[str, Region]], media: MediaWriter | None, tmp: Path, options: ScanOptions
) -> dict[str, str]:
    """{"TABLE 1": Markdown-таблица, "FIGURE 1": vision-блок + ссылка}; вырезы — с выровненной страницы."""
    parts = {}
    pad = _PAD_PT * page.dpi / 72
    for use, r in crops:
        role, k = use.split()[:2]
        box = _px(r.box, page.image.size, pad)
        if role == "figure":
            path, link = _save(page.image.crop(box), f"scan-{number:03d}-fig-{k}.png", media, tmp)
            parts[f"FIGURE {k}"] = _figure(path, link, int(k), number, options)
        else:
            parts[f"TABLE {k}"] = _table(page, box, int(k), number, media, tmp, options)
    return parts


def _figure(path: Path, link: str | None, k: int, number: int, options: ScanOptions) -> str:
    from ingest_parse.vision.enrich import describe_image

    alt = f"Рисунок {k}, страница {number}"
    block = describe_image(path, alt, options.client, options.force, options.max_long_edge).rstrip("\n")
    return f"{block}\n\n{image_markdown(alt, link)}" if link is not None else block


_LABEL_PREFIX = re.compile(r"^\s*\**\s*(Описание|Текст с изображения|Анализ)\s*:?\s*\**\s*:?\s*")


class _HtmlTables(HTMLParser):
    """<table> → строки ячеек; colspan повторяет пустые ячейки, чтобы столбцы не съезжали."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._cell: list[str] | None = None
        self._span = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self.tables[-1].append([])
        elif tag in ("td", "th") and self.tables and self.tables[-1]:
            self._cell = []
            span = dict(attrs).get("colspan") or "1"
            self._span = int(span) if span.isdigit() else 1
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            row = self.tables[-1][-1]
            row.append(" ".join("".join(self._cell).split()).replace("|", "\\|"))
            row.extend([""] * (self._span - 1))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _html_tables(text: str) -> list[str]:
    parser = _HtmlTables()
    parser.feed(text)
    out = []
    for rows in parser.tables:
        rows = [r for r in rows if r]
        if len(rows) < 2:
            continue
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        out.append("\n".join(lines + ["| " + " | ".join(r) + " |" for r in rows[1:]]))
    return out


def _table_markdown(answer: str) -> str:
    """Таблица из ответа VLM, где бы она ни была: без меток разделов, в ```-блоке, в «Описании», HTML."""
    if "<table" in answer.lower():
        return "\n\n".join(_html_tables(answer))
    runs: list[list[str]] = [[]]
    for line in answer.splitlines():
        line = _LABEL_PREFIX.sub("", line).strip()
        if line.startswith("|"):
            runs[-1].append(line)
        elif runs[-1]:
            runs.append([])
    return "\n\n".join("\n".join(run) for run in runs if len(run) >= 2)


def _sharper(path: Path, tmp: Path) -> Path:
    """Увеличенный и резкий вырез для второй попытки (другие байты — мимо кэша первой)."""
    from PIL import ImageFilter

    out = tmp / f"{path.stem}-retry.png"
    with Image.open(path) as image:
        big = image.convert("L").resize((int(image.width * 1.5), int(image.height * 1.5)), Image.Resampling.LANCZOS)
    big.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=2)).save(out)
    return out


def _read_table(
    path: Path, number: int, options: ScanOptions, kind: str, mode: str, tmp: Path, hint: str = ""
) -> tuple[str, str]:
    """(таблица, причина неудачи); не вышло — вторая попытка: увеличенный вырез и строгий промпт."""
    from ingest_parse.vision import VisionError
    from ingest_parse.vision.prompts import build_page_prompt, build_table_retry_prompt

    reason = ""
    tries = [
        (path, build_page_prompt(kind, number) + hint, mode),
        (None, build_table_retry_prompt(number) + hint, "table_retry"),
    ]
    for image, prompt, try_mode in tries:
        try:
            answer = _complete(image or _sharper(path, tmp), prompt, try_mode, options, path.parent)
        except VisionError as exc:
            reason = f"vision API failed: {exc}"
            continue
        table = _table_markdown(answer)
        if table:
            return table, ""
        reason = "no table in answer: " + _safe(answer)[:80]
    return "", reason


def _table(
    page: _Page, box: tuple[int, int, int, int], k: int, number: int, media: MediaWriter | None, tmp: Path,
    options: ScanOptions,
) -> str:
    """Таблица: ≤ 10 строк — один вырез; длиннее — куски по строкам, у каждого сверху шапка таблицы."""
    from ingest_parse.ttn.prompts import grid_hint
    from ingest_parse.ttn.zones import table_chunks, table_zones, with_grid

    zones = table_zones(page.image, box)
    tall = len(zones.rows) > _MAX_ROWS or (not zones.rows and box[3] - box[1] > _TILE_HEIGHT)
    pieces = [c.image for c in table_chunks(page.image, zones, _MAX_ROWS)] if tall else [with_grid(page.image, zones).crop(box)]
    hint = grid_hint(len(zones.columns) - 1)
    alt = f"Таблица {k}, страница {number}"
    texts, misses = [], []
    for j, piece in enumerate(pieces, 1):
        name = f"scan-{number:03d}-table-{k}.png" if len(pieces) == 1 else f"scan-{number:03d}-table-{k}-{j}.png"
        path, link = _save(piece, name, media, tmp)
        kind, mode = ("table_scan", "table_extract") if len(pieces) == 1 else ("table_tile", "table_tile")
        text, reason = _read_table(path, number, options, kind, mode, tmp, hint)
        if text:
            texts.append(text)
        else:
            part = "" if len(pieces) == 1 else f" part {j}"
            warnings.warn(
                f"pdf page {number}: table {k}{part} not extracted: {reason}; image link emitted",
                PdfPageWarning,
                stacklevel=4,
            )
            label = alt if len(pieces) == 1 else f"{alt}, часть {j}"
            misses.append(image_markdown(label, link) if link is not None else f"<!-- {label}: not extracted -->")
    out = [_join_tables(texts)] if texts else []
    return "\n\n".join(out + misses)


def _join_tables(texts: list[str]) -> str:
    """Куски одной таблицы → одна: шапка — только из первого куска; повтор строки на стыке убирается."""
    lines: list[str] = []
    for i, text in enumerate(texts):
        rows = text.strip().splitlines()
        if i:
            sep = next((n for n, row in enumerate(rows[:4]) if _SEPARATOR.match(row)), None)
            if sep is not None:
                rows = rows[sep + 1 :]
        for row in rows:
            if not (lines and row.strip() == lines[-1].strip()):
                lines.append(row)
    return "\n".join(lines)


def _merge(body: str, parts: dict[str, str]) -> str:
    """Метки [FIGURE K] / [TABLE K] → содержимое; потерянные моделью — в конец страницы."""
    used: set[str] = set()

    def put(m: re.Match[str]) -> str:
        key = f"{m.group(1)} {m.group(2)}"
        if key not in parts or key in used:
            return ""
        used.add(key)
        return f"\n\n{parts[key]}\n\n"

    body = _MARK.sub(put, body)
    rest = [text for key, text in parts.items() if key not in used]
    body = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", body).strip()
    return "\n\n".join([body, *rest] if body else rest)


def _kind(body: str) -> str:
    lines = [line for line in body.splitlines() if line.strip()]
    rows = sum(line.lstrip().startswith("|") for line in lines)
    if rows >= 2 and rows >= 0.8 * len(lines):
        return "table_scan"
    return "diagram" if len(body) < 40 else "text_scan"


def _block(
    number: int,
    sha: str,
    body: str,
    sections: dict[str, str],
    link: str | None,
    model: str | None,
    layout: bool = False,
) -> str:
    from ingest_parse.vision.markers import render_block

    kind = _kind(body)
    source = 'source="vision" layout="heron"' if layout else 'source="vision"'
    out = [f'<!-- pdf-scan:begin page="{number}" {source} sha256="{sha}" kind="{kind}" -->', ""]
    if body:
        if not re.search(r"^#{1,6} ", body, re.M):
            out += [f"## Страница {number}", ""]  # ориентир, если в извлечённом тексте нет заголовка
        out += [body, ""]
    if sections:
        out += [render_block(f"scan-{number:03d}", sha, kind, model, sections).rstrip("\n"), ""]
    if link is not None:
        out += [image_markdown(f"Страница {number} (скан)", link), ""]
    out.append(f'<!-- pdf-scan:end page="{number}" -->')
    return "\n".join(out)


def _failure(number: int, code: str, detail: str, link: str | None) -> str:
    human = _REASON[code].format(detail=detail)
    _warn(number, f"placeholder emitted ({human}{'; use --vision' if code == 'vision_off' else ''})")
    out = [
        f'<!-- pdf-scan:begin page="{number}" source="none" status="error" reason="{code}" -->',
        f"<!-- page {number}: scanned; {_safe(human)}{'; see media' if link else ''} -->",
    ]
    if link is not None:
        out += ["", image_markdown(f"Страница {number} (скан)", link)]
    out += ["", f'<!-- pdf-scan:end page="{number}" -->']
    return "\n".join(out)
