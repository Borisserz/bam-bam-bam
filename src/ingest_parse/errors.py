"""Стабильный error-контракт для worker/retry (не глотать extract failures)."""

from __future__ import annotations


class IngestParseError(Exception):
    """Базовый тип ошибок ingest_parse (удобно ловить в оркестраторе)."""


class HookExtractError(IngestParseError):
    """Сбой TableParser / ImageParser при извлечении блока.

    В обычном режиме (`strict=False`) extract ловит это и пишет в `warnings`.
    В `strict=True` пробрасывается наружу — parse не маскирует потерю данных.
    """

    def __init__(
        self,
        message: str,
        *,
        branch: str,
        block_index: int,
    ) -> None:
        super().__init__(message)
        self.branch = branch
        self.block_index = block_index


class HookContractError(IngestParseError):
    """Hook вернул блок с нарушенными инвариантами block_index / block_id."""

    def __init__(
        self,
        message: str,
        *,
        branch: str,
        block_index: int,
    ) -> None:
        super().__init__(message)
        self.branch = branch
        self.block_index = block_index
