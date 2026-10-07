"""Проверка вопроса «вверх ногами?» на живой VLM: каждая страница прямо и повёрнутой на 180°.

Геометрия (лист на боку, перекос) от модели не зависит; от модели зависит только выбор 0/180.
Страницы в папке считаются стоящими прямо; ответ «B» на прямой странице — ошибка модели (или скан сам перевёрнут).

    uv run python scripts/check_orientation.py папка_со_сканами [ещё папки или файлы] [--limit 20]

Нужен VISION_API_BASE_URL (как для ingest-ttn). Картинки .png/.jpg и PDF (первая страница).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path

from PIL import Image

from ingest_parse.ttn.preprocess import prepare, upright_pair
from ingest_parse.ttn.prompts import UPRIGHT, parse_json, parse_upright
from ingest_parse.vision import VisionClient, VisionConfig, VisionError
from ingest_parse.vision.enrich import image_payload

_IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def _pages(paths: list[Path]) -> list[tuple[str, Image.Image, float]]:
    files = [f for p in paths for f in (sorted(p.rglob("*")) if p.is_dir() else [p])]
    out = []
    for f in files:
        suffix = f.suffix.lower()
        if suffix in _IMAGES:
            image = Image.open(f).convert("RGB")
            out.append((f.name, image, max(image.size) / 11.69))
        elif suffix == ".pdf":
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(str(f))
            out.append((f.name, pdf[0].render(scale=200 / 72).to_pil().convert("RGB"), 200.0))
            pdf.close()
    return out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", type=Path, nargs="+")
    p.add_argument("--limit", type=int, default=0, help="Не больше N страниц (0 — все).")
    p.add_argument("--save", type=Path, help="Папка: сохранить пары A/B, на которых модель ошиблась.")
    args = p.parse_args()

    config = VisionConfig.from_env()
    client = VisionClient(dataclasses.replace(config, temperature=0.0))
    pages = _pages(args.paths)[: args.limit or None]
    if not pages:
        print("нет картинок/PDF", file=sys.stderr)
        return 2
    wrong, failed, seconds = [], 0, []
    for name, image, dpi in pages:
        upright = prepare(image, dpi, denoise=False).image
        for truth, page in ((0, upright), (180, upright.rotate(180))):
            pair = upright_pair(page)
            start = time.monotonic()
            try:
                answer = client.complete(UPRIGHT, image_payload_from(pair, config.max_long_edge))
            except VisionError as exc:
                failed += 1
                print(f"{name:40s} {truth:3d}°  ошибка API: {exc}")
                continue
            seconds.append(time.monotonic() - start)
            said = parse_upright(parse_json(answer))
            ok = said == truth
            print(f"{name:40s} {truth:3d}°  модель: {said:3d}°  {'OK' if ok else 'ОШИБКА'}  {seconds[-1]:.1f} с", flush=True)
            if not ok:
                wrong.append((name, truth, answer.strip()[:120]))
                if args.save:
                    args.save.mkdir(parents=True, exist_ok=True)
                    pair.save(args.save / f"{Path(name).stem}-{truth}.png")
    asked = len(seconds)
    print(f"\nверно {asked - len(wrong)}/{asked}; сбоев API {failed}; в среднем {sum(seconds) / max(1, asked):.1f} с на вопрос")
    for name, truth, answer in wrong:
        print(f"  {name} ({truth}°): {answer!r}")
    return 0 if not wrong and not failed else 1


def image_payload_from(image: Image.Image, max_long_edge: int) -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pair.png"
        image.save(path)
        return image_payload(path, max_long_edge=max_long_edge)


if __name__ == "__main__":
    raise SystemExit(main())
