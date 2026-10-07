"""Поля ТН-2 / ТТН-1. Значения — строки как на бланке; числа — to_number при проверке."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal, InvalidOperation
from typing import Any

_LATIN_TO_CYR = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")


@dataclass
class Party:
    name: str | None = None
    unp: str | None = None
    address: str | None = None


@dataclass
class Item:
    n: str | None = None
    name: str | None = None
    unit: str | None = None
    quantity: str | None = None
    price: str | None = None
    cost: str | None = None
    vat_rate: str | None = None
    vat: str | None = None
    cost_with_vat: str | None = None
    places: str | None = None
    mass: str | None = None
    note: str | None = None
    page: int | None = None


@dataclass
class Totals:
    quantity: str | None = None
    cost: str | None = None
    vat: str | None = None
    cost_with_vat: str | None = None
    places: str | None = None
    mass: str | None = None
    vat_words: str | None = None
    cost_with_vat_words: str | None = None
    mass_words: str | None = None
    places_words: str | None = None


@dataclass
class Waybill:
    form: str | None = None  # ТН-2 | ТТН-1
    series: str | None = None
    number: str | None = None
    date: str | None = None
    shipper: Party = field(default_factory=Party)
    consignee: Party = field(default_factory=Party)
    carrier_customer: Party = field(default_factory=Party)  # ТТН-1: заказчик перевозки
    basis: str | None = None
    loading_point: str | None = None
    unloading_point: str | None = None
    vehicle: str | None = None
    trailer: str | None = None
    waybill: str | None = None  # к путевому листу №
    driver: str | None = None
    released_by: str | None = None
    handed_by: str | None = None
    accepted_by: str | None = None
    power_of_attorney: str | None = None
    seal: str | None = None
    items: list[Item] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


HEADER_FIELDS = [f.name for f in fields(Waybill) if f.name not in ("items", "totals")]
PARTY_FIELDS = ("shipper", "consignee", "carrier_customer")
ITEM_FIELDS = [f.name for f in fields(Item) if f.name != "page"]
TOTAL_FIELDS = [f.name for f in fields(Totals)]


def to_number(raw: Any) -> Decimal | None:
    """«1 234,56» / «1.234,56» / «20%» / «150,00 руб.» → Decimal; не число — None."""
    if raw is None:
        return None
    s = str(raw).replace("\u00a0", " ").strip().lower()
    s = re.sub(r"руб\.?|коп\.?|byn|%", "", s).replace(" ", "")
    if not re.fullmatch(r"-?[\d.,]*\d[\d.,]*", s):
        return None
    if "," in s and "." in s:
        cut = max(s.rfind(","), s.rfind("."))
        s = re.sub(r"[.,]", "", s[:cut]) + "." + s[cut + 1 :]
    elif s.count(",") == 1:
        s = s.replace(",", ".")
    elif s.count(",") > 1 or s.count(".") > 1:
        s = s.replace(",", "").replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None if text.lower() not in ("null", "none", "-", "—", "нет") else None


def _party(value: Any) -> Party:
    if isinstance(value, str):
        return Party(name=_text(value))
    if not isinstance(value, dict):
        return Party()
    unp = _text(value.get("unp"))
    return Party(_text(value.get("name")), re.sub(r"\s", "", unp) if unp else None, _text(value.get("address")))


def item_from_dict(data: dict[str, Any], page: int | None = None) -> Item:
    return Item(**{k: _text(data.get(k)) for k in ITEM_FIELDS}, page=page)


def totals_from_dict(data: dict[str, Any]) -> Totals:
    return Totals(**{k: _text(data.get(k)) for k in TOTAL_FIELDS})


def waybill_from_dict(data: dict[str, Any]) -> Waybill:
    """Терпимо к лишним ключам, числам вместо строк и мусору вида «№ 0012345»."""
    w = Waybill(**{k: _text(data.get(k)) for k in HEADER_FIELDS if k not in PARTY_FIELDS})
    for k in PARTY_FIELDS:
        setattr(w, k, _party(data.get(k)))
    if w.series:
        w.series = re.sub(r"[\s.]", "", w.series).upper().translate(_LATIN_TO_CYR)
    if w.number:
        w.number = re.sub(r"[^\w]", "", w.number.replace("№", ""))
    w.items = [item_from_dict(i) for i in data.get("items") or [] if isinstance(i, dict)]
    if isinstance(data.get("totals"), dict):
        w.totals = totals_from_dict(data["totals"])
    return w
