"""Кэш ответов VLM: <папка картинки>/.vision-cache/<sha256>-<режим>[-<model>].json."""

from __future__ import annotations

import json
import re
from pathlib import Path


class VisionCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, sha256: str, mode: str, model: str | None) -> Path:
        suffix = f"-{re.sub(r'[^A-Za-z0-9._-]', '_', model)}" if model else ""
        return self.directory / f"{sha256}-{mode}{suffix}.json"

    def get(self, sha256: str, mode: str, model: str | None = None) -> str | None:
        try:
            data = json.loads(self._path(sha256, mode, model).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        answer = data.get("answer") if isinstance(data, dict) else None
        return answer if isinstance(answer, str) and answer else None

    def put(self, sha256: str, mode: str, model: str | None, answer: str) -> None:
        payload = {"sha256": sha256, "mode": mode, "model": model, "answer": answer}
        try:  # Windows: путь длиннее 260 символов — без кэша, ответ всё равно используется
            self.directory.mkdir(parents=True, exist_ok=True)
            self._path(sha256, mode, model).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
