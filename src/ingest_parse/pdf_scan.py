"""Страницы-сканы PDF: PNG (pypdfium2) → VLM (--vision), иначе заглушка со ссылкой на PNG.

С layout (--scan-layout): Heron размечает страницу; рисунки и таблицы вырезаются и уходят в VLM
отдельно, на копии страницы они закрашены и помечены [FIGURE K] / [TABLE K].
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from PIL import ImageDraw, ImageFont

from ingest_parse import pdf_layout
from ingest_parse.media import MediaWriter, image_markdown
from ingest_parse.pdf_layout import Region
from ingest_parse.pdf_triage import SCAN_PLACEHOLDER, PdfPageWarning

_TEXT = "Текст с изображения"
_REASON = {
    "vision_off": "vision is off",
    "vision_error": "vision API failed: {detail}",
    "vision_empty": "vision API returned no text",
}
_ROLE = {"picture": "figure", "chart": "figure", "table": "table"}
_MASKED = {"page_header", "page_footer"}
_MIN_CONFIDENCE = 0.5
_PAGE_SHARE = 0.6  # область больше — страница-схема, режется не она, а вся страница целиком идёт в VLM
_INSIDE_SHARE = 0.8
_RENDER_SCALE = 2
_CROP_SCALE = 4
_PAD = 4.0  # пункты PDF вокруг выреза
_MARK = re.compile(r"\**\[(FIGURE|TABLE)\s+(\d+)\]\**")
_COLOR = {"figure": "blue", "table": "green", "masked": "gray", "text": "red"}


@dataclass(frozen=True)
class ScanOptions:
    client: Any = None  # VisionClient; None — без --vision в сеть не ходим
    force: bool = False
    max_long_edge: int = 2048
    layout: bool = False  # Heron на страницах-сканах


def _safe(text: str) -> str:
    return " ".join(str(text).split()).replace("-->", "–>").replace('"', "'")


def _warn(number: int, outcome: str) -> None:
    warnings.warn(f"pdf page {number} classified as full_scan; {outcome}", PdfPageWarning, stacklevel=4)


def fill_scans(md: str, pdf_path: Path, numbers: list[int], media: MediaWriter | None, options: ScanOptions) -> str:
    """Каждую заглушку <!-- page N: scanned … --> заменить блоком pdf-scan; ошибка одной страницы не роняет файл."""
    if not numbers:
        return md
    layouts = None
    if options.layout:
        try:
            layouts = pdf_layout.detect_layout(pdf_path, numbers)
        except Exception as exc:  # модель не скачана, сбой Docling — сканы идут без раскладки
            warnings.warn(f"heron layout failed: {exc}; scans go without layout", PdfPageWarning, stacklevel=3)
    with tempfile.TemporaryDirectory(prefix="ingest-parse-scan-", ignore_cleanup_errors=True) as tmp:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            for number in numbers:
                regions = layouts.get(number) if layouts is not None else None
                block = _page_block(pdf, number, media, Path(tmp), options, regions)
                md = md.replace(SCAN_PLACEHOLDER.format(n=number), block, 1)
        finally:
            pdf.close()
    return md


def _render(pdf: Any, number: int, scale: float) -> tuple[Any, tuple[float, float]]:
    page = pdf[number - 1]
    try:
        return page.render(scale=scale).to_pil(), page.get_size()
    finally:
        page.close()


def _page_block(
    pdf: Any, number: int, media: MediaWriter | None, tmp: Path, options: ScanOptions, regions: list[Region] | None
) -> str:
    try:
        image, size = _render(pdf, number, _RENDER_SCALE)
    except (pdfium.PdfiumError, OSError, ValueError) as exc:
        _warn(number, f"placeholder emitted (render failed: {exc})")
        return f"<!-- page {number}: scanned; render failed: {_safe(exc)} -->"
    if media is not None:
        path, link = media.save_scan(image, number)
    else:
        path, link = tmp / f"scan-{number:03d}.png", None
        image.save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    plan = _plan(regions, size) if regions is not None else None
    if plan is not None:
        masked = _masked(image, plan, size)
        if media is not None:
            _save_debug(media, number, image, masked, plan, size)

    if options.client is None:
        return _failure(number, "vision_off", "", link)
    from ingest_parse.vision import VisionError

    crops = [(use, r) for r, use in plan or [] if use.startswith(("figure ", "table "))]
    try:
        if plan is None:
            sections = _vision(path, sha, number, options)
        else:
            page_path = tmp / f"scan-{number:03d}-masked.png"
            masked.save(page_path)
            page_sha = hashlib.sha256(page_path.read_bytes()).hexdigest()
            sections = _vision(
                page_path, page_sha, number, options, mode="page_layout", markers=bool(crops), cache_dir=path.parent
            )
    except VisionError as exc:
        return _failure(number, "vision_error", str(exc), link)
    body = sections.pop(_TEXT, "").strip()
    if crops:
        body = _merge(body, _crop_parts(pdf, number, crops, size, media, tmp, options))
    if not (body or sections):
        return _failure(number, "vision_empty", "", link)
    _warn(number, "rendered + heron + vision" if plan is not None else "rendered + vision")
    return _block(number, sha, body, sections, link, getattr(options.client, "model", None), plan is not None)


def _vision(
    path: Path,
    sha: str,
    number: int,
    options: ScanOptions,
    *,
    kind: str = "text_scan",
    mode: str = "page_extract",
    markers: bool = False,
    cache_dir: Path | None = None,
) -> dict[str, str]:
    from ingest_parse.vision.cache import VisionCache
    from ingest_parse.vision.enrich import image_payload
    from ingest_parse.vision.prompts import build_page_prompt, parse_sections

    model = getattr(options.client, "model", None)
    cache = VisionCache((cache_dir or path.parent) / ".vision-cache")
    answer = None if options.force else cache.get(sha, mode, model)
    if answer is None:
        prompt = build_page_prompt(kind, number, markers=markers)
        answer = options.client.complete(prompt, image_payload(path, max_long_edge=options.max_long_edge))
        cache.put(sha, mode, model, answer)
    return parse_sections(answer)


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
            use = "masked"
        elif role is None:
            use = "text"
        elif r.confidence < _MIN_CONFIDENCE:
            use = "skipped: low confidence"
        elif _area(r.box) >= _PAGE_SHARE * page:
            use = "skipped: page-sized"
        elif any(_inside(r.box, t.box) >= _INSIDE_SHARE for t in taken):
            use = "skipped: inside another region"
        else:
            count[role] += 1
            use = f"{role} {count[role]}"
            taken.append(r)
        plan.append((r, use))
    return plan


def _px(box: tuple[float, float, float, float], scale: float, limit: tuple[int, int], pad: float) -> tuple[int, ...]:
    l, t, r, b = box
    return (
        max(0, int((l - pad) * scale)),
        max(0, int((t - pad) * scale)),
        min(limit[0], int((r + pad) * scale + 0.5)),
        min(limit[1], int((b + pad) * scale + 0.5)),
    )


def _font(size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # Pillow без FreeType
        return ImageFont.load_default()


def _masked(image: Any, plan: list[tuple[Region, str]], size: tuple[float, float]) -> Any:
    """Копия страницы для VLM: колонтитулы, рисунки и таблицы закрашены; на рисунках и таблицах — метка."""
    out = image.copy()
    draw = ImageDraw.Draw(out)
    scale = image.width / size[0]
    for r, use in plan:
        if use != "masked" and not use.startswith(("figure ", "table ")):
            continue
        box = _px(r.box, scale, image.size, 1.0)
        draw.rectangle(box, fill="white")
        if use != "masked":
            role, k = use.split()
            draw.text(
                ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2),
                f"[{role.upper()} {k}]",
                fill="black",
                anchor="mm",
                font=_font(max(20, min(64, (box[3] - box[1]) // 3))),
            )
    return out


def _save_debug(
    media: MediaWriter, number: int, image: Any, masked: Any, plan: list[tuple[Region, str]], size: tuple[float, float]
) -> None:
    """media/debug/: рамки Heron, JSON областей и то, что увидит VLM."""
    boxes = image.copy()
    draw = ImageDraw.Draw(boxes)
    scale = image.width / size[0]
    font = _font(20)
    for r, use in plan:
        color = _COLOR.get(use.split()[0], "orange")
        box = _px(r.box, scale, image.size, 0.0)
        draw.rectangle(box, outline=color, width=3)
        draw.text((box[0] + 4, max(0, box[1] - 22)), f"{r.label} {r.confidence:.2f} → {use}", fill=color, font=font)
    stem = f"debug/scan-{number:03d}"
    media.save_named(boxes, f"{stem}-heron.png")
    media.save_named(masked, f"{stem}-masked.png")
    data = {
        "page": number,
        "size_pt": [round(size[0], 1), round(size[1], 1)],
        "regions": [
            {"label": r.label, "confidence": round(r.confidence, 3), "box_pt": [round(v, 1) for v in r.box], "use": use}
            for r, use in plan
        ],
    }
    (media.directory / f"{stem}-heron.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _crop_parts(
    pdf: Any,
    number: int,
    crops: list[tuple[str, Region]],
    size: tuple[float, float],
    media: MediaWriter | None,
    tmp: Path,
    options: ScanOptions,
) -> dict[str, str]:
    """{"TABLE 1": Markdown-таблица, "FIGURE 1": vision-блок + ссылка}; вырезы — с рендера ×4."""
    hires, _ = _render(pdf, number, _CROP_SCALE)
    parts = {}
    for use, r in crops:
        role, k = use.split()
        name = f"scan-{number:03d}-{'fig' if role == 'figure' else 'table'}-{k}.png"
        crop = hires.crop(_px(r.box, hires.width / size[0], hires.size, _PAD))
        if media is not None:
            path, link = media.save_named(crop, name)
        else:
            path, link = tmp / name, None
            crop.save(path)
        make = _figure if role == "figure" else _table
        parts[f"{role.upper()} {k}"] = make(path, link, int(k), number, options)
    return parts


def _figure(path: Path, link: str | None, k: int, number: int, options: ScanOptions) -> str:
    from ingest_parse.vision.enrich import describe_image

    alt = f"Рисунок {k}, страница {number}"
    block = describe_image(path, alt, options.client, options.force, options.max_long_edge).rstrip("\n")
    return f"{block}\n\n{image_markdown(alt, link)}" if link is not None else block


def _table(path: Path, link: str | None, k: int, number: int, options: ScanOptions) -> str:
    from ingest_parse.vision import VisionError

    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        text = _vision(path, sha, number, options, kind="table_scan", mode="table_extract").get(_TEXT, "").strip()
    except VisionError:
        text = ""
    if sum(line.lstrip().startswith("|") for line in text.splitlines()) >= 2:
        return text
    alt = f"Таблица {k}, страница {number}"
    return image_markdown(alt, link) if link is not None else f"<!-- {alt}: not extracted -->"


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
