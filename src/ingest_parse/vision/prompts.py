"""Промпты VLM и разбор ответа на разделы."""

from __future__ import annotations

import re

from ingest_parse.vision.taxonomy import PromptMode

_KIND_RU = {
    "logo": "логотип",
    "photo": "фотография или иллюстрация",
    "diagram": "схема или страница документа со схемой",
    "table_scan": "таблица, вставленная картинкой",
    "chart": "график или диаграмма",
    "equation_img": "формула",
    "unknown": "изображение",
}

SECTIONS = ("Описание", "Текст с изображения", "Анализ")
_LABEL = re.compile(r"^\s*\**\s*(Описание|Текст с изображения|Анализ)\s*:?\s*\**\s*:?\s*", re.M)


def build_prompt(mode: PromptMode, kind: str, caption: str | None) -> str:
    context = f"Это изображение из документа ({_KIND_RU.get(kind, 'изображение')})."
    if caption:
        context += f" Подпись в документе: «{caption}»."
    if mode == "describe_only":
        return (
            f"{context}\nКратко опиши изображение на русском языке (1–3 предложения). "
            "Ответ строго в формате:\nОписание: …"
        )
    return (
        f"{context}\nОтветь на русском языке строго в формате из трёх разделов:\n"
        "Описание: что изображено (1–3 предложения).\n"
        "Текст с изображения: весь читаемый текст, числа и подписи как есть; таблицу — Markdown-таблицей; "
        "формулу — в LaTeX; если текста нет — «нет».\n"
        "Анализ: что показывает изображение и главные выводы (1–3 предложения)."
    )


def parse_sections(text: str) -> dict[str, str]:
    """{'Описание': …, 'Текст с изображения': …, 'Анализ': …}; без меток — всё в «Описание»."""
    matches = list(_LABEL.finditer(text))
    if not matches:
        return {"Описание": text.strip()}
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[m.end() : end].strip()
        if value and value.lower().strip(" .«»\"") not in ("нет", "-", "—"):
            sections.setdefault(m.group(1), value)
    return sections
