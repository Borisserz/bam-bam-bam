# ingest-parse

Parse-only Python package: **txt / doc / docx** → `ParsedDocument`
(text + tables + images + `section_path` / `block_id`).  
**No** chunking, embedding, Qdrant, or Postgres.

Aligns with Ilya schema column **parse / canon** (stage-1 ingest).

## Drop into a multi-parser micro-pipeline

Один стабильный вход — дальше команда склеит 3–4 парсера (text/doc + table + image + …):

```python
from ingest_parse import parse_document, TableParser, ImageParser

# path
doc = parse_document("report.docx")

# bytes (filename нужен для детекта расширения)
doc = parse_document(raw_bytes, filename="report.docx")

# подмена веток без правки core
doc = parse_document(
    "report.docx",
    table_parser=MyTableParser(),   # Protocol или callable
    image_parser=my_image_fn,       # callable(**kwargs) -> ImageBlock
)

payload = doc.model_dump()          # JSON-ready
# payload["blocks"][*]["type"] ∈ {"text","table","image"}
```

| Ветка | Когда | Hook |
|-------|--------|------|
| text | абзацы / заголовки / списки | основной путь в `extract` |
| table | Docling `TableItem` | `TableParser.parse_table_block` |
| image | Docling `PictureItem` | `ImageParser.parse_image_block` |

Дефолты: `DefaultTableParser` (rows + `has_header_row`), `DefaultImageParser` (PIL→sha256/`vision_status`; **байты на диск не пишет**, `asset_hint=None` — S3 делает оркестратор или ваш hook).  
**Image bytes handoff:** extract читает PNG bytes **до** cleanup Docling/temp и передаёт в hook как `image_bytes`. Кастомный `ImageParser` заливает в S3 и ставит реальный `asset_hint`.  
Пустые таблицы / сбой hook / картинки без пикселей → `warnings[]` (не silent success). `strict=True` → `HookExtractError` / `HookContractError`.  
`block_index` / `block_id` нормализуются после hook (mismatch → warning или raise в strict).  
Side effects: только чтение файла; temp — для `bytes` и `.doc→docx`, удаляются сразу.

Установка: `uv sync` / `pip install -e .` → `from ingest_parse import parse_document`.

### Public API (EN)

```python
from ingest_parse import parse_document, HookExtractError

doc = parse_document(path_or_bytes, filename="x.docx")  # filename required for bytes
doc = parse_document("x.docx", image_parser=my_fn, strict=False)
# my_fn(..., image_bytes: bytes | None) -> ImageBlock  # upload bytes yourself
```

Errors: `IngestParseError` → `HookExtractError` (extract fail), `HookContractError` (`block_id` invariant), `LibreOfficeNotFoundError`, `UnsupportedFormatError`.

## Stack

| Role | Tech |
|------|------|
| DOCX parse | [Docling](https://github.com/docling-project/docling) (MIT) |
| TXT | plain UTF-8 (`InputFormat.TXT` нет) |
| Legacy `.doc` | LibreOffice `soffice` → `.docx` → Docling |
| Models | Pydantic v2 |

**Not used:** PyMuPDF (AGPL).

## Install

Python **3.12+**.

```bash
uv sync --group dev
# optional for .doc:
sudo apt install libreoffice-writer
```

## CLI (тонкая обёртка над тем же API)

```bash
uv run python -m ingest_parse tests/fixtures/sample.txt
uv run python -m ingest_parse path/to/file.docx --full
```

## Example shape

```json
{
  "source_path": "/abs/path/sample.docx",
  "source_sha256": "…64 hex…",
  "format": "docx",
  "title": "Sample Title",
  "blocks": [
    {
      "type": "text",
      "block_index": 0,
      "block_id": "b0000",
      "text": "Sample Title",
      "page": null,
      "section_path": ["Sample Title"],
      "label": "section_header",
      "heading_level": 1
    },
    {
      "type": "table",
      "block_index": 2,
      "block_id": "b0002",
      "rows": [["Name", "Value"], ["alpha", "42"]],
      "has_header_row": true,
      "parser_hook": "default_table_parser",
      "section_path": ["Sample Title"]
    }
  ],
  "parser": "docling",
  "parser_version": "0.1.0",
  "canon_schema": "ingest_parse.ParsedDocument@0.2",
  "warnings": []
}
```

Office: `page` часто `null` → цитата через `section_path`.

## Tests

```bash
uv run pytest -q
```

## Layout

```
src/ingest_parse/
  parse.py        # parse_document(path|bytes, …) — публичный вход
  hooks.py        # TableParser / ImageParser + defaults
  models.py       # ParsedDocument / Text|Table|ImageBlock
  extract.py      # Docling → роутинг веток
  detect.py
  convert_doc.py
  __main__.py     # CLI only
```
