"""Shared, dependency-free text fixture for matched TTS comparisons."""

from __future__ import annotations

from typing import Any


FIXTURE_ID = "neutral-spanish-streaming-tts"
FIXTURE_VERSION = 1
DEFAULT_TEXT = (
    "La traducción en tiempo real debe mantener un ritmo claro y constante."
)


def identify_text(
    text: str,
    *,
    custom_override: bool = False,
) -> dict[str, Any]:
    """Identify the reference fixture without exposing its text."""
    return {
        "fixture_id": FIXTURE_ID,
        "fixture_version": FIXTURE_VERSION,
        "comparison_status": (
            "matched"
            if text == DEFAULT_TEXT and not custom_override
            else "custom_unmatched"
        ),
        "character_count": len(text),
        "utf8_byte_count": len(text.encode("utf-8")),
    }
