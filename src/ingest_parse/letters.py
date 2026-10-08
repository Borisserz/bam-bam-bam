"""Слот текстовой подсказки для промпта Qwen. Сейчас пустой."""

from __future__ import annotations

from PIL import Image


def read_page_letters(image: Image.Image) -> str:
    """Подсказка страницы для Qwen.

    Завтра сюда кладётся markdown Chandra 2, локальный vLLM.
    Слот только для промпта Qwen. Markdown при выключенном Qwen не заменяет.
    Сейчас всегда "". Картинка главнее подсказки. Пустую клетку из подсказки не заполнять.
    """
    del image
    return ""


def page_hint(pdf_text: str, image: Image.Image) -> str:
    """Текстовый слой PDF и слот букв. Чужой распознанный текст сюда не входит."""
    extra = read_page_letters(image)
    parts = [p.strip() for p in (pdf_text, extra) if p and p.strip()]
    return "\n\n".join(parts)
