"""Минимальные Protocol-адаптеры вместо голого Any для Docling-объектов.

Не тянем runtime-зависимость на конкретные классы Docling — только duck-typing.
Кастомные hooks могут аннотировать raw_item/doc этими протоколами.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class DoclingProv(Protocol):
    page_no: int | None


@runtime_checkable
class DoclingItem(Protocol):
    """Общий item из iterate_items (text / table / picture)."""

    label: Any
    prov: list[DoclingProv] | None

    @property
    def text(self) -> str | None: ...


@runtime_checkable
class DoclingTableItem(Protocol):
    """TableItem: dataframe export + caption + cell grid fallback."""

    label: Any
    data: Any
    prov: list[DoclingProv] | None

    def export_to_dataframe(self, *, doc: Any) -> Any: ...

    def caption_text(self, *, doc: Any) -> str: ...


@runtime_checkable
class DoclingPictureItem(Protocol):
    """PictureItem: PIL через get_image + caption."""

    label: Any
    prov: list[DoclingProv] | None

    def get_image(self, doc: Any) -> Any: ...

    def caption_text(self, *, doc: Any) -> str: ...


@runtime_checkable
class DoclingDocumentLike(Protocol):
    """Документ после DocumentConverter.convert(...).document."""

    def iterate_items(self) -> Iterator[tuple[Any, int]]: ...
