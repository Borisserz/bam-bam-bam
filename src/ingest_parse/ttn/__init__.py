"""ТН-2 / ТТН-1 (Беларусь): скан PDF → JSON полей + Markdown для проверки."""

from ingest_parse.ttn.schema import Item, Party, Totals, Waybill

__all__ = ["Item", "Party", "Totals", "Waybill"]
