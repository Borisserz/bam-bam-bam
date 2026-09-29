"""Тесты parse_document: txt/docx обязательны; .doc — skip без soffice."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ingest_parse import (
    ImageBlock,
    LibreOfficeNotFoundError,
    ParsedDocument,
    TableBlock,
    parse_document,
    parse_table_block,
)
from ingest_parse.convert_doc import find_soffice
from ingest_parse.detect import UnsupportedFormatError, detect_format
from ingest_parse.models import make_block_id


def test_detect_format_ok() -> None:
    assert detect_format("a.TXT") == "txt"
    assert detect_format(Path("x/y.Docx")) == "docx"
    assert detect_format("legacy.doc") == "doc"


def test_detect_format_rejects_unknown() -> None:
    with pytest.raises(UnsupportedFormatError):
        detect_format("notes.pdf")


def test_parse_txt_nonempty(sample_txt: Path) -> None:
    doc = parse_document(sample_txt)
    assert isinstance(doc, ParsedDocument)
    assert doc.format == "txt"
    assert doc.plain_text().strip()
    assert any(b.type == "text" and b.text.strip() for b in doc.blocks)
    assert len(doc.source_sha256) == 64
    assert [b.block_index for b in doc.blocks] == list(range(len(doc.blocks)))
    assert [b.block_id for b in doc.blocks] == [
        make_block_id(i) for i in range(len(doc.blocks))
    ]
    assert all(getattr(b, "label", None) for b in doc.blocks)
    payload = doc.model_dump()
    assert payload["source_path"]
    assert payload["source_sha256"]
    assert payload["blocks"]
    assert payload["canon_schema"].startswith("ingest_parse.ParsedDocument")


def test_parse_txt_from_bytes(sample_txt: Path) -> None:
    raw = sample_txt.read_bytes()
    doc = parse_document(raw, filename="sample.txt")
    assert doc.format == "txt"
    assert doc.source_path.startswith("<bytes:")
    assert doc.source_sha256 == __import__("hashlib").sha256(raw).hexdigest()
    assert doc.plain_text().strip()


def test_parse_bytes_requires_filename() -> None:
    with pytest.raises(ValueError, match="filename"):
        parse_document(b"hello")


def test_parse_docx_nonempty(sample_docx: Path) -> None:
    doc = parse_document(sample_docx)
    assert doc.format == "docx"
    assert doc.parser == "docling"
    assert len(doc.source_sha256) == 64
    assert [b.block_index for b in doc.blocks] == list(range(len(doc.blocks)))
    plain = doc.plain_text()
    assert "Hello from the sample fixture paragraph." in plain
    table_blocks = [b for b in doc.blocks if b.type == "table"]
    assert table_blocks, "expected at least one table block from sample.docx"
    assert isinstance(table_blocks[0], TableBlock)
    assert any("alpha" in cell for row in table_blocks[0].rows for cell in row)
    assert table_blocks[0].has_header_row is True
    assert table_blocks[0].parser_hook == "default_table_parser"
    headers = [b for b in doc.blocks if getattr(b, "heading_level", None) is not None]
    assert headers, "expected at least one heading with heading_level"
    assert headers[0].label in {"section_header", "title"}


def test_parse_rich_docx_section_path_and_labels(rich_docx: Path) -> None:
    doc = parse_document(rich_docx)
    assert doc.title == "Rich Sample Document"
    assert any(b.type == "table" for b in doc.blocks)
    nested = [
        b for b in doc.blocks if b.type == "text" and "Body paragraph under H2" in b.text
    ]
    assert nested
    assert nested[0].section_path[:2] == [
        "Rich Sample Document",
        "Section Two — Details",
    ]
    labels = {getattr(b, "label", None) for b in doc.blocks}
    assert "section_header" in labels or "title" in labels
    assert "table" in labels


def test_injectable_table_parser(sample_docx: Path) -> None:
    """Команда может подменить TableParser без правки core."""
    calls: list[int] = []

    class SpyTable:
        def parse_table_block(
            self,
            *,
            block_index: int,
            page: int | None,
            section_path: list[str],
            raw_item: Any,
            doc: Any,
        ) -> TableBlock:
            calls.append(block_index)
            # делегируем дефолту через публичный hook
            return parse_table_block(
                block_index=block_index,
                page=page,
                section_path=section_path,
                raw_item=raw_item,
                doc=doc,
            )

    doc = parse_document(sample_docx, table_parser=SpyTable())
    assert calls, "table branch must invoke injectable parser"
    assert any(b.type == "table" for b in doc.blocks)


def test_injectable_table_callable(sample_docx: Path) -> None:
    seen: list[str] = []

    def my_table(**kwargs: Any) -> TableBlock:
        seen.append("ok")
        return parse_table_block(**kwargs)

    doc = parse_document(sample_docx, table_parser=my_table)
    assert seen
    assert any(b.type == "table" for b in doc.blocks)


def test_parse_docx_with_image_routes_image_hook(docx_with_image: Path) -> None:
    doc = parse_document(docx_with_image)
    images = [b for b in doc.blocks if b.type == "image"]
    assert images, "expected ImageBlock from picture in docx"
    assert isinstance(images[0], ImageBlock)
    assert images[0].parser_hook == "default_image_parser"
    assert images[0].block_id.startswith("b")
    # vision_status: pending если байты есть, skipped если Docling не отдал PIL
    assert images[0].vision_status in {"pending", "skipped"}


def test_parse_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_document(tmp_path / "nope.txt")


def test_doc_without_soffice_raises_clear_error(tmp_path: Path) -> None:
    fake = tmp_path / "legacy.doc"
    fake.write_bytes(b"\xd0\xcf\x11\xe0")
    with patch("ingest_parse.convert_doc.find_soffice", return_value=None):
        with pytest.raises(LibreOfficeNotFoundError) as exc_info:
            parse_document(fake)
    msg = str(exc_info.value)
    assert "LibreOffice" in msg
    assert "apt install" in msg or "brew install" in msg


@pytest.mark.skipif(find_soffice() is None, reason="LibreOffice/soffice not installed")
def test_doc_with_soffice_if_available(sample_docx: Path, tmp_path: Path) -> None:
    from ingest_parse.convert_doc import convert_doc_to_docx

    assert find_soffice()
    with pytest.raises(ValueError):
        convert_doc_to_docx(sample_docx, output_dir=tmp_path)


def test_injectable_image_callable(docx_with_image: Path) -> None:
    """Кастомный callable image hook получает image_bytes handoff."""
    seen_bytes: list[bytes | None] = []

    def my_image(
        *,
        block_index: int,
        page: int | None,
        section_path: list[str],
        raw_item: Any,
        doc: Any,
        image_bytes: bytes | None = None,
        **_kwargs: Any,
    ) -> ImageBlock:
        seen_bytes.append(image_bytes)
        return ImageBlock(
            block_index=block_index,
            block_id=make_block_id(block_index),
            page=page,
            section_path=list(section_path),
            caption="custom",
            content_sha256=None,
            asset_hint="s3://bucket/custom.png" if image_bytes else None,
            vision_status="pending" if image_bytes else "skipped",
            parser_hook="custom_image_callable",
        )

    doc = parse_document(docx_with_image, image_parser=my_image)
    images = [b for b in doc.blocks if b.type == "image"]
    assert images
    assert images[0].parser_hook == "custom_image_callable"
    assert seen_bytes, "image callable must be invoked"
    # Docling обычно отдаёт PIL → bytes; если нет — handoff честно None
    if seen_bytes[0] is not None:
        assert images[0].asset_hint == "s3://bucket/custom.png"
        assert images[0].vision_status == "pending"
    else:
        assert "vision_status=skipped" in " ".join(doc.warnings) or images[0].vision_status == "skipped"


def test_hook_failure_becomes_warning(sample_docx: Path) -> None:
    """Сбой TableParser → warning + skip блока (не silent success без следа)."""

    class BoomTable:
        def parse_table_block(self, **_kwargs: Any) -> TableBlock:
            raise RuntimeError("boom-table")

    doc = parse_document(sample_docx, table_parser=BoomTable(), strict=False)
    assert any("boom-table" in w for w in doc.warnings)
    assert any("skipped" in w for w in doc.warnings)
    assert not any(b.type == "table" for b in doc.blocks)
    # текст всё ещё на месте
    assert any(b.type == "text" for b in doc.blocks)


def test_hook_failure_strict_raises(sample_docx: Path) -> None:
    from ingest_parse import HookExtractError

    class BoomTable:
        def parse_table_block(self, **_kwargs: Any) -> TableBlock:
            raise RuntimeError("boom-strict")

    with pytest.raises(HookExtractError) as exc_info:
        parse_document(sample_docx, table_parser=BoomTable(), strict=True)
    assert exc_info.value.branch == "table"


def test_hook_block_id_invariant_normalized(sample_docx: Path) -> None:
    """Hook с кривым block_id → normalize + warning (strict=False)."""

    class BadIds:
        def parse_table_block(
            self,
            *,
            block_index: int,
            page: int | None,
            section_path: list[str],
            raw_item: Any,
            doc: Any,
            **_kwargs: Any,
        ) -> TableBlock:
            return TableBlock(
                block_index=999,
                block_id="wrong-id",
                rows=[["a"]],
                page=page,
                section_path=list(section_path),
                parser_hook="bad_ids",
            )

    doc = parse_document(sample_docx, table_parser=BadIds(), strict=False)
    tables = [b for b in doc.blocks if b.type == "table"]
    assert tables
    assert tables[0].block_id == make_block_id(tables[0].block_index)
    assert tables[0].block_index != 999
    assert any("normalized" in w for w in doc.warnings)


def test_bytes_temp_cleanup(sample_txt: Path, tmp_path: Path) -> None:
    """Temp для bytes-входа удаляется после parse (нет утечки ingest-parse-bytes-*)."""
    import tempfile

    raw = sample_txt.read_bytes()
    before = set(Path(tempfile.gettempdir()).glob("ingest-parse-bytes-*"))
    doc = parse_document(raw, filename="cleanup-me.txt")
    assert doc.format == "txt"
    after = set(Path(tempfile.gettempdir()).glob("ingest-parse-bytes-*"))
    # новых незакрытых tmpdir не осталось
    assert after <= before


def test_default_image_no_fake_asset_hint(docx_with_image: Path) -> None:
    doc = parse_document(docx_with_image)
    for b in doc.blocks:
        if b.type == "image":
            assert b.asset_hint is None
