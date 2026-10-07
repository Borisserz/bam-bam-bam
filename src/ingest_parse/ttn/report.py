"""TtnResult → Markdown для проверки глазами."""

from __future__ import annotations

from ingest_parse.ttn.extract import TtnResult
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


def render(result: TtnResult, link_base: str = "") -> str:
    w = result.waybill
    title = " ".join(x for x in (w.form or "Накладная", w.series, w.number) if x)
    out = [f"# {title}" + (f" от {w.date}" if w.date else ""), "", f"Файл: `{result.source.name}`", ""]
    errors = [i for i in result.issues if i.level == "error"]
    warns = [i for i in result.issues if i.level == "warning"]
    if not result.vision:
        out += ["**Статус:** без модели (`--no-vision`) — только предобработка и зоны, см. картинки ниже.", ""]
    elif result.ok:
        out += [f"**Статус:** ✓ все проверки пройдены (предупреждений: {len(warns)})", ""]
    else:
        out += [f"**Статус:** ✗ проверить вручную — ошибок: {len(errors)}, предупреждений: {len(warns)}"
                + (f", сбоев модели: {len(result.errors)}" if result.errors else ""), ""]

    if result.vision:
        out += ["## Реквизиты", "", "| Поле | Значение |", "|---|---|"]
        for key, label in _FIELDS:
            value = getattr(w, key)
            out.append(f"| {label} | {_cell(_party(value) if isinstance(value, Party) else value)} |")
        out += ["", "## Товарный раздел", ""]
        out.append("| ✓ | " + " | ".join(label for _, label in _COLUMNS) + " |")
        out.append("|---" * (len(_COLUMNS) + 1) + "|")
        for i, item in enumerate(w.items):
            mark = "✗" if check_item(item, i) else "✓"
            out.append(f"| {mark} | " + " | ".join(_cell(getattr(item, k)) for k, _ in _COLUMNS) + " |")
        out += ["", "## Итоги", "", "| Итог | Значение |", "|---|---|"]
        out += [f"| {label} | {_cell(getattr(w.totals, key))} |" for key, label in _TOTALS]
        out += ["", "## Проверки", ""]
        out += [f"- ✗ {i.message}" for i in errors] + [f"- ⚠ {i.message}" for i in warns]
        if not result.issues:
            out.append("- ✓ реквизиты, арифметика строк, итоги и суммы прописью сходятся")
        if result.repaired:
            out += ["", "Исправлено повторным чтением:", ""] + [f"- {r}" for r in result.repaired]
        if result.errors:
            out += ["", "Сбои модели:", ""] + [f"- {e}" for e in result.errors]
        out.append("")

    out += ["## Страницы", ""]
    prefix = f"{link_base.rstrip('/')}/" if link_base else ""
    for p in result.pages:
        dpi = f"скан {p.native_dpi:.0f} dpi → {p.dpi:.0f} dpi" if p.native_dpi else f"{p.dpi:.0f} dpi (цифровая)"
        out += [f"### Страница {p.number} ({p.kind})", "",
                f"{dpi}; {', '.join(p.steps)}; зоны: {p.zones}; строк по линиям: {p.rows}; кусков таблицы: {p.chunks}", ""]
        out += [f"![{label}](<{prefix}{path}>)" for label, path in p.images.items()] + [""]
    return "\n".join(out).rstrip() + "\n"
