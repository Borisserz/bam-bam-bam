"""Проверки накладной: реквизиты, арифметика строк, итоги, суммы прописью."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal

from ingest_parse.ttn.schema import Item, Waybill, to_number
from ingest_parse.ttn.words import words_to_amount

_KOPECK = Decimal("0.011")


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    level: str = "error"  # error | warning
    row: int | None = None  # индекс в items


def _fmt(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def check_item(item: Item, row: int | None = None) -> list[Issue]:
    q, p, c = to_number(item.quantity), to_number(item.price), to_number(item.cost)
    rate, vat, total = to_number(item.vat_rate), to_number(item.vat), to_number(item.cost_with_vat)
    label = f"строка {item.n or (row + 1 if row is not None else '?')}"
    issues = []
    if q is not None and p is not None and c is not None:
        tolerance = _KOPECK + abs(q) * Decimal("0.005")  # цена на бланке округлена до копеек
        if abs(q * p - c) > tolerance:
            issues.append(Issue("row_cost", f"{label}: {item.quantity} × {item.price} = {_fmt(q * p)}, а стоимость {item.cost}", row=row))
    if c is not None and rate is not None and vat is not None and abs(c * rate / 100 - vat) > Decimal("0.02"):
        issues.append(Issue("row_vat", f"{label}: НДС {item.vat_rate}% от {item.cost} = {_fmt(c * rate / 100)}, а указано {item.vat}", row=row))
    if c is not None and total is not None and abs(c + (vat or 0) - total) > _KOPECK:
        issues.append(Issue("row_total", f"{label}: {item.cost} + НДС {item.vat or 0} ≠ {item.cost_with_vat}", row=row))
    return issues


def _sum(values: list[str | None]) -> Decimal | None:
    numbers = [to_number(v) for v in values]
    return sum((n for n in numbers if n is not None), Decimal(0)) if any(n is not None for n in numbers) else None


def check_totals(w: Waybill) -> list[Issue]:
    issues = []
    t = w.totals
    for key, name, tolerance in (
        ("cost", "стоимость", _KOPECK),
        ("vat", "НДС", _KOPECK),
        ("cost_with_vat", "стоимость с НДС", _KOPECK),
        ("mass", "масса", Decimal("0.0011")),
        ("places", "грузовые места", Decimal("0.0011")),
    ):
        stated, summed = to_number(getattr(t, key)), _sum([getattr(i, key) for i in w.items])
        if stated is not None and summed is not None and abs(stated - summed) > tolerance:
            issues.append(Issue(f"total_{key}", f"итого {name} {getattr(t, key)}, а сумма строк {_fmt(summed)}"))
    for key, name in (("cost_with_vat", "стоимость с НДС"), ("vat", "НДС")):
        words, stated = getattr(t, f"{key}_words"), to_number(getattr(t, key))
        amount = words_to_amount(words)
        if words and stated is not None and amount is not None and abs(amount - stated) > _KOPECK:
            issues.append(Issue(f"words_{key}", f"{name} прописью «{words}» = {_fmt(amount)}, цифрами {getattr(t, key)}"))
    return issues


def _date_ok(text: str) -> bool:
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})", text.strip())
    if not m:
        return False
    day, month, year = (int(g) for g in m.groups())
    year += 2000 if year < 100 else 0
    try:
        date = dt.date(year, month, day)
    except ValueError:
        return False
    return dt.date(2000, 1, 1) <= date <= dt.date.today() + dt.timedelta(days=31)


def check_requisites(w: Waybill) -> list[Issue]:
    issues = []
    if not w.series:
        issues.append(Issue("series", "серия бланка не прочитана", "warning"))
    elif not re.fullmatch(r"[А-ЯA-Z]{2}", w.series):
        issues.append(Issue("series", f"серия «{w.series}» — ожидаются 2 буквы"))
    if not w.number:
        issues.append(Issue("number", "номер бланка не прочитан", "warning"))
    elif not re.fullmatch(r"\d{7}", w.number):
        issues.append(Issue("number", f"номер «{w.number}» — ожидаются 7 цифр"))
    if not w.date:
        issues.append(Issue("date", "дата не прочитана", "warning"))
    elif not _date_ok(w.date):
        issues.append(Issue("date", f"дата «{w.date}» некорректна"))
    for key, name in (("shipper", "грузоотправителя"), ("consignee", "грузополучателя")):
        unp = getattr(w, key).unp
        if not unp:
            issues.append(Issue(f"{key}_unp", f"УНП {name} не прочитан", "warning"))
        elif not re.fullmatch(r"\d{9}", unp):
            issues.append(Issue(f"{key}_unp", f"УНП {name} «{unp}» — ожидаются 9 цифр"))
    if not w.items:
        issues.append(Issue("items", "товарные строки не найдены"))
    return issues


def validate(w: Waybill) -> list[Issue]:
    issues = check_requisites(w)
    for i, item in enumerate(w.items):
        issues += check_item(item, i)
    return issues + check_totals(w)
