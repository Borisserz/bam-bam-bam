"""HTML-комментарии vision:begin / vision:end вокруг описания картинки."""

from __future__ import annotations

import re

from ingest_parse.vision.prompts import SECTIONS

BLOCK = re.compile(
    r'<!-- vision:begin id="(?P<id>[^"]+)"(?P<attrs>[^>]*?)-->\n.*?<!-- vision:end id="(?P=id)" -->\n(?:\n)?',
    re.S,
)


def _safe(value: str) -> str:
    return value.replace("-->", "–>").replace('"', "'")


_FENCE = re.compile(r"^\s*```")
_PIPES = re.compile(r"^\s*\|[\s|]*$")


def settle_markdown(text: str) -> str:
    """Снимает незакрытый ``` и схлопывает простыню из пустых | | |. Настоящая таблица с текстом в клетках остаётся."""
    kept: list[str] = []
    run = 0
    for line in text.splitlines():
        if _FENCE.match(line):
            run = 0
            continue
        if _PIPES.match(line):
            run += 1
            if run <= 3:
                kept.append(line)
            continue
        run = 0
        kept.append(line)
    return "\n".join(kept).strip()


def render_block(
    image_id: str, sha256: str, kind: str, model: str | None, sections: dict[str, str], error: str | None = None
) -> str:
    attrs = f'id="{_safe(image_id)}" sha256="{sha256}" kind="{kind}" model="{_safe(model or "-")}"'
    if error is not None:
        attrs += ' status="error"'
        sections = {"Описание": f"не получено (ошибка vision API: {error})"}
    lines = [f"<!-- vision:begin {attrs} -->"]
    for name in SECTIONS:
        value = sections.get(name)
        if not value:
            continue
        value = settle_markdown(value.replace("-->", "–>"))
        if not value:
            continue
        lines.append(f"**{name}:** {value}" if "\n" not in value else f"**{name}:**\n\n{value}\n")
    lines.append(f'<!-- vision:end id="{_safe(image_id)}" -->')
    return "\n".join(lines) + "\n\n"
