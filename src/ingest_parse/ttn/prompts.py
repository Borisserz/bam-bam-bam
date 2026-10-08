"""Промпты VLM для ТН-2 / ТТН-1 и разбор JSON из ответа."""

from __future__ import annotations

import json
import re
from typing import Any

_RULES = (
    "Правила: переписывай значения ТОЧНО как на бланке — не исправляй, не вычисляй, не дописывай; "
    "числа — как напечатаны или написаны (с запятой: «1 234,56»); пустое или нечитаемое поле — null. "
    "Рукописные значения тоже переписывай. Ответ — только JSON, без пояснений и без ```."
)

_PARTY = '{{"name": …, "unp": "9 цифр", "address": …}}'  # фигурные скобки удвоены под .format

HEADER = (
    "Это верхняя часть белорусской товарной накладной ТН-2 или товарно-транспортной накладной ТТН-1 "
    "(скан, страница {page}).\n"
    "Подсказки: серия (2 буквы) и номер (7 цифр) бланка напечатаны типографски, обычно справа вверху "
    "(например «ЕВ 1234567»); УНП (9 цифр) — в клетках вверху у грузоотправителя, грузополучателя и "
    "заказчика перевозки; дата — рядом с номером или в строке «Дата».\n"
    f"{_RULES}\n"
    "JSON:\n"
    '{{"page_kind": "front — лицевая с реквизитами | continuation — продолжение товарного раздела | '
    'back — оборот (раздел II, погрузочно-разгрузочные операции) | other",\n'
    ' "form": "ТН-2 | ТТН-1 | null", "series": …, "number": …, "date": "ДД.ММ.ГГГГ",\n'
    f' "shipper": {_PARTY}, "consignee": {_PARTY}, "carrier_customer": {_PARTY},\n'
    ' "basis": "основание отпуска", "loading_point": …, "unloading_point": …,\n'
    ' "vehicle": "марка и гос. номер автомобиля", "trailer": …, "waybill": "№ путевого листа", "driver": …}}'
)

FIELDS = (
    "Это поля формы белорусской накладной ТН-2/ТТН-1, вырезанные из шапки и сложенные стопкой (страница {page}): "
    "в каждой полосе слева — название поля («Грузоотправитель», «Грузополучатель», «Основание отпуска», "
    "«Пункт погрузки», «Автомобиль», «Водитель» …), правее — его значение; полосы разделены серой чертой.\n"
    "Мелкие подписи под линиями («наименование, адрес») — не значения.\n"
    f"{_RULES}\n"
    "JSON (поле, которого нет на картинке, — null):\n"
    f'{{{{"shipper": {_PARTY}, "consignee": {_PARTY}, "carrier_customer": {_PARTY},\n'
    ' "basis": "основание отпуска", "loading_point": …, "unloading_point": …,\n'
    ' "vehicle": "марка и гос. номер автомобиля", "trailer": …, "waybill": "№ путевого листа", "driver": …}}'
)

ITEMS = (
    "Это фрагмент таблицы «I. ТОВАРНЫЙ РАЗДЕЛ» белорусской накладной ТН-2/ТТН-1: сверху — шапка таблицы "
    "(названия и номера столбцов), под серой полосой — строки товаров (страница {page}).\n"
    "Перепиши КАЖДУЮ строку товара по столбцам. Название, перенесённое на следующую строку таблицы, — часть "
    "той же строки. Строку с номерами столбцов «1 2 3 …» не включай. Строку «Итого» не включай в items — "
    "верни её в totals (если её нет на фрагменте — null).\n"
    f"{_RULES}\n"
    "JSON:\n"
    '{{"items": [{{"n": "№ п/п, если есть", "name": "наименование товара", "unit": "единица измерения", '
    '"quantity": …, "price": …, "cost": "стоимость", "vat_rate": "ставка НДС, %", "vat": "сумма НДС", '
    '"cost_with_vat": "стоимость с НДС", "places": "количество грузовых мест", "mass": "масса груза", '
    '"note": "примечание"}}],\n'
    ' "totals": {{"quantity": …, "cost": …, "vat": …, "cost_with_vat": …, "places": …, "mass": …}} | null}}'
)

FOOTER = (
    "Это нижняя часть белорусской накладной ТН-2/ТТН-1 под товарной таблицей (страница {page}).\n"
    f"{_RULES}\n"
    "JSON:\n"
    '{{"totals": {{"quantity": …, "cost": …, "vat": …, "cost_with_vat": …, "places": …, "mass": …}} '
    "— только если здесь видна строка «Итого», иначе null,\n"
    ' "vat_words": "Всего сумма НДС (прописью)", "cost_with_vat_words": "Всего стоимость с НДС (прописью)", '
    '"mass_words": "Всего масса груза (прописью)", "places_words": "Всего количество грузовых мест (прописью)",\n'
    ' "released_by": "Отпуск разрешил", "handed_by": "Сдал грузоотправитель", '
    '"accepted_by": "Товар к доставке принял / Принял грузополучатель", '
    '"power_of_attorney": "По доверенности (номер, дата, кем выдана)", "seal": "№ пломбы"}}'
)

