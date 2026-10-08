"""HTTP-клиент VLM: POST {base}/v1/chat/completions, тело как в Postman (без model)."""

from __future__ import annotations

import http.client
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
    timeout_s: float = 300.0
    max_retries: int = 2
    max_long_edge: int = 2048
    temperature: float = 0.2
    reasoning: str = "low"  # off | low | medium | high; размышление модели — основное время ответа

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
            timeout_s=_number("INGEST_VISION_TIMEOUT_S", 300, float),
            max_retries=_number("INGEST_VISION_MAX_RETRIES", 2, int),
            max_long_edge=_number("INGEST_VISION_MAX_LONG_EDGE", 2048, int),
            reasoning=_reasoning(),
        )


_REASONING = ("off", "low", "medium", "high")


def _reasoning() -> str:
    raw = (_env("INGEST_VISION_REASONING") or "low").lower()
    if raw not in _REASONING:
        raise VisionConfigError(f"INGEST_VISION_REASONING={raw!r}: expected one of {', '.join(_REASONING)}")
    return raw


def _number(name: str, default: Any, kind: Callable[[str], Any]) -> Any:
    raw = _env(name)
    try:
        return default if raw is None else kind(raw)
    except ValueError as exc:
        raise VisionConfigError(f"{name}={raw!r} is not a number") from exc


def build_body(
    prompt: str, image_url: str, model: str | None = None, temperature: float = 0.2, reasoning: str = "low"
) -> dict[str, Any]:
    thinking: dict[str, Any] = (
        {"enable_thinking": False} if reasoning == "off" else {"enable_thinking": True, "resolved_reasoning_effort": reasoning}
    )
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
        "chat_template_kwargs": thinking,
    }
    if model:
        body["model"] = model
    return body


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(p.get("text", "") for p in value if isinstance(p, dict))
    return value if isinstance(value, str) else ""


def _after_think(raw: str) -> str:
    """Ответ после <think>: шаблон Qwen3 иногда открывает тег сам, закрывающий приходит в тексте."""
    text = _THINK.sub("", raw)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def _think_body(raw: str) -> str:
    parts = [p.strip() for p in re.findall(r"<think>(.*?)</think>", raw, re.DOTALL) if p.strip()]
    return parts[-1] if parts else ""


def _content(data: Any) -> str:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionError(f"unexpected response: {str(data)[:200]}") from exc
    if not isinstance(message, dict):
        raise VisionError(f"unexpected response: {str(data)[:200]}")
    # Короткий ответ («B») Qwen3 часто оставляет только в reasoning_content, content = null.
    chunks = [_as_text(message.get("content"))]
    chunks += [_as_text(message.get(key)) for key in ("reasoning_content", "reasoning")]
    for raw in chunks:
        if text := _after_think(raw):
            return text
    for raw in chunks:
        if text := _think_body(raw):
            return text
    raise VisionError("empty response")


def _body(exc: urllib.error.HTTPError) -> str:
    """Причина от сервера (например, превышен контекст) — в сообщение об ошибке."""
    try:
        return " ".join(exc.read()[:300].decode("utf-8", "replace").split())
    except (OSError, http.client.HTTPException):
        return ""


class VisionClient:
    def __init__(self, config: VisionConfig, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.model = config.model
        self._sleep = sleep

    def complete(self, prompt: str, image_url: str) -> str:
        body = build_body(prompt, image_url, self.model, self.config.temperature, self.config.reasoning)
        data = json.dumps(body).encode("utf-8")
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
                error = VisionError(f"HTTP {exc.code} from {self.config.endpoint}: {_body(exc)}")
                if exc.code < 500 and exc.code != 429:
                    raise error from exc
            except TimeoutError as exc:  # сервер дальше считает брошенный запрос — повтор только удлинит очередь
                raise VisionError(f"timed out after {self.config.timeout_s:.0f} s (INGEST_VISION_TIMEOUT_S)") from exc
            except (urllib.error.URLError, ConnectionError, http.client.HTTPException) as exc:
                if isinstance(getattr(exc, "reason", None), TimeoutError):
                    raise VisionError(f"timed out after {self.config.timeout_s:.0f} s (INGEST_VISION_TIMEOUT_S)") from exc
                error = VisionError(f"{type(exc).__name__}: {exc}")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise VisionError(f"invalid JSON from {self.config.endpoint}") from exc
        raise error or VisionError("request failed")
