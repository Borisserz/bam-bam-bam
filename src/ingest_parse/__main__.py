"""CLI: python -m ingest_parse path/to/file.docx → JSON summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ingest_parse.parse import parse_document


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m ingest_parse",
        description="Parse txt/doc/docx into canonical ParsedDocument JSON (no chunk/embed).",
    )
    p.add_argument("path", type=Path, help="Path to .txt / .doc / .docx")
    p.add_argument(
        "--full",
        action="store_true",
        help="Print full ParsedDocument JSON (default: compact summary).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        doc = parse_document(args.path)
    except Exception as exc:  # CLI: показать ошибку и ненулевой код
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.full:
        print(doc.model_dump_json(indent=2, ensure_ascii=False))
        return 0

    plain = doc.plain_text()
    summary = {
        "source_path": doc.source_path,
        "source_sha256": doc.source_sha256,
        "format": doc.format,
        "title": doc.title,
        "parser": doc.parser,
        "parser_version": doc.parser_version,
        "canon_schema": doc.canon_schema,
        "block_count": len(doc.blocks),
        "text_preview": plain[:500],
        "text_chars": len(plain),
        "warnings": doc.warnings,
        "block_types": [b.type for b in doc.blocks],
        "block_labels": [getattr(b, "label", None) for b in doc.blocks],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
