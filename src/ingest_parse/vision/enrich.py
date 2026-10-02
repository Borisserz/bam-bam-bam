"""Markdown + media/ → блок описания VLM над каждой ![…](…)."""

from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import re
from pathlib import Path
from typing import Protocol

from PIL import Image

from ingest_parse.vision.cache import VisionCache
from ingest_parse.vision.client import VisionError
from ingest_parse.vision.markers import BLOCK, render_block
from ingest_parse.vision.prompts import build_prompt, parse_sections
from ingest_parse.vision.taxonomy import image_kind, prompt_mode

_IMAGE = re.compile(r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]\((?:<(?P<angle>[^>]+)>|(?P<plain>[^)\s]+))\)")


class Completer(Protocol):
    model: str | None

    def complete(self, prompt: str, image_url: str) -> str: ...


def image_payload(path: Path, *, max_long_edge: int) -> str:
    """data:URL; длинная сторона > max_long_edge → уменьшенная копия в памяти, файл не меняется."""
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if max(image.size) > max_long_edge:
                image.thumbnail((max_long_edge, max_long_edge))
                buf = io.BytesIO()
                if image.mode not in ("RGB", "L", "RGBA"):
                    image = image.convert("RGBA")
                image.save(buf, format="PNG")
                raw, mime = buf.getvalue(), "image/png"
    except (OSError, ValueError):
        pass
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _caption(alt: str) -> str | None:
    alt = re.sub(r"\\(.)", r"\1", alt).strip()
    return None if alt in ("", "image") else alt


def _anchor(lines: list[str], i: int) -> int:
    """Картинка в строке таблицы → блок над всей таблицей."""
    if not lines[i].lstrip().startswith("|"):
        return i
    while i > 0 and lines[i - 1].lstrip().startswith("|"):
        i -= 1
    return i


def enrich_markdown(
    md: str,
    *,
    base_dir: str | Path,
    client: Completer,
    force: bool = False,
    max_long_edge: int = 2048,
) -> str:
    """Над первой ссылкой на каждый файл — <!-- vision:begin … --> … <!-- vision:end … -->.

    Уже описанные картинки пропускаются (force — описать заново, мимо кэша); блоки с ошибкой
    переписываются при следующем запуске; ошибка API у одной картинки не мешает остальным.
    """
    base = Path(base_dir)
    done = {
        m.group("id")
        for m in BLOCK.finditer(md)
        if not force and 'status="error"' not in m.group("attrs")
    }
    md = BLOCK.sub(lambda m: m.group(0) if m.group("id") in done else "", md)

    lines = md.split("\n")
    inserts: dict[int, list[str]] = {}
    for i, line in enumerate(lines):
        for m in _IMAGE.finditer(line):
            link = m.group("angle") or m.group("plain")
            path = base / link
            if "://" in link or not path.is_file() or path.stem in done:
                continue
            done.add(path.stem)
            block = _describe(path, m.group("alt"), client, force, max_long_edge)
            inserts.setdefault(_anchor(lines, i), []).append(block)

    out: list[str] = []
    for i, line in enumerate(lines):
        for block in inserts.get(i, []):
            out.extend(block.rstrip("\n").split("\n") + [""])
        out.append(line)
    return "\n".join(out)


def _describe(path: Path, alt: str, client: Completer, force: bool, max_long_edge: int) -> str:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    kind = image_kind(path, _caption(alt) or "image")
    mode = prompt_mode(kind)
    cache = VisionCache(path.parent / ".vision-cache")
    model = getattr(client, "model", None)
    answer = None if force else cache.get(sha, mode, model)
    if answer is None:
        try:
            answer = client.complete(build_prompt(mode, kind, _caption(alt)), image_payload(path, max_long_edge=max_long_edge))
        except VisionError as exc:
            return render_block(path.stem, sha, kind, model, {}, error=str(exc))
        cache.put(sha, mode, model, answer)
    sections = parse_sections(answer)
    if mode == "describe_only":
        sections = {"Описание": sections.get("Описание") or answer.strip()}
    return render_block(path.stem, sha, kind, model, sections)
