"""TtnResult → Markdown для проверки глазами."""

from __future__ import annotations

import re

from ingest_parse.ttn.extract import PageReport, TtnDoc, TtnResult
from ingest_parse.ttn.schema import Party

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
    out = ["### Товарный раздел", "", "| " + " | ".join(label for _, label in _COLUMNS) + " |",
           "|---" * len(_COLUMNS) + "|"]
    out += ["| " + " | ".join(_cell(getattr(item, k)) for k, _ in _COLUMNS) + " |" for _, item in rows]
    return [*out, ""]


def _totals(doc: TtnDoc) -> list[str]:
    out = ["### Итоги", "", "| Итог | Значение |", "|---|---|"]
    out += [f"| {label} | {_cell(getattr(doc.waybill.totals, key))} |" for key, label in _TOTALS]
    return [*out, ""]


def _demote(markdown: str) -> str:
    """Заголовки страницы — ниже «# Страница N»."""
    return re.sub(r"^(#{1,6})\s", lambda m: "#" * min(6, len(m[1]) + 2) + " ", markdown, flags=re.MULTILINE)


def _heading(result: TtnResult, p: PageReport) -> str:
    doc = result.docs[p.doc - 1] if p.doc else None
    if doc is not None and p.number == doc.pages[0] and p.kind == "front":
        return _title(doc)
    if p.kind == "back":
        return "Оборот"
    if p.kind == "continuation" and doc is not None:
        return "Продолжение"
    return "Документ"


def render(result: TtnResult, link_base: str = "") -> str:
    """Только содержимое страниц подряд; проверки, статусы и ремонт — в JSON."""
    docs = result.docs
    prefix = f"{link_base.rstrip('/')}/" if link_base else ""
    out = [f"# {result.source.name}", ""]
    for p in result.pages:
        out += [f"## Страница {p.number} — {_heading(result, p)}", ""]
        out += [f"![{label}](<{prefix}{path}>)" for label, path in p.images.items()] + [""]
        if p.error:
            out += [f"_страница не обработана: {p.error}_", ""]
            continue
        if p.markdown is not None:
            out += [_demote(p.markdown), ""]
            continue
        doc = docs[p.doc - 1] if p.doc else None
        if doc is None or not result.vision:
            continue
        if p.number == doc.pages[0] and p.kind == "front":
            out += _requisites(doc)
        out += _items(doc, p.copy_of or p.number)
        if p.number == doc.pages[-1]:
            out += _totals(doc)
    return "\n".join(out).rstrip() + "\n"
