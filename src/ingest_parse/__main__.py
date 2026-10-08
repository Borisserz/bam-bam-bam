"""CLI: python -m ingest_parse file (.txt/.doc/.docx/.docm/.rtf/.pdf) [-o out.md] [--media-dir DIR] [--vision | --vision-force]."""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

from ingest_parse.parse import parse_to_markdown
from ingest_parse.pdf_triage import PdfPageWarning


def main(argv: list[str] | None = None) -> int:
    from ingest_parse.envfile import load_scan_env

    load_scan_env()
    p = argparse.ArgumentParser(
        prog="ingest-parse",
        description="Parse txt/doc/docx/docm/rtf/pdf into Markdown (pdf scans: text via --vision).",
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
        "--scan-layout",
        action="store_true",
        help="PDF scans: run Heron layout (boxes in <media>/debug/); with --vision, figures and tables go to VLM as crops.",
    )
    p.add_argument(
        "--no-preprocess",
        action="store_true",
        help="PDF scans: plain 2x render (no native dpi, rotation, deskew, light/noise cleanup, orientation question).",
    )
    p.add_argument(
        "--chandra",
        action="store_true",
        help="Optional local Chandra 2 markdown hint for Qwen (SCAN_CHANDRA_URL). Off by default.",
    )
    p.add_argument(
        "--olm",
        action="store_true",
        help="Optional local olmOCR markdown hint for Qwen (SCAN_OLM_URL). Off by default. Do not combine with --chandra.",
    )
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows: вывод в файл идёт в cp1251 — символ вне кодировки не роняет запуск
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    from ingest_parse.letters import set_chandra, set_olm

    set_chandra(args.chandra)
    set_olm(args.olm)

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
                scan_layout=args.scan_layout,
                scan_preprocess=not args.no_preprocess,
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