ROW_REPAIR = (
    "Это строка товарной таблицы белорусской накладной (сверху — шапка таблицы).\n"
    "Раньше строка прочитана так: {previous}\n"
    "Проверка не сошлась: {problem}\n"
    "Перечитай ОЧЕНЬ внимательно каждую цифру и запятую этой строки (частые ошибки: 3↔8, 1↔7, 5↔6, 0↔8, "
    "пропущенная или лишняя цифра). Не подгоняй числа под проверку — пиши то, что видишь.\n"
    f"{_RULES}\n"
    'JSON одной строки: {{"n": …, "name": …, "unit": …, "quantity": …, "price": …, "cost": …, "vat_rate": …, '
    '"vat": …, "cost_with_vat": …, "places": …, "mass": …, "note": …}}'
)

TOTALS_REPAIR = (
    "Это низ товарной таблицы и итоги белорусской накладной.\n"
    "Раньше прочитано: {previous}\n"
    "Проверка не сошлась: {problem}\n"
    "Перечитай очень внимательно строку «Итого» и суммы прописью. Не подгоняй — пиши то, что видишь.\n"
    f"{_RULES}\n"
    'JSON: {{"totals": {{"quantity": …, "cost": …, "vat": …, "cost_with_vat": …, "places": …, "mass": …}}, '
    '"vat_words": …, "cost_with_vat_words": …}}'
)

REQUISITES_REPAIR = (
    "Это увеличенный фрагмент верха белорусской накладной ТН-2/ТТН-1.\n"
    "Нужны только эти поля: {fields}. Проблема: {problem}\n"
    "Серия — 2 буквы, номер — 7 цифр (напечатаны типографски), УНП — 9 цифр в клетках, дата — ДД.ММ.ГГГГ. "
    "Если поля на этом фрагменте нет — null.\n"
    f"{_RULES}\n"
    'JSON: {{"series": …, "number": …, "date": …, "shipper": {{"unp": …}}, "consignee": {{"unp": …}}}}'
)

PAGE = (
    "Это скан страницы из пачки документов к накладным (страница {page}): оборот накладной, письмо, акт, "
    "приложение и т. п.\n"
    "Перепиши страницу в Markdown ТОЧНО как на бланке, в порядке чтения: заголовки — #, абзацы — текстом, "
    "таблицы — GitHub Flavored Markdown (все строки и столбцы, пустая ячейка — пусто), рукописное — как "
    "написано, нечитаемое — […]. Печати и подписи — одной строкой: [печать: текст], [подпись]. "
    "Не исправляй, не вычисляй, не дописывай. Верни ТОЛЬКО Markdown страницы."
)

UPRIGHT = (
    "На картинке две копии одной страницы документа: слева A, справа B (B — та же страница, повёрнутая на 180°). "
    "На какой копии текст читается нормально? Смотри на буквы и цифры, а не на рамки. "
    "Ответь ОДНОЙ буквой и ничем больше: A или B."
)


def parse_upright(data: dict[str, Any] | None) -> int:
    """{"upright": "B"} → 180 (перевернуть); «A», мусор и пустой ответ → 0 (не крутим)."""
    value = str((data or {}).get("upright", "")).strip().upper()
    return 180 if value in ("B", "В") else 0  # «В» — кириллица, модели путают


def parse_upright_answer(answer: str) -> int:
    """Поворот из ответа целиком: Qwen пишет текст вместо JSON. Неясно, какая копия нормальная — 0 (не крутим)."""
    data = parse_json(answer)
    if data and str(data.get("upright") or "").strip():
        return parse_upright(data)
    text = answer.upper().translate(str.maketrans({"В": "B", "А": "A"}))
    bare = [ln.strip(" *_`.").strip() for ln in text.splitlines() if ln.strip()]
    if bare and bare[-1] in ("A", "B"):  # «одной буквой», как просим
        return 180 if bare[-1] == "B" else 0
    named = re.findall(r"(?:КОПИ\w*|COPY|IMAGE)\s+([AB])", text)

    def upright_letter(letter: str) -> bool:
        for m in re.finditer(rf"(?:КОПИ\w*|COPY|IMAGE)\s+{letter}", text):
            window = text[m.start() : m.end() + 30]
            if not any(w in window for w in ("НОГ", "ПЕРЕВЕР", "UPSIDE")):
                return True
        return False

    good = [x for x in dict.fromkeys(named) if upright_letter(x)]
    return 180 if good == ["B"] else 0

TEXT_LAYER = (
    "\nТекстовый слой этой страницы PDF (может быть точнее изображения для цифр; при расхождении сверяйся "
    "с изображением):\n<<<\n{text}\n>>>"
)


def grid_hint(columns: int) -> str:
    """Подсказка по сетке бланка: число столбцов в строках данных (по вертикальным линиям)."""
    if columns < 3:  # 1–2 «столбца» — рамка без сетки, подсказка только собьёт
        return ""
    return (
        f"\nПо линиям бланка в строках данных {columns} столбцов (ячейки разделены вертикальными линиями). "
        "Каждое значение — в своём столбце; пустая ячейка — пусто, значения соседних столбцов не сдвигай."
    )


def with_text_layer(prompt: str, text: str, limit: int = 6000) -> str:
    return prompt + TEXT_LAYER.format(text=text[:limit]) if text.strip() else prompt


def parse_json(answer: str) -> dict[str, Any] | None:
    """Первый JSON-объект в ответе (```json … ```, текст до/после); None — не нашли."""
    text = re.sub(r"```(?:json)?", "", answer)
    start = text.find("{")
    while start != -1:
        depth, in_string, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    return data if isinstance(data, dict) else None
        start = text.find("{", start + 1)
    return None
