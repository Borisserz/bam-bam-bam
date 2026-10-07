"""Сумма прописью («Сто двадцать три рубля 45 копеек») → Decimal."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100, "двести": 200,
    "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800,
    "девятьсот": 900,
}  # fmt: skip
_SCALES = (("тысяч", 1_000), ("миллион", 1_000_000), ("миллиард", 1_000_000_000))


def _scale(token: str) -> int | None:
    return next((value for stem, value in _SCALES if token.startswith(stem)), None)


def words_to_amount(text: str | None) -> Decimal | None:
    """Рубли и копейки словами или цифрами; None — в тексте нет числа."""
    if not text:
        return None
    t = text.lower().replace("ё", "е")
    t = re.sub(r"(?<=\d)[\s\u00a0]+(?=\d{3}\b)", "", t)  # «1 250» — одно число
    total, current, rubles, seen = Decimal(0), Decimal(0), None, False
    for token in re.findall(r"\d+(?:[.,]\d+)?|[а-я]+", t):
        if token[0].isdigit():
            try:
                current += Decimal(token.replace(",", "."))
            except InvalidOperation:
                return None
            seen = True
        elif token in _UNITS:
            current += _UNITS[token]
            seen = True
        elif (scale := _scale(token)) is not None:
            total += (current or 1) * scale
            current, seen = Decimal(0), True
        elif token.startswith("руб") and rubles is None:
            rubles, total, current = total + current, Decimal(0), Decimal(0)
        elif token.startswith("коп"):
            break
    if not seen:
        return None
    if rubles is None:
        return total + current
    return rubles + (total + current) / 100
