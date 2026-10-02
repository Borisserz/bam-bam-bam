"""Описание картинок из media/ через VLM (OpenAI-совместимый /v1/chat/completions)."""

from ingest_parse.vision.client import VisionClient, VisionConfig, VisionConfigError, VisionError
from ingest_parse.vision.enrich import enrich_markdown

__all__ = ["VisionClient", "VisionConfig", "VisionConfigError", "VisionError", "enrich_markdown"]
