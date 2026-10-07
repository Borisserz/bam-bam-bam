"""HTTP-клиент VLM: POST {base}/v1/chat/completions, тело как в Postman (без model)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.S)


class VisionConfigError(ValueError):
    pass


class VisionError(RuntimeError):
    pass


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True)
class VisionConfig:
    base_url: str
    api_key: str | None = None
    model: str | None = None
    timeout_s: float = 60.0
    max_retries: int = 2
    max_long_edge: int = 2048
    temperature: float = 0.2

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        base = base.removesuffix("/v1/chat/completions").removesuffix("/v1")
        return f"{base}/v1/chat/completions"

    @classmethod
    def from_env(cls) -> VisionConfig:
        base = _env("INGEST_VISION_API_BASE_URL", "VISION_API_BASE_URL")
        if base is None:
            raise VisionConfigError(
                "Vision API is not configured: set VISION_API_BASE_URL "
                "(e.g. http://localhost:8080 on the model PC, http://192.168.4.101:8080 from LAN)"
            )
        return cls(
            base_url=base,
            api_key=_env("INGEST_VISION_API_KEY", "VISION_API_KEY"),
            model=_env("INGEST_VISION_MODEL", "VISION_MODEL"),
            timeout_s=float(_env("INGEST_VISION_TIMEOUT_S") or 60),
            max_retries=int(_env("INGEST_VISION_MAX_RETRIES") or 2),
            max_long_edge=int(_env("INGEST_VISION_MAX_LONG_EDGE") or 2048),
        )


def build_body(prompt: str, image_url: str, model: str | None = None, temperature: float = 0.2) -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": ""},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": True, "resolved_reasoning_effort": "high"},
    }
    if model:
        body["model"] = model
    return body


def _content(data: Any) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionError(f"unexpected response: {str(data)[:200]}") from exc
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    text = _THINK.sub("", content or "").strip()
    if not text:
        raise VisionError("empty response")
    return text


class VisionClient:
    def __init__(self, config: VisionConfig, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.model = config.model
        self._sleep = sleep

    def complete(self, prompt: str, image_url: str) -> str:
        data = json.dumps(build_body(prompt, image_url, self.model, self.config.temperature)).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            if attempt:
                self._sleep(float(attempt))
            request = urllib.request.Request(self.config.endpoint, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
                    return _content(json.loads(response.read().decode("utf-8")))
            except urllib.error.HTTPError as exc:
                error = VisionError(f"HTTP {exc.code} from {self.config.endpoint}")
                if exc.code < 500 and exc.code != 429:
                    raise error from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                error = VisionError(f"{type(exc).__name__}: {exc}")
            except json.JSONDecodeError as exc:
                raise VisionError(f"invalid JSON from {self.config.endpoint}") from exc
        raise error or VisionError("request failed")
