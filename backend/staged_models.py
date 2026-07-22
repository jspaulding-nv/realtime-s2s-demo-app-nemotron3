"""Typed records shared by the staged speech pipeline foundation."""

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class EmissionReason(str, Enum):
    """Why buffered ASR text became a translation segment."""

    PUNCTUATION = "punctuation"
    LENGTH = "length"
    AGE = "age"
    FINAL_FLUSH = "final_flush"


class ASRStreamEventKind(str, Enum):
    """Ordered event kinds produced by one direct-ASR stream."""

    INTERIM = "interim"
    FINAL = "final"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass(frozen=True)
class ASRTranscript:
    """One interim or final result returned by streaming ASR."""

    text: str
    is_final: bool
    received_monotonic_ms: float
    audio_processed_s: float = 0.0
    stability: float = 0.0
    confidence: float = 0.0
    source_start_ms: Optional[float] = None
    source_end_ms: Optional[float] = None
    detected_languages: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_nonnegative_finite(
            "received_monotonic_ms", self.received_monotonic_ms
        )
        _validate_nonnegative_finite("audio_processed_s", self.audio_processed_s)
        _validate_finite("stability", self.stability)
        _validate_finite("confidence", self.confidence)
        _validate_source_range(self.source_start_ms, self.source_end_ms)


@dataclass(frozen=True)
class AsrFinal:
    """A finalized ASR fragment with identity independent of segment IDs."""

    final_id: int
    text: str
    received_monotonic_ms: float
    source_start_ms: Optional[float] = None
    source_end_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if self.final_id < 0:
            raise ValueError("final_id must be non-negative")
        _validate_nonnegative_finite(
            "received_monotonic_ms", self.received_monotonic_ms
        )
        _validate_source_range(self.source_start_ms, self.source_end_ms)

    @classmethod
    def from_transcript(
        cls,
        final_id: int,
        transcript: ASRTranscript,
    ) -> "AsrFinal":
        if not transcript.is_final:
            raise ValueError("only final ASR transcripts can become AsrFinal records")
        return cls(
            final_id=final_id,
            text=transcript.text,
            received_monotonic_ms=transcript.received_monotonic_ms,
            source_start_ms=transcript.source_start_ms,
            source_end_ms=transcript.source_end_ms,
        )


@dataclass(frozen=True)
class TextSegment:
    """One ordered text unit ready for the NMT stage."""

    sequence_id: int
    text: str
    reason: EmissionReason
    emitted_monotonic_ms: float
    buffered_since_monotonic_ms: float
    source_start_ms: Optional[float]
    source_end_ms: Optional[float]
    contributing_final_ids: Tuple[int, ...]

    def __post_init__(self) -> None:
        if self.sequence_id < 0:
            raise ValueError("sequence_id must be non-negative")
        if not self.text.strip():
            raise ValueError("segment text must contain non-whitespace content")
        _validate_nonnegative_finite(
            "emitted_monotonic_ms", self.emitted_monotonic_ms
        )
        _validate_nonnegative_finite(
            "buffered_since_monotonic_ms", self.buffered_since_monotonic_ms
        )
        if self.emitted_monotonic_ms < self.buffered_since_monotonic_ms:
            raise ValueError("segment cannot be emitted before it was buffered")
        if not self.contributing_final_ids:
            raise ValueError("segment must identify at least one contributing ASR final")
        _validate_source_range(self.source_start_ms, self.source_end_ms)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["reason"] = self.reason.value
        return payload


@dataclass(frozen=True)
class ASRStreamEvent:
    """One FIFO event crossing from the blocking ASR worker to asyncio."""

    kind: ASRStreamEventKind
    transcript: Optional[ASRTranscript] = None
    final: Optional[AsrFinal] = None
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ASRStreamEventKind):
            raise ValueError("kind must be an ASRStreamEventKind")
        if self.kind is ASRStreamEventKind.INTERIM:
            if not isinstance(self.transcript, ASRTranscript) or self.transcript.is_final:
                raise ValueError("interim events require an interim transcript")
            if self.final is not None or self.error:
                raise ValueError("interim events cannot carry final/error payloads")
        elif self.kind is ASRStreamEventKind.FINAL:
            if not isinstance(self.final, AsrFinal):
                raise ValueError("final events require an AsrFinal payload")
            if self.transcript is not None or self.error:
                raise ValueError("final events cannot carry transcript/error payloads")
        elif self.kind is ASRStreamEventKind.ERROR:
            if not self.error or self.transcript is not None or self.final is not None:
                raise ValueError("error events require only a non-empty error message")
        elif self.transcript is not None or self.final is not None or self.error:
            raise ValueError("complete events cannot carry a payload")


@dataclass(frozen=True)
class PipelineEvent:
    """Minimum event contract for future per-stage queue telemetry."""

    session_id: str
    stage: str
    event: str
    monotonic_ms: float
    sequence_id: Optional[int] = None
    asr_final_id: Optional[int] = None
    contributing_final_ids: Tuple[int, ...] = ()
    emission_reason: Optional[EmissionReason] = None
    source_start_ms: Optional[float] = None
    source_end_ms: Optional[float] = None
    queue_depth: Optional[int] = None
    text_chars: int = 0
    audio_bytes: int = 0
    audio_duration_ms: float = 0.0
    retry_count: int = 0
    error_code: str = ""

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id is required")
        if not self.stage or not self.event:
            raise ValueError("stage and event are required")
        _validate_nonnegative_finite("monotonic_ms", self.monotonic_ms)
        if self.sequence_id is not None and self.sequence_id < 0:
            raise ValueError("sequence_id must be non-negative")
        if self.asr_final_id is not None and self.asr_final_id < 0:
            raise ValueError("asr_final_id must be non-negative")
        if any(final_id < 0 for final_id in self.contributing_final_ids):
            raise ValueError("contributing_final_ids must be non-negative")
        if len(set(self.contributing_final_ids)) != len(self.contributing_final_ids):
            raise ValueError("contributing_final_ids must not contain duplicates")
        if (
            self.emission_reason is not None
            and not isinstance(self.emission_reason, EmissionReason)
        ):
            raise ValueError("emission_reason must be an EmissionReason")
        if self.queue_depth is not None and self.queue_depth < 0:
            raise ValueError("queue_depth must be non-negative")
        if min(self.text_chars, self.audio_bytes, self.retry_count) < 0:
            raise ValueError("event counters must be non-negative")
        _validate_nonnegative_finite("audio_duration_ms", self.audio_duration_ms)
        _validate_source_range(self.source_start_ms, self.source_end_ms)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if self.emission_reason is not None:
            payload["emission_reason"] = self.emission_reason.value
        return payload


def _validate_source_range(
    source_start_ms: Optional[float],
    source_end_ms: Optional[float],
) -> None:
    if source_start_ms is not None:
        _validate_nonnegative_finite("source_start_ms", source_start_ms)
    if source_end_ms is not None:
        _validate_nonnegative_finite("source_end_ms", source_end_ms)
    if (
        source_start_ms is not None
        and source_end_ms is not None
        and source_end_ms < source_start_ms
    ):
        raise ValueError("source_end_ms cannot precede source_start_ms")


def _validate_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _validate_nonnegative_finite(name: str, value: float) -> None:
    _validate_finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
