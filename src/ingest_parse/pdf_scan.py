"""Страницы-сканы PDF: PNG (pypdfium2) → VLM, иначе OCR, иначе заглушка со ссылкой на PNG."""

from __future__ import annotations

import functools
import hashlib
import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from ingest_parse.media import MediaWriter, image_markdown
from ingest_parse.pdf_triage import SCAN_PLACEHOLDER, PdfPageWarning

OcrLine = tuple[str, float, float, float]  # текст, left, top, bottom (пиксели рендера)

_TEXT = "Текст с изображения"
_REASON = {
    "vision_unavailable_ocr_disabled": "vision and OCR fallback are off",
    "vision_error": "vision API failed: {detail}",
    "vision_empty": "vision API returned no text",
    "ocr_unavailable": "OCR unavailable: {detail}",
    "ocr_error": "OCR failed: {detail}",
    "ocr_empty": "OCR found no text",
}
_NUMBERED = re.compile(r"(\d+(?:\.\d+)*)\.?\s+[A-ZА-ЯЁ]")
_NUMERIC = re.compile(r"[-+−–]?\d+(?:[.,]\d+)?%?(?:\s+[-+−–]?\d+(?:[.,]\d+)?%?)*")


class OcrUnavailable(RuntimeError):
    """Флаг OCR есть, а движка нет (не установлен extra ocr)."""


@dataclass(frozen=True)
class ScanOptions:
    client: Any = None  # VisionClient; None — без --vision в сеть не ходим
    force: bool = False
    max_long_edge: int = 2048
    ocr: bool = False


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
                block = _page_block(pdf, number, media, Path(tmp), options)
                md = md.replace(SCAN_PLACEHOLDER.format(n=number), block, 1)
        finally:
            pdf.close()
    return md


def _page_block(pdf: Any, number: int, media: MediaWriter | None, tmp: Path, options: ScanOptions) -> str:
    try:
        page = pdf[number - 1]
        try:
            image = page.render(scale=2).to_pil()
        finally:
            page.close()
    except (pdfium.PdfiumError, OSError, ValueError) as exc:
        _warn(number, f"placeholder emitted (render failed: {exc})")
        return f"<!-- page {number}: scanned; render failed: {_safe(exc)} -->"
    if media is not None:
        path, link = media.save_scan(image, number)
    else:
        path, link = tmp / f"scan-{number:03d}.png", None
        image.save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    failures: list[tuple[str, str]] = []
    if options.client is not None:
        from ingest_parse.vision import VisionError

        try:
            sections = _vision(path, sha, number, options)
        except VisionError as exc:
            failures.append(("vision_error", str(exc)))
        else:
            body = sections.pop(_TEXT, "").strip()
            if body or sections:
                _warn(number, "rendered + vision")
                return _block(number, "vision", sha, body, sections, link, getattr(options.client, "model", None))
            failures.append(("vision_empty", ""))
    if options.ocr:
        try:
            body = ocr_markdown(_ocr_lines(path))
        except OcrUnavailable as exc:
            failures.append(("ocr_unavailable", str(exc)))
        except Exception as exc:  # движок OCR падает по-разному; страница не должна ронять документ
            failures.append(("ocr_error", f"{type(exc).__name__}: {exc}"))
        else:
            if body:
                _warn(number, "rendered + ocr")
                return _block(number, "ocr", sha, body, {}, link, None)
            failures.append(("ocr_empty", ""))
    return _failure(number, failures or [("vision_unavailable_ocr_disabled", "")], link)


def _vision(path: Path, sha: str, number: int, options: ScanOptions) -> dict[str, str]:
    from ingest_parse.vision.cache import VisionCache
    from ingest_parse.vision.enrich import image_payload
    from ingest_parse.vision.prompts import build_page_prompt, parse_sections

    model = getattr(options.client, "model", None)
    cache = VisionCache(path.parent / ".vision-cache")
    answer = None if options.force else cache.get(sha, "page_extract", model)
    if answer is None:
        prompt = build_page_prompt("text_scan", number)
        answer = options.client.complete(prompt, image_payload(path, max_long_edge=options.max_long_edge))
        cache.put(sha, "page_extract", model, answer)
    return parse_sections(answer)


def _kind(body: str) -> str:
    lines = [line for line in body.splitlines() if line.strip()]
    rows = sum(line.lstrip().startswith("|") for line in lines)
    if rows >= 2 and rows >= 0.8 * len(lines):
        return "table_scan"
    return "diagram" if len(body) < 40 else "text_scan"


