"""
Ветки parse для table / image.

Текст идёт по основному пути в extract.
Таблица → TableParser.parse_table_block (не flatten-only).
Картинка → ImageParser.parse_image_block (метаданные + hook под vision/OCR).

Команда подставит свои реализации через Protocol; дефолты — рабочие минимальные.

Image bytes handoff
-------------------
До ухода Docling/temp extract читает PIL → PNG bytes и передаёт в hook как
`image_bytes`. Дефолт байты НЕ персистит (только sha256/meta, `asset_hint=None`).
Кастомный ImageParser должен принять `image_bytes` и сам залить в S3 / диск,
затем выставить `asset_hint` на реальный ключ/URL.
"""

from __future__ import annotations

import hashlib
import inspect
import io
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from ingest_parse.docling_types import DoclingDocumentLike, DoclingPictureItem, DoclingTableItem
from ingest_parse.models import ImageBlock, TableBlock, make_block_id

_R = TypeVar("_R")


def _call_hook(method: Callable[..., _R], **kwargs: Any) -> _R:
    """Передать только те kwargs, которые hook принимает (совместимость со старыми сигнатурами)."""
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return method(**kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return method(**kwargs)
    allowed = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    return method(**{k: v for k, v in kwargs.items() if k in allowed})


def _rows_as_text(rows: list[list[str]]) -> str:
    return "\n".join("\t".join(cell for cell in row) for row in rows)


def extract_table_rows(
    table_item: DoclingTableItem | Any,
    doc: DoclingDocumentLike | Any,
) -> tuple[list[list[str]], bool, list[str]]:
    """Матрица ячеек + has_header_row + extract-warnings (не глотаем сбои молча)."""
    notes: list[str] = []
    try:
        df = table_item.export_to_dataframe(doc=doc)
        header = [str(c) for c in df.columns.tolist()]
        body = [[("" if v is None else str(v)) for v in row] for row in df.values.tolist()]
        if header and all(h == str(i) for i, h in enumerate(header)):
            return body, False, notes
        return [header, *body], True, notes
    except Exception as exc:  # noqa: BLE001 — поверхность в notes, не silent skip
        notes.append(
            f"dataframe export failed ({type(exc).__name__}: {exc}); "
            "using cell-grid fallback (merged cells may be lossy)"
        )

    data = getattr(table_item, "data", None)
    if data is None:
        notes.append("table data missing after dataframe failure")
        return [], False, notes
    n_rows = int(getattr(data, "num_rows", 0) or 0)
    n_cols = int(getattr(data, "num_cols", 0) or 0)
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for cell in getattr(data, "table_cells", []) or []:
        r = int(getattr(cell, "start_row_offset_idx", 0) or 0)
        c = int(getattr(cell, "start_col_offset_idx", 0) or 0)
        if 0 <= r < n_rows and 0 <= c < n_cols:
            grid[r][c] = getattr(cell, "text", "") or ""
    return grid, False, notes


def load_image_bytes(
    raw_item: DoclingPictureItem | Any,
    doc: DoclingDocumentLike | Any,
) -> tuple[bytes | None, dict[str, Any], list[str]]:
    """
    Честный handoff пикселей: PNG bytes + meta + notes.

    Вызывать ДО удаления temp/Docling context. Дефолтный ImageParser
    байты не пишет на диск — только хеширует; custom hook получает `image_bytes`.
    """
    notes: list[str] = []
    meta: dict[str, Any] = {
        "mime_type": None,
        "width": None,
        "height": None,
        "content_sha256": None,
    }
    try:
        pil = raw_item.get_image(doc)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"get_image failed ({type(exc).__name__}: {exc})")
        return None, meta, notes

    if pil is None:
        notes.append("get_image returned None")
        return None, meta, notes

    try:
        width, height = int(pil.width), int(pil.height)
        buf = io.BytesIO()
        # PNG — предсказуемый контейнер без зависимости от исходного формата
        pil.save(buf, format="PNG")
        raw = buf.getvalue()
        meta = {
            "mime_type": "image/png",
            "width": width,
            "height": height,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
        }
        return raw, meta, notes
    except Exception as exc:  # noqa: BLE001
        notes.append(f"PIL→PNG encode failed ({type(exc).__name__}: {exc})")
        return None, meta, notes


class TableParser(Protocol):
    """Точка расширения: структурированный разбор таблицы."""

    def parse_table_block(
        self,
        *,
        block_index: int,
        page: int | None,
        section_path: list[str],
        raw_item: DoclingTableItem | Any,
        doc: DoclingDocumentLike | Any,
        warnings_out: list[str] | None = None,
    ) -> TableBlock: ...


class ImageParser(Protocol):
    """Точка расширения: картинка → метаданные / байты под S3+vision.

    `image_bytes` — PNG (или None), извлечённые extract'ом до cleanup temp.
    Custom hook: upload bytes → выставить `asset_hint` на реальный URL/key.
    """

    def parse_image_block(
        self,
        *,
        block_index: int,
        page: int | None,
        section_path: list[str],
        raw_item: DoclingPictureItem | Any,
        doc: DoclingDocumentLike | Any,
        image_bytes: bytes | None = None,
        warnings_out: list[str] | None = None,
    ) -> ImageBlock: ...


