"""CLI: ingest-ttn <file.pdf | folder> [...] -o out_dir [--no-vision] [--vision-force]."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import warnings
from pathlib import Path


def _inputs(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += sorted(x for x in p.rglob("*") if x.suffix.lower() == ".pdf")
        else:
            files.append(p)
    return files


def _progress(report) -> None:
    where = f"waybill {report.doc}" if report.doc else "not a waybill"
    extra = f"; ERROR {report.error}" if report.error else (f"; copy of page {report.copy_of}" if report.copy_of else "")
    orient = next((s for s in report.steps if s.startswith("orientation")), "")
    print(f"  page {report.number}: {report.kind}, {where}; zones {report.zones}{extra}"
          + (f"; {orient}" if orient else ""), flush=True)


def main(argv: list[str] | None = None) -> int:
    from ingest_parse.envfile import load_scan_env

    load_scan_env()
    p = argparse.ArgumentParser(prog="ingest-ttn", description="ТН-2 / ТТН-1 (Беларусь): PDF-скан → JSON + Markdown.")
    p.add_argument("paths", type=Path, nargs="+", help="PDF files or folders (recursively)")
    p.add_argument("-o", "--output", type=Path, default=Path("out_ttn"), help="Output folder (default: out_ttn)")
    p.add_argument("--no-vision", action="store_true", help="Only preprocessing + page layout, no VLM.")
    p.add_argument("--vision-force", action="store_true", help="Ignore the VLM answer cache.")
    p.add_argument("--no-heron", action="store_true", help="Do not run Heron layout (fixed bands instead).")
    p.add_argument("--no-orientation", action="store_true", help="Skip the upside-down check.")
    p.add_argument("--no-denoise", action="store_true", help="Skip denoising (faster).")
    p.add_argument("--repair-rounds", type=int, default=2, help="Re-read rounds for rows failing checks (default 2).")
    p.add_argument("--debug", action="store_true", help="Also save preprocessed page, zones and VLM crops.")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows: вывод в файл идёт в cp1251, «→» и кириллица имён не падают
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    from ingest_parse.ttn.extract import TtnOptions, extract_ttn
    from ingest_parse.ttn.report import render

    client, max_edge = None, 2048
    if not args.no_vision:
        from ingest_parse.vision import VisionClient, VisionConfig, VisionConfigError

        try:
            config = VisionConfig.from_env()
        except VisionConfigError as exc:
            print(f"error: {exc} (or run with --no-vision)", file=sys.stderr)
            return 2
        config = dataclasses.replace(config, temperature=0.0, timeout_s=max(config.timeout_s, 180.0))
        client, max_edge = VisionClient(config), config.max_long_edge
    from ingest_parse.parse import _dots_sender

    sender = _dots_sender()
    options = TtnOptions(
        client=client,
        force=args.vision_force,
        max_long_edge=max_edge,
        repair_rounds=args.repair_rounds,
        orientation=not args.no_orientation,
        denoise=not args.no_denoise,
        heron=not args.no_heron,
        debug=args.debug,
        on_page=_progress,
        dots=sender,
    )

    files = _inputs(args.paths)
    if not files:
        print("error: no PDF files found", file=sys.stderr)
        return 1
    args.output.mkdir(parents=True, exist_ok=True)
    failed = 0
    for path in files:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = extract_ttn(path, args.output, options)
        except Exception as exc:  # noqa: BLE001 — один битый файл не останавливает пачку
            failed += 1
            print(f"{path.name}: error: {exc}", file=sys.stderr)
            continue
        for w in caught:
            if "ttn" in w.category.__name__.lower():
                print(f"warning: {w.message}", file=sys.stderr)
        stem = args.output / path.stem
        (stem.parent / f"{path.stem}.ttn.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (stem.parent / f"{path.stem}.ttn.md").write_text(render(result), encoding="utf-8", newline="\n")
        errors = sum(i.level == "error" for i in result.issues)
        status = "no vision" if not result.vision else ("OK" if result.ok else f"CHECK: {errors} errors")
        items = sum(len(d.waybill.items) for d in result.docs)
        print(f"{path.name}: {status}; waybills={len(result.docs)}; items={items}; pages={len(result.pages)}; "
              f"vlm calls={result.calls} → {stem}.ttn.md", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