def _block(
    number: int, source: str, sha: str, body: str, sections: dict[str, str], link: str | None, model: str | None
) -> str:
    from ingest_parse.vision.markers import render_block

    kind = _kind(body)
    out = [f'<!-- pdf-scan:begin page="{number}" source="{source}" sha256="{sha}" kind="{kind}" -->', ""]
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


def _failure(number: int, failures: list[tuple[str, str]], link: str | None) -> str:
    human = "; ".join(_REASON[code].format(detail=detail) for code, detail in failures)
    hint = "; use --vision or --ocr-fallback" if failures[-1][0] == "vision_unavailable_ocr_disabled" else ""
    _warn(number, f"placeholder emitted ({human}{hint})")
    out = [
        f'<!-- pdf-scan:begin page="{number}" source="none" status="error" reason="{failures[-1][0]}" -->',
        f"<!-- page {number}: scanned; {_safe(human)}{'; see media' if link else ''} -->",
    ]
    if link is not None:
        out += ["", image_markdown(f"Страница {number} (скан)", link)]
    out += ["", f'<!-- pdf-scan:end page="{number}" -->']
    return "\n".join(out)


# --- OCR fallback: RapidOCR (Apache-2.0), extra [ocr] ---


@functools.lru_cache(maxsize=1)
def _rapidocr() -> Any:
    try:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    except ImportError as exc:
        raise OcrUnavailable("RapidOCR is not installed: uv sync --extra ocr") from exc
    # восточнославянская модель PP-OCRv5: кириллица + латиница; бывает только mobile
    params = {
        "Rec.lang_type": LangRec.ESLAV,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Global.log_level": "error",  # иначе INFO о загрузке моделей в stderr на каждый запуск
    }
    return RapidOCR(params=params)


def _ocr_lines(path: Path) -> list[OcrLine]:
    result = _rapidocr()(str(path))
    if result.boxes is None or result.txts is None:
        return []
    lines = []
    for box, text in zip(result.boxes, result.txts, strict=False):
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        if text.strip():
            lines.append((text.strip(), min(xs), min(ys), max(ys)))
    return lines


def _heading(text: str) -> int | None:
    if len(text) > 80 or text.endswith((".", ",", ";", ":")):
        return None
    if m := _NUMBERED.match(text):
        return min(m.group(1).count(".") + 1, 6)
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters):
        return 1
    return None


def _rows(fragments: list[OcrLine]) -> list[OcrLine]:
    """Куски одной строки (OCR режет по широким пробелам) → строка слева направо, по перекрытию по вертикали."""
    rows: list[list[OcrLine]] = []
    for frag in sorted(fragments, key=lambda f: (f[2] + f[3]) / 2):
        center = (frag[2] + frag[3]) / 2
        if rows:
            top, bottom = min(f[2] for f in rows[-1]), max(f[3] for f in rows[-1])
            if top <= center <= bottom:
                rows[-1].append(frag)
                continue
        rows.append([frag])
    out = []
    for row in rows:
        row.sort(key=lambda f: f[1])
        text = " ".join(f[0] for f in row)
        out.append((text, row[0][1], min(f[2] for f in row), max(f[3] for f in row)))
    return out


def ocr_markdown(lines: list[OcrLine]) -> str:
    """Куски OCR → строки → абзацы по вертикальным зазорам и красной строке; «1.2 Тема» / КАПС — заголовки.

    Строки из одних чисел (номера страниц, подписи осей графиков) выбрасываются.
    """
    lines = [row for row in _rows(lines) if not _NUMERIC.fullmatch(row[0])]
    if not lines:
        return ""
    heights = sorted(bottom - top for _, _, top, bottom in lines)
    height = heights[len(heights) // 2] or 1.0
    left_edge = min(left for _, left, _, _ in lines)
    blocks: list[str] = []
    current: list[str] = []
    prev_bottom: float | None = None

    def flush() -> None:
        if current:
            text = current[0]
            for part in current[1:]:
                text = text[:-1] + part if text.endswith("-") and part[:1].islower() else f"{text} {part}"
            blocks.append(text)
            current.clear()

    for text, left, top, bottom in lines:
        level = _heading(text)
        gap = prev_bottom is not None and top - prev_bottom > 0.8 * height
        indent = left > left_edge + 1.2 * height
        if level is not None:
            flush()
            blocks.append(f"{'#' * level} {text}")
        else:
            if gap or indent:
                flush()
            current.append(text)
        prev_bottom = bottom
    flush()
    return "\n\n".join(blocks)
