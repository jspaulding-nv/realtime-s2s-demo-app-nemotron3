"""Deterministic, lossless splitting of translated text for TTS.

The splitter normalizes a single NMT result to NFC, collapses each run of
Unicode whitespace to one ASCII space, and removes leading/trailing
whitespace.  Returned chunk payloads are stripped for TTS.  The whitespace
removed at a chunk boundary is retained as ``separator_before`` metadata, so
``reconstruct_target_text`` restores the normalized parent exactly, including
when a long token has to be split at a hard character boundary.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


__all__ = [
    "TargetTextChunk",
    "TargetTextSplitError",
    "normalize_target_text",
    "reconstruct_target_text",
    "split_target_text",
]


_WHITESPACE = re.compile(r"\s+")
_SENTENCE_PUNCTUATION = frozenset(".?!…")
_SOFT_PUNCTUATION = frozenset(",;:—–")
_FIXED_CLOSERS = frozenset("”’»)]}")
_STRAIGHT_QUOTES = frozenset("\"'")


class TargetTextSplitError(ValueError):
    """Raised when text cannot satisfy the chunking contract."""


@dataclass(frozen=True)
class TargetTextChunk:
    """One trimmed TTS payload and its normalized parent separator.

    ``separator_before`` is empty for the first chunk and for a hard split
    inside a token.  It is one ASCII space when the source boundary contained
    normalized whitespace.
    """

    text: str
    separator_before: str = ""

    def __post_init__(self) -> None:
        if not self.text or self.text != self.text.strip():
            raise ValueError("chunk text must be non-empty and stripped")
        if self.separator_before not in {"", " "}:
            raise ValueError("chunk separator must be empty or one space")
        if not _has_substance(self.text):
            raise ValueError("chunk text must contain a letter or number")


def normalize_target_text(text: str) -> str:
    """Return the splitter's canonical NFC and whitespace-normalized text."""
    if not isinstance(text, str):
        raise TypeError("target text must be a string")
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def reconstruct_target_text(chunks: Sequence[TargetTextChunk]) -> str:
    """Reconstruct the normalized parent represented by ``chunks``."""
    return "".join(chunk.separator_before + chunk.text for chunk in chunks)


def split_target_text(
    text: str,
    *,
    max_chars: int,
    min_chars: int = 12,
) -> Tuple[TargetTextChunk, ...]:
    """Split one translated parent into ordered, bounded TTS payloads.

    Boundaries are selected deterministically in this order:

    1. sentence-ending punctuation;
    2. comma, semicolon, colon, or dash;
    3. whitespace;
    4. a hard Unicode-code-point boundary.

    Within one boundary class, the latest feasible boundary is selected.
    Feasibility is computed for the complete parent before cuts are chosen, so
    a punctuation suffix or closer is never stranded in a punctuation-only
    chunk.  Adjacent chunks shorter than ``min_chars`` are then merged when the
    merged payload still fits ``max_chars``.
    """
    if (
        not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
        or max_chars <= 0
    ):
        raise ValueError("max_chars must be a positive integer")
    if (
        not isinstance(min_chars, int)
        or isinstance(min_chars, bool)
        or min_chars <= 0
    ):
        raise ValueError("min_chars must be a positive integer")

    normalized = normalize_target_text(text)
    if not normalized:
        raise TargetTextSplitError("target text is empty after normalization")
    if not _has_substance(normalized):
        raise TargetTextSplitError(
            "target text must contain at least one letter or number"
        )

    feasible = _compute_feasible_starts(normalized, max_chars)
    if not feasible.get(0, False):
        raise TargetTextSplitError(
            "target text cannot be split without a punctuation-only chunk"
        )

    chunks: List[TargetTextChunk] = []
    start = 0
    separator_before = ""
    text_length = len(normalized)

    while start < text_length:
        candidates = _feasible_cuts(normalized, start, max_chars, feasible)
        if not candidates:
            raise TargetTextSplitError(
                "target text cannot satisfy the character cap"
            )

        if text_length - start <= max_chars:
            cut = text_length
        else:
            cut = min(
                candidates,
                key=lambda candidate: (
                    _boundary_priority(normalized, start, candidate),
                    -candidate,
                ),
            )

        payload = normalized[start:cut]
        chunks.append(
            TargetTextChunk(
                text=payload,
                separator_before=separator_before,
            )
        )
        start, separator_before = _next_start(normalized, cut)

    chunks = _merge_tiny_adjacent_chunks(
        chunks,
        max_chars=max_chars,
        min_chars=min(min_chars, max_chars),
    )

    result = tuple(chunks)
    if any(len(chunk.text) > max_chars for chunk in result):
        raise AssertionError("splitter emitted a payload over the character cap")
    if reconstruct_target_text(result) != normalized:
        raise AssertionError("splitter failed its lossless reconstruction contract")
    return result


