"""TtnResult → Markdown для проверки глазами."""

from __future__ import annotations

import re

from ingest_parse.ttn.extract import PageReport, TtnDoc, TtnResult
from ingest_parse.ttn.schema import Party
from ingest_parse.ttn.validate import check_item

_FIELDS = (
    ("form", "Вид бланка"), ("series", "Серия"), ("number", "Номер"), ("date", "Дата"),
    ("shipper", "Грузоотправитель"), ("consignee", "Грузополучатель"), ("carrier_customer", "Заказчик перевозки"),
    ("basis", "Основание отпуска"), ("loading_point", "Пункт погрузки"), ("unloading_point", "Пункт разгрузки"),
    ("vehicle", "Автомобиль"), ("trailer", "Прицеп"), ("waybill", "Путевой лист"), ("driver", "Водитель"),
    ("released_by", "Отпуск разрешил"), ("handed_by", "Сдал грузоотправитель"), ("accepted_by", "Принял"),
    ("power_of_attorney", "Доверенность"), ("seal", "Пломба"),
)  # fmt: skip
_COLUMNS = (
    ("n", "№"), ("name", "Наименование"), ("unit", "Ед."), ("quantity", "Кол-во"), ("price", "Цена"),
    ("cost", "Стоимость"), ("vat_rate", "НДС %"), ("vat", "НДС"), ("cost_with_vat", "С НДС"),
    ("places", "Мест"), ("mass", "Масса"), ("note", "Примечание"),
)  # fmt: skip
_TOTALS = (
    ("quantity", "Количество"), ("cost", "Стоимость"), ("vat", "Сумма НДС"), ("cost_with_vat", "Стоимость с НДС"),
    ("places", "Грузовых мест"), ("mass", "Масса"), ("vat_words", "НДС прописью"),
    ("cost_with_vat_words", "Стоимость с НДС прописью"), ("mass_words", "Масса прописью"),
    ("places_words", "Мест прописью"),
)  # fmt: skip


def _cell(value: object) -> str:
    return "—" if value in (None, "") else str(value).replace("|", "\\|").replace("\n", " ")


def _party(p: Party) -> str | None:
    parts = [p.name, f"УНП {p.unp}" if p.unp else None, p.address]
    return ", ".join(x for x in parts if x) or None


def _title(doc: TtnDoc) -> str:
    w = doc.waybill
    return " ".join(x for x in (w.form or "Накладная", w.series, w.number) if x) + (f" от {w.date}" if w.date else "")


def _status(doc: TtnDoc) -> str:
    errors = sum(i.level == "error" for i in doc.issues)
    return "✓" if not errors else f"✗ ошибок: {errors}"


def _requisites(doc: TtnDoc) -> list[str]:
    out = ["### Реквизиты", "", "| Поле | Значение |", "|---|---|"]
    for key, label in _FIELDS:
        value = getattr(doc.waybill, key)
        out.append(f"| {label} | {_cell(_party(value) if isinstance(value, Party) else value)} |")
    return [*out, ""]


def _items(doc: TtnDoc, page: int) -> list[str]:
    rows = [(i, item) for i, item in enumerate(doc.waybill.items) if item.page == page]
    if not rows:
        return ["### Товарный раздел", "", "_строк товаров на странице не прочитано_", ""]
    out = ["### Товарный раздел", "", "| ✓ | " + " | ".join(label for _, label in _COLUMNS) + " |",
           "|---" * (len(_COLUMNS) + 1) + "|"]
    for i, item in rows:
        mark = "✗" if check_item(item, i) else "✓"
        out.append(f"| {mark} | " + " | ".join(_cell(getattr(item, k)) for k, _ in _COLUMNS) + " |")
    return [*out, ""]


