"""Страницы-сканы PDF: PNG (pypdfium2) → VLM (--vision), иначе заглушка со ссылкой на PNG."""

from __future__ import annotations

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

_TEXT = "Текст с изображения"
_REASON = {
    "vision_off": "vision is off",
    "vision_error": "vision API failed: {detail}",
    "vision_empty": "vision API returned no text",
}


@dataclass(frozen=True)
class ScanOptions:
    client: Any = None  # VisionClient; None — без --vision в сеть не ходим
    force: bool = False
    max_long_edge: int = 2048


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

    if options.client is None:
        return _failure(number, "vision_off", "", link)
    from ingest_parse.vision import VisionError

    try:
        sections = _vision(path, sha, number, options)
    except VisionError as exc:
        return _failure(number, "vision_error", str(exc), link)
    body = sections.pop(_TEXT, "").strip()
    if not (body or sections):
        return _failure(number, "vision_empty", "", link)
    _warn(number, "rendered + vision")
    return _block(number, sha, body, sections, link, getattr(options.client, "model", None))


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


def _block(number: int, sha: str, body: str, sections: dict[str, str], link: str | None, model: str | None) -> str:
    from ingest_parse.vision.markers import render_block

    kind = _kind(body)
    out = [f'<!-- pdf-scan:begin page="{number}" source="vision" sha256="{sha}" kind="{kind}" -->', ""]
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