def _compute_feasible_starts(text: str, max_chars: int) -> Dict[int, bool]:
    """Return whether each non-whitespace index can begin a valid suffix."""
    feasible: Dict[int, bool] = {len(text): True}

    for start in range(len(text) - 1, -1, -1):
        if text[start].isspace():
            continue
        feasible[start] = bool(
            _feasible_cuts(text, start, max_chars, feasible)
        )

    return feasible


def _feasible_cuts(
    text: str,
    start: int,
    max_chars: int,
    feasible: Dict[int, bool],
) -> List[int]:
    cuts: List[int] = []
    limit = min(len(text), start + max_chars)

    for cut in range(start + 1, limit + 1):
        if text[cut - 1].isspace():
            continue
        payload = text[start:cut]
        if not _has_substance(payload):
            continue

        next_start, _ = _next_start(text, cut)
        if (
            next_start < len(text)
            and unicodedata.category(text[next_start]).startswith("M")
        ):
            continue
        if next_start < len(text) and _starts_with_closing_punctuation(
            text, next_start
        ):
            continue
        if feasible.get(next_start, False):
            cuts.append(cut)

    return cuts


def _next_start(text: str, cut: int) -> Tuple[int, str]:
    if cut < len(text) and text[cut].isspace():
        return cut + 1, " "
    return cut, ""


def _boundary_priority(text: str, start: int, cut: int) -> int:
    punctuation_index = cut - 1
    while punctuation_index >= start and _is_closer_at(
        text, punctuation_index
    ):
        punctuation_index -= 1

    if (
        punctuation_index >= start
        and text[punctuation_index] in _SENTENCE_PUNCTUATION
    ):
        return 0
    if (
        punctuation_index >= start
        and text[punctuation_index] in _SOFT_PUNCTUATION
    ):
        return 1
    if cut < len(text) and text[cut].isspace():
        return 2
    return 3


def _starts_with_closing_punctuation(text: str, index: int) -> bool:
    character = text[index]
    return (
        character in _SENTENCE_PUNCTUATION
        or character in _SOFT_PUNCTUATION
        or _is_closer_at(text, index)
    )


def _is_closer_at(text: str, index: int) -> bool:
    character = text[index]
    if character in _FIXED_CLOSERS:
        return True
    if character not in _STRAIGHT_QUOTES:
        return False

    # A straight quote following normalized whitespace is an opener.  A quote
    # following content is a closer, except for apostrophes inside a word.
    if index == 0 or text[index - 1].isspace():
        return False
    if (
        character == "'"
        and index + 1 < len(text)
        and text[index - 1].isalnum()
        and text[index + 1].isalnum()
    ):
        return False
    return True


def _has_substance(text: str) -> bool:
    return any(
        unicodedata.category(character).startswith(("L", "N"))
        for character in text
    )


def _merge_tiny_adjacent_chunks(
    chunks: List[TargetTextChunk],
    *,
    max_chars: int,
    min_chars: int,
) -> List[TargetTextChunk]:
    """Merge a tiny chunk with an adjacent sibling whenever it fits."""
    merged = list(chunks)
    index = 0

    while index < len(merged):
        if len(merged[index].text) >= min_chars:
            index += 1
            continue

        if index > 0:
            combined_text = (
                merged[index - 1].text
                + merged[index].separator_before
                + merged[index].text
            )
            if len(combined_text) <= max_chars:
                merged[index - 1] = TargetTextChunk(
                    text=combined_text,
                    separator_before=merged[index - 1].separator_before,
                )
                del merged[index]
                index = max(0, index - 1)
                continue

        if index + 1 < len(merged):
            combined_text = (
                merged[index].text
                + merged[index + 1].separator_before
                + merged[index + 1].text
            )
            if len(combined_text) <= max_chars:
                merged[index] = TargetTextChunk(
                    text=combined_text,
                    separator_before=merged[index].separator_before,
                )
                del merged[index + 1]
                continue

        index += 1

    return merged
