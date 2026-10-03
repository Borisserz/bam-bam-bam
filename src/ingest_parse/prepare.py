"""Копия .docx для Docling: метки номеров списков, индексов и (опционально) фигур."""

from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from ingest_parse.numbering import Numbering, mark_numbering, mark_scripts
from ingest_parse.shapes import Shape, mark_shape_runs, read_part, write_with_document


def prepare_docx(src: Path, dst: Path, *, shapes: bool) -> tuple[bool, list[Shape]]:
    """(dst записан?, фигуры). Номера и индексы — всегда; фигуры — если shapes (их метки ставятся
    в том же порядке, что и в копии для PDF из mark_shapes)."""
    with zipfile.ZipFile(src) as zin:
        root = etree.fromstring(zin.read("word/document.xml"))
        numbering = Numbering(read_part(zin, "word/numbering.xml"), read_part(zin, "word/styles.xml"))
        changed = mark_numbering(root, numbering)
        changed = mark_scripts(root) or changed
        found = mark_shape_runs(root, read_part(zin, "word/_rels/document.xml.rels")) if shapes else []
        if changed or found:
            write_with_document(zin, dst, root)
    return bool(changed or found), found