class DefaultTableParser:
    """
    Дефолт: сохранить rows (+ header flag), text — только снимок.
    TableNormalizer / паспорт LLM подключат поверх этого hook.
    """

    def parse_table_block(
        self,
        *,
        block_index: int,
        page: int | None,
        section_path: list[str],
        raw_item: DoclingTableItem | Any,
        doc: DoclingDocumentLike | Any,
        warnings_out: list[str] | None = None,
    ) -> TableBlock:
        rows, has_header, notes = extract_table_rows(raw_item, doc)
        if warnings_out is not None:
            warnings_out.extend(notes)
        caption: str | None = None
        try:
            cap = raw_item.caption_text(doc=doc)
            if isinstance(cap, str) and cap.strip():
                caption = cap.strip()
        except Exception:
            caption = None
        return TableBlock(
            block_index=block_index,
            block_id=make_block_id(block_index),
            rows=rows,
            text=_rows_as_text(rows) if rows else None,
            page=page,
            section_path=list(section_path),
            label="table",
            has_header_row=has_header,
            caption=caption,
            parser_hook="default_table_parser",
        )


class DefaultImageParser:
    """
    Дефолт: caption + sha256 пикселей.
    Байты/S3 НЕ сохраняет — `asset_hint=None`, `vision_status=pending|skipped`.
    Для продакшена подставьте свой ImageParser (upload `image_bytes` → URL).
    """

    def parse_image_block(
        self,
        *,
        block_index: int,
        page: int | None,
        section_path: list[str],
        raw_item: DoclingPictureItem | Any,
        doc: DoclingDocumentLike | Any,
        image_bytes: bytes | None = None,
        warnings_out: list[str] | None = None,
    ) -> ImageBlock:
        caption: str | None = None
        try:
            cap = raw_item.caption_text(doc=doc)
            if isinstance(cap, str) and cap.strip():
                caption = cap.strip()
        except Exception:
            caption = None

        content_sha256: str | None = None
        mime_type: str | None = None
        width: int | None = None
        height: int | None = None
        vision_status: str = "skipped"

        # Предпочтительно bytes от extract (handoff); иначе пробуем сами.
        raw = image_bytes
        if raw is None:
            raw, meta, notes = load_image_bytes(raw_item, doc)
            if warnings_out is not None:
                warnings_out.extend(notes)
            if raw is not None:
                mime_type = meta["mime_type"]
                width = meta["width"]
                height = meta["height"]
                content_sha256 = meta["content_sha256"]
                vision_status = "pending"
        else:
            content_sha256 = hashlib.sha256(raw).hexdigest()
            mime_type = "image/png"
            vision_status = "pending"
            # width/height — если extract уже положил через load; иначе None ok
            try:
                from PIL import Image as PILImage

                with PILImage.open(io.BytesIO(raw)) as im:
                    width, height = int(im.width), int(im.height)
            except Exception:
                width, height = None, None

        # asset_hint=None: дефолт НЕ пишет файл/S3 — только sha/meta.
        # Оркестратор или кастомный ImageParser выставит реальный S3 key.
        return ImageBlock(
            block_index=block_index,
            block_id=make_block_id(block_index),
            page=page,
            section_path=list(section_path),
            label="picture",
            caption=caption,
            mime_type=mime_type,
            width=width,
            height=height,
            content_sha256=content_sha256,
            asset_hint=None,
            vision_status=vision_status,  # type: ignore[arg-type]
            parser_hook="default_image_parser",
        )


# синглтоны по умолчанию — parse/extract могут принять свои
DEFAULT_TABLE_PARSER = DefaultTableParser()
DEFAULT_IMAGE_PARSER = DefaultImageParser()


def parse_table_block(
    *,
    block_index: int,
    page: int | None,
    section_path: list[str],
    raw_item: DoclingTableItem | Any,
    doc: DoclingDocumentLike | Any,
    parser: TableParser | None = None,
    warnings_out: list[str] | None = None,
) -> TableBlock:
    """Публичный вход ветки table (удобно мокать в тестах)."""
    # Важно: `is None`, не `or` — иначе falsey-объект подменится дефолтом.
    impl: TableParser = DEFAULT_TABLE_PARSER if parser is None else parser
    return _call_hook(
        impl.parse_table_block,
        block_index=block_index,
        page=page,
        section_path=section_path,
        raw_item=raw_item,
        doc=doc,
        warnings_out=warnings_out,
    )


def parse_image_block(
    *,
    block_index: int,
    page: int | None,
    section_path: list[str],
    raw_item: DoclingPictureItem | Any,
    doc: DoclingDocumentLike | Any,
    parser: ImageParser | None = None,
    image_bytes: bytes | None = None,
    warnings_out: list[str] | None = None,
) -> ImageBlock:
    """Публичный вход ветки image."""
    impl: ImageParser = DEFAULT_IMAGE_PARSER if parser is None else parser
    return _call_hook(
        impl.parse_image_block,
        block_index=block_index,
        page=page,
        section_path=section_path,
        raw_item=raw_item,
        doc=doc,
        image_bytes=image_bytes,
        warnings_out=warnings_out,
    )
