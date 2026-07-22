"""Deterministic punctuation-aware segmentation of finalized ASR text."""

import math
import re
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, List, Optional, Sequence, Set, Tuple

from staged_models import AsrFinal, EmissionReason, TextSegment


DEFAULT_ABBREVIATIONS = frozenset(
    {
        "mr.",
        "mrs.",
        "ms.",
        "dr.",
        "prof.",
        "rev.",
        "jr.",
        "sr.",
        "st.",
        "vs.",
        "e.g.",
        "i.e.",
        "a.m.",
        "p.m.",
    }
)

TERMINAL_PUNCTUATION = frozenset(".?!。？！…")
CLOSING_PUNCTUATION = frozenset("\"'”’»)]}")
NO_SPACE_BEFORE = TERMINAL_PUNCTUATION.union(CLOSING_PUNCTUATION).union(
    frozenset(",;:%")
)
NO_SPACE_AFTER = frozenset("“‘«([{/-")


@dataclass
class _BufferedPiece:
    final_id: int
    text: str
    received_monotonic_ms: float
    source_start_ms: Optional[float]
    source_end_ms: Optional[float]


class PunctuationSegmenter:
    """Accumulate ASR finals and emit ordered translation units.

    The object is intentionally synchronous and single-owner. A future ASR
    consumer task owns one instance per session and places returned segments on
    the bounded NMT queue.
    """

    def __init__(
        self,
        max_chars: int = 240,
        max_age_ms: float = 2_000,
        abbreviations: Optional[Iterable[str]] = None,
    ) -> None:
        if (
            not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or max_chars <= 0
        ):
            raise ValueError("max_chars must be a positive integer")
        if not isinstance(max_age_ms, (int, float)) or not math.isfinite(max_age_ms):
            raise ValueError("max_age_ms must be finite")
        if max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")

        configured = set(DEFAULT_ABBREVIATIONS)
        if abbreviations is not None:
            configured.update(_normalize_abbreviation(value) for value in abbreviations)

        self.max_chars = max_chars
        self.max_age_ms = float(max_age_ms)
        self.abbreviations: Set[str] = configured
        self._pieces: Deque[_BufferedPiece] = deque()
        self._next_sequence_id = 0
        self._last_final_id: Optional[int] = None
        self._last_observed_ms: Optional[float] = None
        self._closed = False

    @property
    def residual_text(self) -> str:
        return "".join(piece.text for piece in self._pieces).strip()

    @property
    def next_sequence_id(self) -> int:
        return self._next_sequence_id

    @property
    def closed(self) -> bool:
        return self._closed

    def push_final(self, final: AsrFinal) -> List[TextSegment]:
        """Append one finalized ASR fragment and emit every due segment."""
        if self._closed:
            raise RuntimeError("cannot push an ASR final after segmenter flush")
        if self._last_final_id is not None and final.final_id <= self._last_final_id:
            raise ValueError("ASR final IDs must be strictly increasing")
        self._observe_time(final.received_monotonic_ms)
        self._last_final_id = final.final_id

        text = final.text.strip()
        if not text:
            return self._drain(final.received_monotonic_ms, include_age=True)

        separator = self._separator_before(text)
        self._pieces.append(
            _BufferedPiece(
                final_id=final.final_id,
                text=separator + text,
                received_monotonic_ms=final.received_monotonic_ms,
                source_start_ms=final.source_start_ms,
                source_end_ms=final.source_end_ms,
            )
        )
        return self._drain(final.received_monotonic_ms, include_age=True)

    def emit_due(self, now_monotonic_ms: float) -> List[TextSegment]:
        """Emit an aged residual even when ASR has produced no new final."""
        if self._closed:
            return []
        self._observe_time(now_monotonic_ms)
        return self._drain(now_monotonic_ms, include_age=True)

    def flush(self, now_monotonic_ms: float) -> List[TextSegment]:
        """Drain normal cuts, emit the remaining residual once, and close."""
        if self._closed:
            return []
        self._observe_time(now_monotonic_ms)
        emitted = self._drain(now_monotonic_ms, include_age=False)
        if self.residual_text:
            emitted.append(self._emit(len(self._buffer()), EmissionReason.FINAL_FLUSH, now_monotonic_ms))
        self._closed = True
        return emitted

    def _observe_time(self, now_monotonic_ms: float) -> None:
        if not isinstance(now_monotonic_ms, (int, float)) or not math.isfinite(
            now_monotonic_ms
        ):
            raise ValueError("monotonic time must be finite")
        if now_monotonic_ms < 0:
            raise ValueError("monotonic time must be non-negative")
        if self._last_observed_ms is not None and now_monotonic_ms < self._last_observed_ms:
            raise ValueError("monotonic time cannot move backwards")
        self._last_observed_ms = now_monotonic_ms

    def _separator_before(self, incoming: str) -> str:
        existing = self._buffer()
        if not existing:
            return ""
        if existing[-1].isspace() or incoming[0].isspace():
            return ""
        if existing[-1] in NO_SPACE_AFTER:
            return ""
        if incoming[0] in "\"'":
            if self._incoming_straight_quote_is_closing(existing, incoming):
                return ""
            return " "
        if incoming[0] in NO_SPACE_BEFORE:
            return ""
        if existing[-1] in "\"'" and self._has_unclosed_quote(
            existing, existing[-1]
        ):
            return ""
        return " "

    @staticmethod
    def _incoming_straight_quote_is_closing(existing: str, incoming: str) -> bool:
        quote = incoming[0]
        if PunctuationSegmenter._has_unclosed_quote(existing, quote):
            return True
        # ASR can split contractions or possessives immediately before the
        # apostrophe. Treat that shape as punctuation rather than an opener.
        return (
            quote == "'"
            and existing[-1].isalnum()
            and len(incoming) > 1
            and incoming[1].isalnum()
        )

    @staticmethod
    def _has_unclosed_quote(text: str, quote: str) -> bool:
        if quote == "'" and text.endswith("'") and len(text) > 1 and text[-2].isalnum():
            return False

        quote_count = 0
        for index, character in enumerate(text):
            if character != quote:
                continue
            if (
                quote == "'"
                and 0 < index < len(text) - 1
                and text[index - 1].isalnum()
                and text[index + 1].isalnum()
            ):
                continue
            quote_count += 1
        return quote_count % 2 == 1

    def _drain(self, now_ms: float, include_age: bool) -> List[TextSegment]:
        emitted: List[TextSegment] = []
        while self.residual_text:
            buffer = self._buffer()
            boundary = self._first_terminal_boundary(buffer)
            if boundary is not None and boundary <= self.max_chars:
                emitted.append(self._emit(boundary, EmissionReason.PUNCTUATION, now_ms))
                continue
            if len(buffer) >= self.max_chars:
                cut = self._length_cut(buffer)
                emitted.append(self._emit(cut, EmissionReason.LENGTH, now_ms))
                continue
            if boundary is not None:
                emitted.append(self._emit(boundary, EmissionReason.PUNCTUATION, now_ms))
                continue
            break

        if include_age and self.residual_text:
            oldest = self._oldest_buffered_ms()
            if oldest is not None and now_ms - oldest >= self.max_age_ms:
                emitted.append(self._emit(len(self._buffer()), EmissionReason.AGE, now_ms))
        return emitted

    def _first_terminal_boundary(self, text: str) -> Optional[int]:
        for index, character in enumerate(text):
            if character not in TERMINAL_PUNCTUATION:
                continue
            if character == "." and self._period_is_protected(text, index):
                continue

            end = index + 1
            while end < len(text) and text[end] in TERMINAL_PUNCTUATION:
                end += 1
            while end < len(text) and text[end] in CLOSING_PUNCTUATION:
                end += 1

            if end < len(text) and not text[end].isspace() and character == ".":
                continue
            return end
        return None

    def _period_is_protected(self, text: str, index: int) -> bool:
        previous = text[index - 1] if index > 0 else ""
        following = text[index + 1] if index + 1 < len(text) else ""

        # Decimal, version, domain, or the interior of an initialism.
        if previous.isalnum() and following.isalnum():
            return True
        # Only the last dot in an ellipsis or punctuation run can terminate.
        if following == ".":
            return True

        prefix = text[: index + 1]
        token_match = re.search(r"([A-Za-z][A-Za-z.]*)\.$", prefix)
        if token_match:
            token = (token_match.group(1) + ".").lower()
            if token in self.abbreviations:
                return True

        # Conservative handling for initials and unlisted initialisms.
        if re.search(r"(?:^|[\s(\[{'\"“‘«])[A-Z]\.$", prefix):
            return True
        if re.search(r"(?:[A-Za-z]\.){2,}$", prefix):
            return True
        return False

    def _length_cut(self, text: str) -> int:
        limit = min(self.max_chars, len(text))
        if limit == len(text) or (limit < len(text) and text[limit].isspace()):
            return limit
        for index in range(limit, 0, -1):
            if text[index - 1].isspace():
                return index - 1 if index > 1 else limit
        return limit

    def _emit(
        self,
        cut: int,
        reason: EmissionReason,
        now_ms: float,
    ) -> TextSegment:
        raw, contributing = self._consume_prefix(cut)
        text = raw.strip()
        if not text:
            raise RuntimeError("segmenter attempted to emit an empty segment")

        starts = [
            piece.source_start_ms
            for piece in contributing
            if piece.source_start_ms is not None
        ]
        ends = [
            piece.source_end_ms
            for piece in contributing
            if piece.source_end_ms is not None
        ]
        buffered_since = min(piece.received_monotonic_ms for piece in contributing)
        final_ids = tuple(dict.fromkeys(piece.final_id for piece in contributing))
        # A combined envelope is complete at an endpoint only when every
        # contributing piece supplied that endpoint. Never make partial timing
        # look more precise by borrowing a start/end from another final.
        source_start_ms = (
            min(starts) if len(starts) == len(contributing) else None
        )
        source_end_ms = max(ends) if len(ends) == len(contributing) else None
        if (
            source_start_ms is not None
            and source_end_ms is not None
            and source_end_ms < source_start_ms
        ):
            # Partial timing envelopes cannot be combined reliably. Preserve
            # text/provenance and report the aggregate source range as unknown.
            source_start_ms = None
            source_end_ms = None

        segment = TextSegment(
            sequence_id=self._next_sequence_id,
            text=text,
            reason=reason,
            emitted_monotonic_ms=now_ms,
            buffered_since_monotonic_ms=buffered_since,
            source_start_ms=source_start_ms,
            source_end_ms=source_end_ms,
            contributing_final_ids=final_ids,
        )
        self._next_sequence_id += 1
        return segment

    def _consume_prefix(self, count: int) -> Tuple[str, Sequence[_BufferedPiece]]:
        if count <= 0 or count > len(self._buffer()):
            raise RuntimeError("invalid segment prefix length")

        remaining = count
        chunks: List[str] = []
        contributing: List[_BufferedPiece] = []
        while remaining > 0 and self._pieces:
            piece = self._pieces[0]
            take = min(remaining, len(piece.text))
            consumed = piece.text[:take]
            chunks.append(consumed)
            if consumed.strip():
                contributing.append(piece)
            remaining -= take
            if take == len(piece.text):
                self._pieces.popleft()
            else:
                piece.text = piece.text[take:]

        if remaining:
            raise RuntimeError("segment prefix exceeded buffered text")
        self._discard_leading_whitespace()
        if not contributing:
            raise RuntimeError("segment prefix had no contributing ASR final")
        return "".join(chunks), contributing

    def _discard_leading_whitespace(self) -> None:
        while self._pieces:
            piece = self._pieces[0]
            stripped = piece.text.lstrip()
            if stripped:
                piece.text = stripped
                return
            self._pieces.popleft()

    def _oldest_buffered_ms(self) -> Optional[float]:
        for piece in self._pieces:
            if piece.text.strip():
                return piece.received_monotonic_ms
        return None

    def _buffer(self) -> str:
        return "".join(piece.text for piece in self._pieces)


def _normalize_abbreviation(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("abbreviations cannot be empty")
    if not normalized.endswith("."):
        normalized += "."
    return normalized
