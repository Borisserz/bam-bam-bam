"""Кладёт md, лог и картинки прогона в приватный репозиторий Borisserz/bam-bam-bam-scans.

С рабочего ПК, после полного прогона:

    uv run ingest-parse 123.pdf -o out\\scan.md --scan-layout --vision 2>&1 | Tee-Object -FilePath out\\scan.log
    uv run python scripts\\push_scan_debug.py out

scan.env и .vision-cache не копируются. Код этого репозитория туда не входит.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

REPO = "Borisserz/bam-bam-bam-scans"
_SKIP_DIRS = {".git", ".vision-cache", "__pycache__", ".ruff_cache"}
_SKIP_NAMES = {"scan.env", ".env", ".DS_Store"}


def publish_files(out: Path) -> list[Path]:
    """Файлы прогона: markdown, лог, страницы, рамки, вырезы. Кэш и секреты — нет."""
    found: list[Path] = []
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _SKIP_NAMES or set(path.relative_to(out).parts) & _SKIP_DIRS:
            continue
        if path.suffix.lower() not in {".md", ".log", ".png", ".jpg", ".jpeg", ".json", ".txt"}:
            continue
        found.append(path)
    return found


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)


def ensure_repo(dest: Path) -> None:
    """Локальный клон приватного репозитория. Нет на GitHub — создаётся закрытым."""
    if (dest / ".git").is_dir():
        _run(["git", "pull", "--ff-only"], cwd=dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(
        ["gh", "repo", "clone", REPO, str(dest)], text=True, capture_output=True, check=False
    )
    if cloned.returncode == 0:
        return
    _run(
        [
            "gh", "repo", "create", REPO, "--private",
            "--description", "Закрытые прогоны сканов: md, рамки, вырезы.",
        ]
    )
    _run(["gh", "repo", "clone", REPO, str(dest)])


def push_out(out: Path, dest: Path) -> str:
    """Копирует прогон в runs/<время>-<имя md> и пушит. Возвращает путь папки в клоне."""
    files = publish_files(out)
    if not files:
        raise SystemExit(f"в {out} нет md, лога и картинок")
    ensure_repo(dest)
    stem = next((p.stem for p in files if p.suffix.lower() == ".md"), out.name)
    folder = dest / "runs" / f"{datetime.now().astimezone().strftime('%Y%m%d-%H%M')}-{stem}"
    if folder.exists():
        raise SystemExit(f"папка уже есть: {folder}")
    for src in files:
        target = folder / src.relative_to(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    readme = dest / "README.md"
    if not readme.exists():
        readme.write_text(
            "Закрытые прогоны ingest-parse. В runs/ лежат md, лог консоли и картинки рамок.\n",
            encoding="utf-8",
        )
    _run(["git", "add", "-A"], cwd=dest)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, text=True, capture_output=True, check=True)
    if not status.stdout.strip():
        raise SystemExit("новых файлов нет")
    _run(["git", "commit", "-m", f"scan: {stem}"], cwd=dest)
    _run(["git", "push", "-u", "origin", "HEAD"], cwd=dest)
    return str(folder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Залить out/ в приватный репозиторий прогонов.")
    parser.add_argument("out", type=Path, help="Папка с scan.md, scan.log и media/")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.home() / "Projects" / "bam-bam-bam-scans",
        help="Куда клонировать приватный репозиторий",
    )
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if not out.is_dir():
        print(f"нет папки {out}")
        return 1
    try:
        folder = push_out(out, args.repo.resolve())
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        print(detail or f"команда не прошла: {' '.join(exc.cmd)}")
        return 1
    print(folder)
    print(f"https://github.com/{REPO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
