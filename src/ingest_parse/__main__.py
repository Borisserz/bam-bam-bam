"""CLI: python -m ingest_parse file (.txt/.doc/.docx/.docm/.rtf/.pdf) [-o out.md] [--media-dir DIR]
[--vision | --vision-force] [--ocr-fallback]."""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

from ingest_parse.parse import parse_to_markdown
from ingest_parse.pdf_triage import PdfPageWarning


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="ingest-parse",
        description="Parse txt/doc/docx/docm/rtf/pdf into Markdown (pdf scans: --vision or --ocr-fallback).",
    )
    p.add_argument("path", type=Path, help="Path to .txt / .doc / .docx / .docm / .rtf / .pdf")
    p.add_argument("-o", "--output", type=Path, help="Write Markdown to file (default: stdout).")
    p.add_argument(
        "--media-dir",
        type=Path,
        help="Write pictures here (default with -o: <out dir>/media/<document name>/; without -o: none).",
    )
    p.add_argument(
        "--vision",
        action="store_true",
        help="Describe pictures via VLM (VISION_API_BASE_URL); block above each image, cached.",
    )
    p.add_argument(
        "--vision-force", action="store_true", help="Like --vision, but re-ask the API for every picture."
    )
    p.add_argument(
        "--ocr-fallback",
        action="store_true",
        help="Scanned PDF pages: run RapidOCR if --vision is off or the vision API fails (extra: ocr).",
    )
    args = p.parse_args(argv)

    vision = args.vision or args.vision_force
    if vision:
        from ingest_parse.vision import VisionConfig, VisionConfigError

        try:
            VisionConfig.from_env()
        except VisionConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    media_dir = args.media_dir
    if media_dir is None and args.output:
        media_dir = args.output.parent / "media" / args.path.stem
    media_link = None
    if media_dir is not None and args.output:
        # ссылки в out.md — относительно его папки
        media_link = Path(os.path.relpath(media_dir, args.output.parent)).as_posix()

    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", PdfPageWarning)
            md = parse_to_markdown(
                args.path,
                media_dir=media_dir,
                media_link=media_link,
                vision=args.vision,
                vision_force=args.vision_force,
                links_base=args.output.parent if args.output else None,
                ocr_fallback=True if args.ocr_fallback else None,
            )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for w in caught:
        if issubclass(w.category, PdfPageWarning):
            print(f"warning: {w.message}", file=sys.stderr)
        else:
            warnings.showwarning(w.message, w.category, w.filename, w.lineno)

    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(md, encoding="utf-8", newline="\n")  # Windows: без CRLF
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return 1
    else:
        sys.stdout.flush()
        sys.stdout.buffer.write(md.encode("utf-8"))  # не кодировка консоли (cp1251 на Windows)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
