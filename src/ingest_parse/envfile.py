"""scan.env в текущей папке → переменные процесса. Уже заданные в консоли не трогаем."""

from __future__ import annotations

import os
from pathlib import Path


def load_scan_env(path: Path | None = None) -> None:
    file = path if path is not None else Path.cwd() / "scan.env"
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or not value or os.environ.get(key, "").strip():
            continue
        os.environ[key] = value
