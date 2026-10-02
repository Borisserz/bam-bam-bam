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
        value = value.replace("-->", "–>")
        lines.append(f"**{name}:** {value}" if "\n" not in value else f"**{name}:**\n\n{value}\n")
    lines.append(f'<!-- vision:end id="{_safe(image_id)}" -->')
    return "\n".join(lines) + "\n\n"
