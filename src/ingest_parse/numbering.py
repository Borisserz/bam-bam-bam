"""Настоящие номера списков Word и верхние/нижние индексы — через метки в копии .docx.

Docling считает номера сам («1.», «2.» внутри группы) и срезает пробелы на краях run'ов, поэтому:
- номер абзаца («3.2.1.», «а)», «IV.») считается здесь по numbering.xml и пишется в начало текста
  абзаца меткой zqxnum<hex>zqx (hex — UTF-8 номера; пусто — маркер-точка);
- у run'а с индексом снимается vertAlign, текст оборачивается в zqxsupzqx…zqxendzqx — Docling склеивает
  его с соседями вместе с пробелами, а метки потом становятся <sup>/<sub>.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_NS = {"w": _W, "mc": _MC}

NUM = re.compile(r"zqxnum([0-9a-f]*)zqx[ \t]?")
SCRIPT = re.compile(r"zqx(sup|sub)zqx(.*?)zqxendzqx", re.S)
_ANY = re.compile(r"zqx(?:num[0-9a-f]*|sup|sub|end)zqx[ \t]?")
_SIMPLE = re.compile(r"\d+[.)]")
_RU = "абвгдежзиклмнопрстуфхцчшщэюя"
_ROMAN = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
          (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))


def _q(name: str) -> str:
    return f"{{{_W}}}{name}"


def _val(el: etree._Element | None, child: str) -> str | None:
    found = el.find(f"w:{child}", _NS) if el is not None else None
    return found.get(_q("val")) if found is not None else None


# --- разметка в тексте ---------------------------------------------------------


def marker_of(text: str) -> tuple[str | None, str]:
    """(номер | "" для маркера-точки | None, текст без меток номера)."""
    m = NUM.search(text)
    if m is None:
        return None, text
    return bytes.fromhex(m.group(1)).decode("utf-8"), NUM.sub("", text)


def list_bullet(marker: str | None) -> str:
    """Начало строки списка: «3.» как есть, «а)» / «1.2.» → «- а)», маркер-точка → «-»."""
    if not marker:
        return "-"
    return marker if _SIMPLE.fullmatch(marker) else f"- {marker}"


def render_scripts(text: str) -> str:
    def tag(m: re.Match[str]) -> str:
        body = m.group(2)
        return body if not body.strip() else f"<{m.group(1)}>{body}</{m.group(1)}>"

    out = SCRIPT.sub(tag, text)
    return re.sub(r"</(sup|sub)>(\s*)<\1>", r"\2", out)  # x<sub>i</sub><sub> – </sub> → x<sub>i – </sub>


def plain(text: str) -> str:
    """Без меток вообще (для распознавания заголовков)."""
    return _ANY.sub("", text)


def finalize(text: str) -> str:
    """Остатки меток (ячейки таблиц, подписи) → номер + пробел / <sup>…</sup>."""

    def number(m: re.Match[str]) -> str:
        marker = bytes.fromhex(m.group(1)).decode("utf-8")
        return f"{marker} " if marker else ""

    return render_scripts(NUM.sub(number, text))


# --- numbering.xml -------------------------------------------------------------


@dataclass
class _Level:
    start: int = 1
    fmt: str = "decimal"
    text: str = "%1."
    restart: int | None = None
    legal: bool = False


@dataclass
class _Num:
    abstract: str
    starts: dict[int, int] = field(default_factory=dict)
    levels: dict[int, _Level] = field(default_factory=dict)


def _parse_level(lvl: etree._Element, base: _Level | None = None) -> _Level:
    level = _Level(**vars(base)) if base else _Level()
    if (v := _val(lvl, "start")) is not None and v.lstrip("-").isdigit():
        level.start = int(v)
    if (v := _val(lvl, "numFmt")) is not None:
        level.fmt = v
    if (v := _val(lvl, "lvlText")) is not None:
        level.text = v
    if (v := _val(lvl, "lvlRestart")) is not None and v.isdigit():
        level.restart = int(v)
    if lvl.find("w:isLgl", _NS) is not None:
        level.legal = True
    return level


def _letters(n: int, alphabet: str) -> str:
    n = max(n, 1)
    return alphabet[(n - 1) % len(alphabet)] * ((n - 1) // len(alphabet) + 1)


def _roman(n: int) -> str:
    out = ""
    for value, sym in _ROMAN:
        while n >= value:
            out, n = out + sym, n - value
    return out


def _format(n: int, fmt: str) -> str:
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt == "lowerLetter":
        return _letters(n, "abcdefghijklmnopqrstuvwxyz")
    if fmt == "upperLetter":
        return _letters(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    if fmt == "russianLower":
        return _letters(n, _RU)
    if fmt == "russianUpper":
        return _letters(n, _RU.upper())
    if fmt == "none":
        return ""
    return str(n)


class Numbering:
    def __init__(self, numbering_xml: bytes | None, styles_xml: bytes | None) -> None:
        self.abstract: dict[str, dict[int, _Level]] = {}
        self.links: dict[str, str] = {}  # abstractNum с numStyleLink → styleId
        self.nums: dict[str, _Num] = {}
        self.styles: dict[str, tuple[str | None, str | None, str | None]] = {}  # id → numId, ilvl, basedOn
        self.default_style: str | None = None
        self.counters: dict[str, list[int | None]] = {}
        self.seen: set[str] = set()
        if numbering_xml:
            root = etree.fromstring(numbering_xml)
            for ab in root.findall("w:abstractNum", _NS):
                aid = ab.get(_q("abstractNumId"))
                levels = {int(l.get(_q("ilvl"), "0")): _parse_level(l) for l in ab.findall("w:lvl", _NS)}
                self.abstract[aid] = levels
                if (link := _val(ab, "numStyleLink")) is not None:
                    self.links[aid] = link
            for num in root.findall("w:num", _NS):
                entry = _Num(_val(num, "abstractNumId") or "")
                for override in num.findall("w:lvlOverride", _NS):
                    ilvl = int(override.get(_q("ilvl"), "0"))
                    if (v := _val(override, "startOverride")) is not None and v.isdigit():
                        entry.starts[ilvl] = int(v)
                    if (lvl := override.find("w:lvl", _NS)) is not None:
                        entry.levels[ilvl] = _parse_level(lvl)
                self.nums[num.get(_q("numId"))] = entry
        if styles_xml:
            for style in etree.fromstring(styles_xml).findall("w:style", _NS):
                if style.get(_q("type")) != "paragraph":
                    continue
                sid = style.get(_q("styleId"))
                num_pr = style.find("w:pPr/w:numPr", _NS)
                self.styles[sid] = (_val(num_pr, "numId"), _val(num_pr, "ilvl"), _val(style, "basedOn"))
                if style.get(_q("default")) in ("1", "true"):
                    self.default_style = sid

    def _style_num(self, sid: str | None) -> tuple[str | None, str | None]:
        num_id = ilvl = None
        for _ in range(20):
            if sid is None or sid not in self.styles:
                break
            s_num, s_ilvl, based = self.styles[sid]
            num_id = num_id or s_num
            ilvl = ilvl or s_ilvl
            sid = based
        return num_id, ilvl

    def _levels(self, num: _Num) -> tuple[str, dict[int, _Level]]:
        aid = num.abstract
        if aid in self.links:  # список описан через стиль нумерации
            linked, _ = self._style_num(self.links[aid])
            if linked in self.nums:
                aid = self.nums[linked].abstract
        levels = dict(self.abstract.get(aid, {}))
        levels.update(num.levels)
        return aid, levels

    def marker(self, p: etree._Element) -> str | None:
        """Номер абзаца как его показывает Word; "" — маркер-точка; None — абзац без нумерации."""
        ppr = p.find("w:pPr", _NS)
        num_pr = ppr.find("w:numPr", _NS) if ppr is not None else None
        num_id, ilvl = _val(num_pr, "numId"), _val(num_pr, "ilvl")
        if num_id is None or ilvl is None:
            s_num, s_ilvl = self._style_num(_val(ppr, "pStyle") or self.default_style)
            num_id, ilvl = num_id or s_num, ilvl or s_ilvl
        if num_id in (None, "0") or num_id not in self.nums:
            return None
        num = self.nums[num_id]
        aid, levels = self._levels(num)
        level_no = int(ilvl or 0)
        level = levels.get(level_no)
        if level is None:
            return None
        counters = self.counters.setdefault(aid, [None] * 9)
        if num_id not in self.seen:
            self.seen.add(num_id)
            for lvl_no, start in num.starts.items():
                counters[lvl_no] = start - 1
        current = counters[level_no]
        counters[level_no] = level.start if current is None else current + 1
        for deeper in range(level_no + 1, 9):
            restart = levels[deeper].restart if deeper in levels else None
            if restart is None or (restart and level_no < restart):
                counters[deeper] = None
        if level.fmt == "bullet":
            return ""

        def sub(m: re.Match[str]) -> str:
            k = int(m.group(1)) - 1
            src = levels.get(k, _Level())
            value = counters[k] if counters[k] is not None else src.start
            return _format(value, "decimal" if level.legal else src.fmt)

        return re.sub(r"%([1-9])", sub, level.text).strip()


# --- разметка копии document.xml --------------------------------------------------


def _first_text_run(p: etree._Element) -> etree._Element | None:
    runs = p.xpath(
        "./w:r[w:t] | ./w:hyperlink/w:r[w:t] | ./w:ins/w:r[w:t] | ./w:smartTag/w:r[w:t]"
        " | ./w:sdt/w:sdtContent/w:r[w:t] | ./w:fldSimple/w:r[w:t]",
        namespaces=_NS,
    )
    return runs[0] if runs else None


def mark_numbering(root: etree._Element, numbering: Numbering) -> bool:
    changed = False
    for p in root.iter(_q("p")):
        if any(anc.tag == f"{{{_MC}}}Fallback" for anc in p.iterancestors()):
            continue  # дубль содержимого mc:Choice
        marker = numbering.marker(p)
        if marker is None:
            continue
        token = f"zqxnum{marker.encode('utf-8').hex()}zqx"
        run = _first_text_run(p)
        if run is None:
            run = etree.SubElement(p, _q("r"))
            etree.SubElement(run, _q("t"))
        t = run.find("w:t", _NS)
        t.text = token + (t.text or "")
        changed = True
    return changed


def mark_scripts(root: etree._Element) -> bool:
    changed = False
    for align in root.iter(_q("vertAlign")):
        kind = {"superscript": "sup", "subscript": "sub"}.get(align.get(_q("val"), ""))
        rpr = align.getparent()
        run = rpr.getparent() if rpr is not None else None
        if kind is None or run is None or run.tag != _q("r"):
            continue
        texts = run.findall("w:t", _NS)
        if not texts or not "".join(t.text or "" for t in texts).strip():
            continue  # ссылки на сноски и пустые run'ы
        rpr.remove(align)
        texts[0].text = f"zqx{kind}zqx" + (texts[0].text or "")
        texts[-1].text = (texts[-1].text or "") + "zqxendzqx"
        changed = True
    return changed
