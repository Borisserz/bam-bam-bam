"""Heron (плитки) | dots.mocr | итог слияния с сеткой линий — на одних и тех же выровненных страницах.

    uv run python scripts/compare_layout.py examples/ttn_public/raw -o examples/ttn_public/fused \
        --dots-cache examples/ttn_public/compare

dots поднимается отдельно (mlx_vlm.server на Mac, vLLM / llama.cpp на ПК) — OpenAI-совместимый API.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ingest_parse.ttn.dots import LONG_EDGE, dots_layout, http_sender
from ingest_parse.ttn.layout import Det, fuse, heron_layout, role, ruled_tables
from ingest_parse.ttn.preprocess import prepare
from ingest_parse.ttn.raster import rasterize

_COLOR = {"table": (0, 160, 0), "picture": (0, 90, 255), "margin": (150, 150, 150), "text": (220, 0, 0), "container": (255, 150, 0), "ink": (170, 0, 200)}
_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}


def pages(path: Path):
    """(номер, картинка, dpi) — PDF через растеризатор ТТН, картинка — dpi по длинной стороне A4."""
    if path.suffix.lower() == ".pdf":
        for p in rasterize(path):
            yield p.number, p.image, p.dpi
        return
    with Image.open(path) as im:
        frames = getattr(im, "n_frames", 1)
        for i in range(frames):
            im.seek(i)
            image = im.convert("RGB")
            dpi = min(600.0, max(72.0, max(image.size) / 11.69))
            yield i + 1, image, dpi


def ink_coverage(gray: np.ndarray, boxes) -> float:
    ink = gray < 128
    total = int(ink.sum())
    if not total:
        return 1.0
    mask = np.zeros_like(ink)
    for b in boxes:
        l, t, r, btm = (int(max(0, b[0])), int(max(0, b[1])), int(min(gray.shape[1], b[2])), int(min(gray.shape[0], b[3])))
        mask[t:btm, l:r] = True
    return float((ink & mask).sum() / total)


def overlay(image: Image.Image, regions, title: str) -> Image.Image:
    scale = 1000 / max(image.size)
    out = image.convert("RGB").resize((int(image.width * scale), int(image.height * scale)))
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default(size=14)
    for label, _, b, *rest in regions:
        color = _COLOR["ink"] if rest and rest[0] == "ink" else _COLOR[role(label)]
        box = [v * scale for v in b]
        draw.rectangle(box, outline=color, width=3)
        draw.text((box[0] + 3, box[1] + 2), label, fill=color, font=font)
    head = Image.new("RGB", (out.width, 36), "white")
    ImageDraw.Draw(head).text((8, 8), title, fill="black", font=ImageFont.load_default(size=20))
    full = Image.new("RGB", (out.width, out.height + 36), "white")
    full.paste(head, (0, 0))
    full.paste(out, (0, 36))
    return full


def side_by_side(a: Image.Image, b: Image.Image) -> Image.Image:
    out = Image.new("RGB", (a.width + b.width + 12, max(a.height, b.height)), (40, 40, 40))
    out.paste(a, (0, 0))
    out.paste(b, (a.width + 12, 0))
    return out


def _panel(page: Image.Image, dets: list[Det], title: str) -> Image.Image:
    return overlay(page, [(d.label or d.role, d.score, d.box, d.source) for d in dets], title)


class _Cached:
    """Ответы dots по порядку вызовов (страница, верх, низ) — в файлы; есть файл — сервер не зовём."""

    def __init__(self, send, stem: str, read: Path, write: Path) -> None:
        self.send, self.stem, self.read, self.write, self.calls, self.fresh = send, stem, read, write, 0, 0.0

    def __call__(self, image: Image.Image, prompt: str) -> str:
        name = f"{self.stem}-dots{('', '-top', '-bottom')[min(self.calls, 2)]}.txt"
        self.calls += 1
        if (self.read / name).exists():
            return (self.read / name).read_text(encoding="utf-8")
        t = time.time()
        answer = self.send(image, prompt)
        self.fresh += time.time() - t
        (self.write / name).write_text(answer, encoding="utf-8")
        return answer


def _best_old(page: Image.Image, dpi: float, gray: np.ndarray, dots_file: Path, edge: int) -> tuple[str, list[Det]]:
    """Лучшее из прошлого сравнения: Heron через Docling или dots одним запросом — у кого больше таблиц, затем покрытие."""
    from ingest_parse.ttn.dots import parse_dots
    from ingest_parse.ttn.zones import heron_regions

    options = [("Heron", [Det(role(lb), c, tuple(int(v) for v in b), "heron", lb) for lb, c, b in heron_regions(page, dpi)])]
    if dots_file.exists():
        dots, status = parse_dots(dots_file.read_text(encoding="utf-8"), page.size, edge)
        if status in ("ok", "salvaged"):
            options.append(("dots", dots))

    def score(item):
        dets = item[1]
        return (sum(d.role == "table" for d in dets), ink_coverage(gray, [d.box for d in dets]))

    return max(options, key=score)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--dots-url", default="http://127.0.0.1:8091")
    ap.add_argument("--dots-model", default="mlx-community/dots.mocr-8bit")
    ap.add_argument("--dots-edge", type=int, default=LONG_EDGE, help="длинная сторона картинки для dots, px")
    ap.add_argument("--dots-cache", type=Path, help="папка с готовыми ответами dots (*-dots.txt); по умолчанию -o")
    ap.add_argument("--no-dots", action="store_true", help="только Heron и линии")
    ap.add_argument("--no-tiles", action="store_true", help="Heron без половин страницы")
    ap.add_argument("--max-pages", type=int, default=2, help="страниц с одного файла")
    ap.add_argument("--vs-best", action="store_true", help="две панели: лучшее из старых Heron/dots | новый пайплайн")
    args = ap.parse_args(argv)

    files = sorted(p for i in args.inputs for p in ([i] if i.is_file() else i.iterdir()) if p.suffix.lower() in _SUFFIXES)
    args.out.mkdir(parents=True, exist_ok=True)
    cache_dir = args.dots_cache or args.out
    results: dict = {}
    sender = http_sender(args.dots_url, args.dots_model, timeout=1800)
    for path in files:
        for number, image, dpi in pages(path):
            if number > args.max_pages:
                break
            key = f"{path.name}#{number}"
            stem = f"{path.stem}-p{number}"
            prep = prepare(image, dpi)
            page = prep.image
            gray = np.asarray(page.convert("L"), dtype=np.uint8)
            t = time.time()
            heron = heron_layout(page, tiles=not args.no_tiles)
            heron_s = time.time() - t
            t = time.time()
            ruled = ruled_tables(gray)
            lines_s = time.time() - t
            dots_s, status = 0.0, "off"
            dots: list[Det] = []
            if not args.no_dots:
                send = _Cached(sender, stem, cache_dir, args.out)
                try:
                    dots, status = dots_layout(page, send, args.dots_edge)
                except Exception as exc:  # сервер упал / таймаут — страница всё равно в отчёте
                    status = f"error: {exc}"
                dots_s = send.fresh
            final = fuse(page.size, heron, dots, ruled, gray)
            count = lambda ds, r: sum(d.role == r for d in ds)
            row = {
                "size": list(page.size),
                "heron": {"n": len(heron), "tables": count(heron, "table"), "s": round(heron_s, 1),
                          "ink": round(ink_coverage(gray, [d.box for d in heron]), 3)},
                "dots": {"n": len(dots), "tables": count(dots, "table"), "s": round(dots_s, 1), "status": status,
                         "ink": round(ink_coverage(gray, [d.box for d in dots]), 3)},
                "lines": {"tables": len(ruled), "s": round(lines_s, 2)},
                "fused": {"n": len(final), "tables": count(final, "table"), "text": count(final, "text") + count(final, "margin"),
                          "pictures": count(final, "picture"), "ink": round(ink_coverage(gray, [d.box for d in final]), 3),
                          "table_sources": sorted({d.source for d in final if d.role == "table"})},
            }
            results[key] = row
            if args.vs_best:
                name, old = _best_old(page, prep.dpi, gray, cache_dir / f"{stem}-dots.txt", args.dots_edge)
                row["best_old"] = {"source": name, "tables": sum(d.role == "table" for d in old),
                                   "ink": round(ink_coverage(gray, [d.box for d in old]), 3)}
                results[key] = row
                side_by_side(
                    _panel(page, old, f"Best old: {name}, {row['best_old']['tables']} tables, {len(old)} regions"),
                    _panel(page, final, f"New pipeline: {row['fused']['tables']} tables, {row['fused']['text']} text, "
                                        f"dots {status}"),
                ).save(args.out / f"{stem}.png")
                print(f"{key}: best old {row['best_old']} | new {row['fused']}", flush=True)
                continue
            pic = side_by_side(
                side_by_side(
                    _panel(page, heron, f"Heron+tiles: {len(heron)} regions, {row['heron']['tables']} tables, {heron_s:.1f} s"),
                    _panel(page, dots, f"dots: {len(dots)} regions, {row['dots']['tables']} tables, {status}"),
                ),
                _panel(page, final, f"Fused: {row['fused']['tables']} tables ({len(ruled)} by lines), {row['fused']['text']} text"),
            )
            pic.save(args.out / f"{stem}.png")
            print(f"{key}: heron {row['heron']} | dots {row['dots']} | lines {len(ruled)} | fused {row['fused']}", flush=True)
    (args.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (write_vs_best if args.vs_best else write_summary)(results, args.out / "summary.md")
    return 0


def write_vs_best(results: dict, path: Path) -> None:
    lines = [
        "| страница | лучшее старое | таблиц было | таблиц стало | текст стало | покрытие было → стало | dots |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, r in results.items():
        b, f = r["best_old"], r["fused"]
        lines.append(
            f"| {key} | {b['source']} | {b['tables']} | {f['tables']} | {f['text']} | {b['ink']:.0%} → {f['ink']:.0%} | {r['dots']['status']} |"
        )
    rows = list(results.values())
    if rows:
        lines += [
            "",
            (
                f"Страниц: {len(rows)}. Таблиц: было {sum(r['best_old']['tables'] for r in rows)}, "
                f"стало {sum(r['fused']['tables'] for r in rows)}. Лучшим старым был Heron на "
                f"{sum(r['best_old']['source'] == 'Heron' for r in rows)} стр., dots на "
                f"{sum(r['best_old']['source'] == 'dots' for r in rows)} стр."
            ),
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_summary(results: dict, path: Path) -> None:
    lines = [
        "| страница | Heron+: обл. / табл. / покрытие | dots: обл. / табл. / покрытие / статус | линии: табл. | итог: табл. / текст / картинки / покрытие |",
        "|---|---|---|---|---|",
    ]
    for key, r in results.items():
        h, d, f = r["heron"], r["dots"], r["fused"]
        lines.append(
            f"| {key} | {h['n']} / {h['tables']} / {h['ink']:.0%} | {d['n']} / {d['tables']} / {d['ink']:.0%} / {d['status']} "
            f"| {r['lines']['tables']} | {f['tables']} / {f['text']} / {f['pictures']} / {f['ink']:.0%} |"
        )
    rows = list(results.values())
    if rows:
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
        lines += [
            "",
            (
                f"Страниц: {len(rows)}. Покрытие чернил: Heron+ {mean([r['heron']['ink'] for r in rows]):.0%}, "
                f"dots {mean([r['dots']['ink'] for r in rows]):.0%}, итог {mean([r['fused']['ink'] for r in rows]):.0%}. "
                f"Heron+ {mean([r['heron']['s'] for r in rows]):.1f} c на страницу. "
                f"Таблиц: Heron+ {sum(r['heron']['tables'] for r in rows)}, dots {sum(r['dots']['tables'] for r in rows)}, "
                f"линии {sum(r['lines']['tables'] for r in rows)}, итог {sum(r['fused']['tables'] for r in rows)}."
            ),
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
