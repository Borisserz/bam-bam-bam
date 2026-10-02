"""Схемы Word без растра (автофигуры, группы, полотна, графики, SmartArt, OLE) → PNG страницы.

Схему не восстанавливаем: в копию .docx перед каждой фигурой ставится невидимая метка. Docling видит
метку в тексте (место в Markdown), LibreOffice рендерит копию в PDF, pypdfium2 находит страницу
с меткой и сохраняет её как page-NNN.png — заготовка под OCR/VLM вне парсера.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lxml import etree

TOKEN = re.compile(r"\s?zqxshape(\d{4})zqx")

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = {
    "w": _W,
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "o": "urn:schemas-microsoft-com:office:office",
    "v": "urn:schemas-microsoft-com:vml",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_RASTER = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff")  # такие картинки Docling отдаёт сам
_VML_SHAPES = "v:rect|v:line|v:oval|v:roundrect|v:polyline|v:arc|v:curve|v:group|v:shape"


@dataclass(frozen=True)
class Shape:
    kind: str  # «Схема», «График», «Формула», «Рисунок», «Объект»
    page: int | None = None
    link: str | None = None


def _q(tag: str) -> str:
    prefix, name = tag.split(":")
    return f"{{{_NS[prefix]}}}{name}"


def _blip_targets(rels_xml: bytes | None) -> dict[str, str]:
    if not rels_xml:
        return {}
    return {rel.get("Id"): rel.get("Target", "") for rel in etree.fromstring(rels_xml).iter(_RELS)}


def _drawing_kind(drawing: etree._Element, targets: dict[str, str]) -> str | None:
    data = drawing.find(".//a:graphicData", _NS)
    uri = data.get("uri", "") if data is not None else ""
    if uri.endswith("/picture"):
        blip = drawing.find(".//a:blip", _NS)
        target = targets.get(blip.get(_q("r:embed"), "") if blip is not None else "", "")
        return None if PurePosixPath(target).suffix.lower() in _RASTER else "Рисунок"
    if uri.endswith("/chart") or "chartex" in uri:
        return "График"
    return "Схема"


def _object_kind(obj: etree._Element) -> str:
    ole = obj.find(".//o:OLEObject", _NS)
    prog = (ole.get("ProgID", "") if ole is not None else "").lower()
    if prog.startswith(("equation", "mathtype")):
        return "Формула"
    if prog.startswith("visio"):
        return "Схема"
    return "Объект"


def _pict_kind(pict: etree._Element) -> str | None:
    shapes = pict.xpath(f".//{_VML_SHAPES.replace('|', ' | .//')}", namespaces=_NS)
    only_image = shapes and all(
        el.tag == _q("v:shape") and el.find("v:imagedata", _NS) is not None and len(el) <= 3
        for el in shapes
    )
    return None if not shapes or only_image else "Схема"


def _kind(el: etree._Element, targets: dict[str, str]) -> str | None:
    if el.tag == _q("mc:AlternateContent"):
        choice = el.find("mc:Choice", _NS)
        inner = choice if choice is not None else el.find("mc:Fallback", _NS)
        found = inner.xpath(".//w:drawing | .//w:pict | .//w:object", namespaces=_NS) if inner is not None else []
        return _kind(found[0], targets) if found else None
    if el.tag == _q("w:drawing"):
        return _drawing_kind(el, targets)
    if el.tag == _q("w:object"):
        return _object_kind(el)
    return _pict_kind(el)


def _marker_run(index: int) -> etree._Element:
    run = etree.Element(_q("w:r"))
    props = etree.SubElement(run, _q("w:rPr"))
    etree.SubElement(props, _q("w:color")).set(_q("w:val"), "FFFFFF")
    etree.SubElement(props, _q("w:sz")).set(_q("w:val"), "2")
    etree.SubElement(run, _q("w:t")).text = f"zqxshape{index:04d}zqx"
    return run


def mark_shapes(src: Path, dst: Path) -> list[Shape]:
    """Копия src → dst с меткой перед каждой фигурой; список фигур по порядку меток."""
    with zipfile.ZipFile(src) as zin:
        root = etree.fromstring(zin.read("word/document.xml"))
        rels = zin.read("word/_rels/document.xml.rels") if "word/_rels/document.xml.rels" in zin.namelist() else None
        targets = _blip_targets(rels)
        shapes: list[Shape] = []
        done: set[etree._Element] = set()
        for el in root.xpath("//mc:AlternateContent | //w:drawing | //w:pict | //w:object", namespaces=_NS):
            if any(anc in done for anc in el.iterancestors()):
                continue
            done.add(el)
            kind = _kind(el, targets)
            run = next((anc for anc in el.iterancestors(_q("w:r"))), None)
            if kind is None or run is None:
                continue
            run.addprevious(_marker_run(len(shapes)))
            shapes.append(Shape(kind))
        if not shapes:
            return []
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info)
                if info.filename == "word/document.xml":
                    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                zout.writestr(info, data)
    return shapes


def render_pages(pdf: Path, shapes: list[Shape], media: object) -> list[Shape]:
    """Страница каждой метки в PDF; нужные страницы → media.save_page(); фигуры с page/link."""
    import pypdfium2 as pdfium

    pages: dict[int, int] = {}
    links: dict[int, str] = {}
    doc = pdfium.PdfDocument(str(pdf))
    try:
        for number, page in enumerate(doc, start=1):
            try:
                textpage = page.get_textpage()
                text = textpage.get_text_range()
                textpage.close()
                found = [int(i) for i in TOKEN.findall(text)]
                for index in found:
                    pages.setdefault(index, number)
                if found and number not in links:
                    links[number] = media.save_page(page.render(scale=2).to_pil(), number)
            finally:
                page.close()  # Windows: открытый PDF не даст удалить временную папку
    finally:
        doc.close()
    return [
        Shape(s.kind, pages.get(i), links.get(pages.get(i, -1))) for i, s in enumerate(shapes)
    ]