def _checks(doc: TtnDoc, k: int) -> list[str]:
    out = [f"### Итоги накладной {k}", "", "| Итог | Значение |", "|---|---|"]
    out += [f"| {label} | {_cell(getattr(doc.waybill.totals, key))} |" for key, label in _TOTALS]
    out += ["", f"### Проверки накладной {k}", ""]
    out += [f"- ✗ {i.message}" for i in doc.issues if i.level == "error"]
    out += [f"- ⚠ {i.message}" for i in doc.issues if i.level == "warning"]
    if not doc.issues:
        out.append("- ✓ реквизиты, арифметика строк, итоги и суммы прописью сходятся")
    if doc.repaired:
        out += ["", "Исправлено повторным чтением:", ""] + [f"- {r}" for r in doc.repaired]
    return [*out, ""]


def _demote(markdown: str) -> str:
    """Заголовки страницы — ниже «## Страница N»."""
    return re.sub(r"^(#{1,6})\s", lambda m: "#" * min(6, len(m[1]) + 3) + " ", markdown, flags=re.MULTILINE)


def _label(result: TtnResult, p: PageReport) -> str:
    doc = result.docs[p.doc - 1] if p.doc else None
    if p.error:
        return "сбой обработки"
    if not result.vision:
        return "без модели"
    if p.kind == "back":
        return f"оборот накладной {p.doc}" if p.doc else "оборот накладной"
    if doc is None:
        return "другой документ"
    if p.number == doc.pages[0]:
        return f"{_title(doc)} (накладная {p.doc})"
    return f"продолжение накладной {p.doc}"


def render(result: TtnResult, link_base: str = "") -> str:
    docs = result.docs
    out = [f"# {result.source.name}: накладных {len(docs)}, страниц {len(result.pages)}", ""]
    failed = [p.number for p in result.pages if p.error]
    if not result.vision:
        out += ["**Статус:** без модели (`--no-vision`) — только предобработка и разметка, см. картинки ниже.", ""]
    elif result.ok:
        out += ["**Статус:** ✓ все проверки пройдены", ""]
    else:
        bad = [str(k) for k, d in enumerate(docs, 1) if not d.ok]
        parts = [f"накладные {', '.join(bad)}" if bad else "", f"сбой страниц {', '.join(map(str, failed))}" if failed else "",
                 f"сбоев модели: {len(result.errors)}" if result.errors else ""]
        out += ["**Статус:** ✗ проверить вручную — " + "; ".join(x for x in parts if x), ""]
    if docs:
        out += ["| № | Накладная | Страницы | Строк | Проверки |", "|---|---|---|---|---|"]
        for k, d in enumerate(docs, 1):
            pages = ", ".join(map(str, d.pages))
            out.append(f"| {k} | {_cell(_title(d))} | {pages} | {len(d.waybill.items)} | {_status(d) if result.vision else '—'} |")
        out.append("")
    if result.errors:
        out += ["Сбои модели:", ""] + [f"- {e}" for e in result.errors] + [""]

    prefix = f"{link_base.rstrip('/')}/" if link_base else ""
    for p in result.pages:
        out += [f"## Страница {p.number} — {_label(result, p)}", ""]
        dpi = f"скан {p.native_dpi:.0f} dpi → {p.dpi:.0f} dpi" if p.native_dpi else f"{p.dpi:.0f} dpi (цифровая)"
        out += [f"_{dpi}; {', '.join(p.steps) or '—'}; зоны: {p.zones}; строк по линиям: {p.rows}_", ""]
        out += [f"![{label}](<{prefix}{path}>)" for label, path in p.images.items()] + [""]
        if p.error:
            out += [f"**сбой обработки страницы:** `{p.error}`", ""]
            continue
        if p.markdown is not None:
            out += [_demote(p.markdown), ""]
            continue
        doc = docs[p.doc - 1] if p.doc else None
        if doc is None or not result.vision:
            continue
        if p.number == doc.pages[0] and p.kind == "front":
            out += _requisites(doc)
        if p.copy_of:
            out += [f"_копия страницы {p.copy_of}: те же строки товаров, в накладную второй раз не добавлены_", ""]
        out += _items(doc, p.copy_of or p.number)
        if p.number == doc.pages[-1]:
            out += _checks(doc, p.doc)
    return "\n".join(out).rstrip() + "\n"
