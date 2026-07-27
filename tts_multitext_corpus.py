"""Versioned, sanitized Spanish fixtures for direct TTS comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tts_comparison_fixture import DEFAULT_TEXT


CORPUS_ID = "neutral-spanish-multitext-tts"
CORPUS_VERSION = 1


@dataclass(frozen=True)
class CorpusItem:
    """One public, non-customer TTS fixture."""

    fixture_id: str
    category: str
    text: str

    def identity(self) -> dict[str, Any]:
        """Return a text-free identity suitable for diagnostic JSON."""
        return {
            "fixture_id": self.fixture_id,
            "category": self.category,
            "character_count": len(self.text),
            "utf8_byte_count": len(self.text.encode("utf-8")),
        }


ITEMS = (
    CorpusItem(
        fixture_id="fixture-001",
        category="micro_response",
        text="De acuerdo.",
    ),
    CorpusItem(
        fixture_id="fixture-002",
        category="short_statement",
        text="Gracias por acompañarnos hoy.",
    ),
    CorpusItem(
        fixture_id="fixture-003",
        category="medium_neutral",
        text=DEFAULT_TEXT,
    ),
    CorpusItem(
        fixture_id="fixture-004",
        category="punctuation_question_exclamation",
        text="¿Están listos? ¡Entonces empecemos, sin perder el ritmo!",
    ),
    CorpusItem(
        fixture_id="fixture-005",
        category="expressive_punchline_like",
        text="Prometí ser breve… y todos miraron el reloj.",
    ),
    CorpusItem(
        fixture_id="fixture-006",
        category="numbers_long_clause",
        text=(
            "La sesión comienza a las diez, incluye dos pausas y termina "
            "a las once y cuarto."
        ),
    ),
)


def validate_corpus() -> tuple[CorpusItem, ...]:
    """Validate and return the immutable formal corpus."""
    if len(ITEMS) != 6:
        raise RuntimeError("the formal multi-text corpus must contain six items")
    fixture_ids = [item.fixture_id for item in ITEMS]
    categories = [item.category for item in ITEMS]
    texts = [item.text for item in ITEMS]
    if len(set(fixture_ids)) != len(fixture_ids):
        raise RuntimeError("corpus fixture IDs must be unique")
    if len(set(categories)) != len(categories):
        raise RuntimeError("corpus categories must be unique")
    if len(set(texts)) != len(texts):
        raise RuntimeError("corpus texts must be unique")
    for index, item in enumerate(ITEMS, start=1):
        if item.fixture_id != f"fixture-{index:03d}":
            raise RuntimeError("corpus fixture IDs must be sequential")
        if not item.category or not item.text.strip():
            raise RuntimeError("corpus categories and texts must be nonempty")
        if "\x00" in item.text or "\r" in item.text or "\n" in item.text:
            raise RuntimeError("corpus text must be a single safe line")
    return ITEMS


def corpus_identity() -> dict[str, Any]:
    """Return the complete text-free corpus identity."""
    items = validate_corpus()
    return {
        "corpus_id": CORPUS_ID,
        "corpus_version": CORPUS_VERSION,
        "fixture_count": len(items),
        "fixtures": [item.identity() for item in items],
    }
