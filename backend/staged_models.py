"""Typed records shared by the staged speech pipeline foundation."""

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple


ASR_TIMING_BASIS_WORD_OFFSETS = "word_offsets"
ASR_TIMING_BASIS_INCOMPLETE_WORD_OFFSETS = "incomplete_word_offsets"
ASR_TIMING_BASIS_AUDIO_PROCESSED_END_ONLY = "audio_processed_end_only"
ASR_TIMING_BASIS_UNAVAILABLE = "unavailable"
ASR_TIMING_BASES = frozenset(
    {
        ASR_TIMING_BASIS_WORD_OFFSETS,
        ASR_TIMING_BASIS_INCOMPLETE_WORD_OFFSETS,
        ASR_TIMING_BASIS_AUDIO_PROCESSED_END_ONLY,
        ASR_TIMING_BASIS_UNAVAILABLE,
    }
)

ASR_BOUNDARY_PRESENCE_PRESENT = "present"
ASR_BOUNDARY_PRESENCE_ABSENT = "absent"
ASR_BOUNDARY_PRESENCE_UNOBSERVABLE = "unobservable"
ASR_BOUNDARY_PRESENCES = frozenset(
    {
        ASR_BOUNDARY_PRESENCE_PRESENT,
        ASR_BOUNDARY_PRESENCE_ABSENT,
        ASR_BOUNDARY_PRESENCE_UNOBSERVABLE,
    }
)
ASR_BOUNDARY_NUMERIC_CLASSES = (
    "not_available",
    "unparseable",
    "nonfinite",
    "negative",
    "zero",
    "positive",
)
ASR_WORD_TIMING_RELATIONS = frozenset(
    {
        "end_after_start",
        "equal",
        "end_before_start",
        "not_comparable",
    }
)
ASR_WORD_TIMING_SHAPES = (
    "valid",
    "absent_boundary",
    "unparseable_boundary",
    "nonfinite_boundary",
    "negative_boundary",
    "zero_length",
    "reversed",
)


@dataclass(frozen=True)
class ASRBoundaryObservation:
    """One raw timing scalar without transcript or token content."""

    presence: str
    numeric_class: str
    finite_value_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if self.presence not in ASR_BOUNDARY_PRESENCES:
            raise ValueError(f"unsupported boundary presence: {self.presence!r}")
        if self.numeric_class not in ASR_BOUNDARY_NUMERIC_CLASSES:
            raise ValueError(
                f"unsupported boundary numeric class: {self.numeric_class!r}"
            )
        if (
            self.presence == ASR_BOUNDARY_PRESENCE_ABSENT
            and self.numeric_class != "not_available"
        ):
            raise ValueError(
                "an absent boundary cannot carry an observed numeric class"
            )
        finite_class = self.numeric_class in {"negative", "zero", "positive"}
        if finite_class != (self.finite_value_ms is not None):
            raise ValueError(
                "finite_value_ms must exist exactly for finite numeric classes"
            )
        if self.finite_value_ms is None:
            return
        if (
            not isinstance(self.finite_value_ms, (int, float))
            or isinstance(self.finite_value_ms, bool)
        ):
            raise ValueError("finite_value_ms must be numeric")
        _validate_finite("finite_value_ms", self.finite_value_ms)
        if (
            (self.numeric_class == "negative" and self.finite_value_ms >= 0)
            or (self.numeric_class == "zero" and self.finite_value_ms != 0)
            or (self.numeric_class == "positive" and self.finite_value_ms <= 0)
        ):
            raise ValueError(
                "finite_value_ms does not match its numeric class"
            )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "presence": self.presence,
            "numeric_class": self.numeric_class,
        }
        if self.finite_value_ms is not None:
            payload["finite_value_ms"] = self.finite_value_ms
        return payload


@dataclass(frozen=True)
class ASRWordTimingEntry:
    """Privacy-safe timing shape for one word entry."""

    word_index: int
    start: ASRBoundaryObservation
    end: ASRBoundaryObservation
    numeric_relation: str
    shape: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.word_index, int)
            or isinstance(self.word_index, bool)
            or self.word_index < 0
        ):
            raise ValueError("word_index must be a non-negative integer")
        _validate_word_timing_shape(
            start=self.start,
            end=self.end,
            numeric_relation=self.numeric_relation,
            shape=self.shape,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "word_index": self.word_index,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "numeric_relation": self.numeric_relation,
            "shape": self.shape,
        }


@dataclass(frozen=True)
class ASRWordTimingEnvelope:
    """Raw first-start/last-end envelope for one ASR result."""

    start_word_index: int
    end_word_index: int
    start: ASRBoundaryObservation
    end: ASRBoundaryObservation
    numeric_relation: str
    shape: str
    usable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.usable, bool):
            raise ValueError("usable must be boolean")
        for name, value in (
            ("start_word_index", self.start_word_index),
            ("end_word_index", self.end_word_index),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.end_word_index < self.start_word_index:
            raise ValueError("end_word_index cannot precede start_word_index")
        _validate_word_timing_shape(
            start=self.start,
            end=self.end,
            numeric_relation=self.numeric_relation,
            shape=self.shape,
        )
        if self.usable != (self.shape == "valid"):
            raise ValueError("only a valid word-timing envelope is usable")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_word_index": self.start_word_index,
            "end_word_index": self.end_word_index,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "numeric_relation": self.numeric_relation,
            "shape": self.shape,
            "usable": self.usable,
        }


@dataclass(frozen=True)
class ASRWordTimingShapeCounts:
    """Fixed counters for all mutually exclusive word-entry shapes."""

    valid: int = 0
    absent_boundary: int = 0
    unparseable_boundary: int = 0
    nonfinite_boundary: int = 0
    negative_boundary: int = 0
    zero_length: int = 0
    reversed: int = 0

    def __post_init__(self) -> None:
        _validate_counter_values(self.to_dict())

    def to_dict(self) -> Dict[str, int]:
        return {
            shape: getattr(self, shape)
            for shape in ASR_WORD_TIMING_SHAPES
        }


@dataclass(frozen=True)
class ASRBoundaryNumericClassCounts:
    """Fixed counters for raw boundary numeric classes."""

    not_available: int = 0
    unparseable: int = 0
    nonfinite: int = 0
    negative: int = 0
    zero: int = 0
    positive: int = 0

    def __post_init__(self) -> None:
        _validate_counter_values(self.to_dict())

    def to_dict(self) -> Dict[str, int]:
        return {
            numeric_class: getattr(self, numeric_class)
            for numeric_class in ASR_BOUNDARY_NUMERIC_CLASSES
        }


@dataclass(frozen=True)
class ASRBoundaryPresenceCounts:
    """Fixed counters for observable versus unobservable field presence."""

    present: int = 0
    absent: int = 0
    unobservable: int = 0

    def __post_init__(self) -> None:
        _validate_counter_values(self.to_dict())

    def to_dict(self) -> Dict[str, int]:
        return {
            presence: getattr(self, presence)
            for presence in sorted(ASR_BOUNDARY_PRESENCES)
        }


@dataclass(frozen=True)
class ASRWordTimingShapeDiagnostics:
    """Transcript-free raw timing shape for every returned word entry."""

    word_entry_count: int
    no_word_entries: bool
    envelope: Optional[ASRWordTimingEnvelope]
    entry_shape_counts: ASRWordTimingShapeCounts
    start_numeric_class_counts: ASRBoundaryNumericClassCounts
    end_numeric_class_counts: ASRBoundaryNumericClassCounts
    start_presence_counts: ASRBoundaryPresenceCounts
    end_presence_counts: ASRBoundaryPresenceCounts
    anomalies: Tuple[ASRWordTimingEntry, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.word_entry_count, int)
            or isinstance(self.word_entry_count, bool)
            or self.word_entry_count < 0
        ):
            raise ValueError("word_entry_count must be a non-negative integer")
        if not isinstance(self.no_word_entries, bool):
            raise ValueError("no_word_entries must be boolean")
        if self.no_word_entries != (self.word_entry_count == 0):
            raise ValueError("no_word_entries must match word_entry_count")
        if (self.envelope is None) != self.no_word_entries:
            raise ValueError("envelope must exist exactly when words exist")
        if self.envelope is not None and (
            self.envelope.start_word_index != 0
            or self.envelope.end_word_index != self.word_entry_count - 1
        ):
            raise ValueError("envelope must span the first and last word entries")
        count_groups = (
            self.entry_shape_counts.to_dict(),
            self.start_numeric_class_counts.to_dict(),
            self.end_numeric_class_counts.to_dict(),
            self.start_presence_counts.to_dict(),
            self.end_presence_counts.to_dict(),
        )
        if any(
            sum(group.values()) != self.word_entry_count
            for group in count_groups
        ):
            raise ValueError("each timing counter group must cover every word entry")
        anomaly_indices = tuple(item.word_index for item in self.anomalies)
        if (
            anomaly_indices != tuple(sorted(set(anomaly_indices)))
            or any(index >= self.word_entry_count for index in anomaly_indices)
            or any(item.shape == "valid" for item in self.anomalies)
        ):
            raise ValueError("timing anomalies must be unique, ordered, and invalid")
        expected_anomaly_count = (
            self.word_entry_count - self.entry_shape_counts.valid
        )
        if len(self.anomalies) != expected_anomaly_count:
            raise ValueError("anomalies must contain every non-valid word entry")
        anomaly_shape_counts = {
            shape: sum(item.shape == shape for item in self.anomalies)
            for shape in ASR_WORD_TIMING_SHAPES
            if shape != "valid"
        }
        entry_shape_counts = self.entry_shape_counts.to_dict()
        if any(
            anomaly_shape_counts[shape] != entry_shape_counts[shape]
            for shape in anomaly_shape_counts
        ):
            raise ValueError(
                "anomaly shapes must match the non-valid entry counters"
            )
        for counter, boundary_name, attribute, categories in (
            (
                self.start_numeric_class_counts.to_dict(),
                "start",
                "numeric_class",
                ASR_BOUNDARY_NUMERIC_CLASSES,
            ),
            (
                self.end_numeric_class_counts.to_dict(),
                "end",
                "numeric_class",
                ASR_BOUNDARY_NUMERIC_CLASSES,
            ),
            (
                self.start_presence_counts.to_dict(),
                "start",
                "presence",
                ASR_BOUNDARY_PRESENCES,
            ),
            (
                self.end_presence_counts.to_dict(),
                "end",
                "presence",
                ASR_BOUNDARY_PRESENCES,
            ),
        ):
            anomaly_counts = {
                category: sum(
                    getattr(getattr(item, boundary_name), attribute)
                    == category
                    for item in self.anomalies
                )
                for category in categories
            }
            if any(
                anomaly_counts[category] > counter[category]
                for category in categories
            ):
                raise ValueError(
                    "boundary counters cannot undercount anomaly observations"
                )
            forbidden_for_valid = (
                (
                    {
                        "not_available",
                        "unparseable",
                        "nonfinite",
                        "negative",
                    }
                    | ({"zero"} if boundary_name == "end" else set())
                )
                if attribute == "numeric_class"
                else {"absent"}
            )
            if any(
                counter[category] != anomaly_counts[category]
                for category in forbidden_for_valid
            ):
                raise ValueError(
                    "valid-entry residual counters contain impossible "
                    "boundary categories"
                )
        if self.envelope is not None and (
            self.start_numeric_class_counts.to_dict()[
                self.envelope.start.numeric_class
            ]
            == 0
            or self.end_numeric_class_counts.to_dict()[
                self.envelope.end.numeric_class
            ]
            == 0
            or self.start_presence_counts.to_dict()[
                self.envelope.start.presence
            ]
            == 0
            or self.end_presence_counts.to_dict()[
                self.envelope.end.presence
            ]
            == 0
        ):
            raise ValueError(
                "boundary counters must include the envelope observations"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "word_entry_count": self.word_entry_count,
            "no_word_entries": self.no_word_entries,
            "envelope": (
                self.envelope.to_dict() if self.envelope is not None else None
            ),
            "counts": {
                "entry_shape": self.entry_shape_counts.to_dict(),
                "start_numeric_class": (
                    self.start_numeric_class_counts.to_dict()
                ),
                "end_numeric_class": self.end_numeric_class_counts.to_dict(),
                "start_presence": self.start_presence_counts.to_dict(),
                "end_presence": self.end_presence_counts.to_dict(),
            },
            "anomalies": [item.to_dict() for item in self.anomalies],
        }


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


class StagedOutputEventKind(str, Enum):
    """Terminal-safe events exposed to a staged-pipeline consumer."""

    AUDIO = "audio"
    AUDIO_FRAME = "audio_frame"
    PARENT_COMPLETE = "parent_complete"
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
    word_count: int = 0
    first_word_start_ms: Optional[float] = None
    last_word_end_ms: Optional[float] = None
    timing_basis: str = "unavailable"
    word_timing_shape_diagnostics: Optional[
        ASRWordTimingShapeDiagnostics
    ] = None

    def __post_init__(self) -> None:
        _validate_nonnegative_finite(
            "received_monotonic_ms", self.received_monotonic_ms
        )
        _validate_nonnegative_finite("audio_processed_s", self.audio_processed_s)
        _validate_finite("stability", self.stability)
        _validate_finite("confidence", self.confidence)
        _validate_source_range(self.source_start_ms, self.source_end_ms)
        _validate_asr_timing_diagnostics(
            word_count=self.word_count,
            first_word_start_ms=self.first_word_start_ms,
            last_word_end_ms=self.last_word_end_ms,
            timing_basis=self.timing_basis,
            word_timing_shape_diagnostics=self.word_timing_shape_diagnostics,
        )


@dataclass(frozen=True)
class AsrFinal:
    """A finalized ASR fragment with identity independent of segment IDs."""

    final_id: int
    text: str
    received_monotonic_ms: float
    source_start_ms: Optional[float] = None
    source_end_ms: Optional[float] = None
    audio_processed_s: float = 0.0
    word_count: int = 0
    first_word_start_ms: Optional[float] = None
    last_word_end_ms: Optional[float] = None
    timing_basis: str = "unavailable"
    word_timing_shape_diagnostics: Optional[
        ASRWordTimingShapeDiagnostics
    ] = None

    def __post_init__(self) -> None:
        if self.final_id < 0:
            raise ValueError("final_id must be non-negative")
        _validate_nonnegative_finite(
            "received_monotonic_ms", self.received_monotonic_ms
        )
        _validate_nonnegative_finite("audio_processed_s", self.audio_processed_s)
        _validate_source_range(self.source_start_ms, self.source_end_ms)
        _validate_asr_timing_diagnostics(
            word_count=self.word_count,
            first_word_start_ms=self.first_word_start_ms,
            last_word_end_ms=self.last_word_end_ms,
            timing_basis=self.timing_basis,
            word_timing_shape_diagnostics=self.word_timing_shape_diagnostics,
        )

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
            audio_processed_s=transcript.audio_processed_s,
            word_count=transcript.word_count,
            first_word_start_ms=transcript.first_word_start_ms,
            last_word_end_ms=transcript.last_word_end_ms,
            timing_basis=transcript.timing_basis,
            word_timing_shape_diagnostics=(
                transcript.word_timing_shape_diagnostics
            ),
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
class TranslatedSegment:
    """One translated segment with its source identity and NMT timing."""

    segment: TextSegment
    text: str
    language: str
    started_monotonic_ms: float
    completed_monotonic_ms: float
    source_override_applied: bool = False
    retry_count: int = 0
    subsequence_id: int = 0
    subsequence_count: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.segment, TextSegment):
            raise ValueError("segment must be a TextSegment")
        if not self.text.strip():
            raise ValueError("translated text must contain non-whitespace content")
        if not self.language.strip():
            raise ValueError("translated language is required")
        if not isinstance(self.source_override_applied, bool):
            raise ValueError("source_override_applied must be a boolean")
        if (
            not isinstance(self.retry_count, int)
            or isinstance(self.retry_count, bool)
            or self.retry_count not in {0, 1}
        ):
            raise ValueError("retry_count must be zero or one")
        _validate_subsequence_identity(
            self.subsequence_id,
            self.subsequence_count,
        )
        _validate_nonnegative_finite(
            "started_monotonic_ms", self.started_monotonic_ms
        )
        _validate_nonnegative_finite(
            "completed_monotonic_ms", self.completed_monotonic_ms
        )
        if self.completed_monotonic_ms < self.started_monotonic_ms:
            raise ValueError("NMT completion cannot precede its start")

    @property
    def sequence_id(self) -> int:
        return self.segment.sequence_id

    @property
    def parent_sequence_id(self) -> int:
        return self.sequence_id

    @property
    def order_key(self) -> Tuple[int, int, int]:
        return (
            self.parent_sequence_id,
            self.subsequence_id,
            self.subsequence_count,
        )

    @property
    def is_final_subsequence(self) -> bool:
        return self.subsequence_id == self.subsequence_count - 1

    @property
    def processing_duration_ms(self) -> float:
        return self.completed_monotonic_ms - self.started_monotonic_ms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment": self.segment.to_dict(),
            "text": self.text,
            "language": self.language,
            "started_monotonic_ms": self.started_monotonic_ms,
            "completed_monotonic_ms": self.completed_monotonic_ms,
            "processing_duration_ms": self.processing_duration_ms,
            "source_override_applied": self.source_override_applied,
            "retry_count": self.retry_count,
            "parent_sequence_id": self.parent_sequence_id,
            "subsequence_id": self.subsequence_id,
            "subsequence_count": self.subsequence_count,
        }


@dataclass(frozen=True)
class TTSResponseChunkMetric:
    """Privacy-safe timing metadata for one successful TTS response.

    The record deliberately excludes PCM and response metadata because the
    latter can reproduce request text.  It is retained only when the explicit
    response-chunk diagnostic is enabled.
    """

    response_index: int
    audio_bytes: int
    cumulative_audio_bytes: int
    received_monotonic_ms: float
    retry_count: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("response_index", self.response_index),
            ("audio_bytes", self.audio_bytes),
            ("cumulative_audio_bytes", self.cumulative_audio_bytes),
            ("retry_count", self.retry_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
        if self.response_index < 0:
            raise ValueError("response_index must be non-negative")
        if self.audio_bytes <= 0:
            raise ValueError("audio_bytes must be positive")
        if self.cumulative_audio_bytes < self.audio_bytes:
            raise ValueError(
                "cumulative_audio_bytes cannot be smaller than audio_bytes"
            )
        if self.retry_count not in {0, 1}:
            raise ValueError("retry_count must be zero or one")
        _validate_nonnegative_finite(
            "received_monotonic_ms", self.received_monotonic_ms
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SynthesizedSegment:
    """Atomic PCM output for one translated segment plus TTS timing."""

    translation: TranslatedSegment
    audio: bytes
    sample_rate_hz: int
    channels: int
    bytes_per_sample: int
    started_monotonic_ms: float
    first_audio_monotonic_ms: float
    completed_monotonic_ms: float
    retry_count: int = 0
    response_chunks: Tuple[TTSResponseChunkMetric, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.translation, TranslatedSegment):
            raise ValueError("translation must be a TranslatedSegment")
        if not isinstance(self.audio, bytes) or not self.audio:
            raise ValueError("synthesized audio must be non-empty bytes")
        for name, value in (
            ("sample_rate_hz", self.sample_rate_hz),
            ("channels", self.channels),
            ("bytes_per_sample", self.bytes_per_sample),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in (
            ("started_monotonic_ms", self.started_monotonic_ms),
            ("first_audio_monotonic_ms", self.first_audio_monotonic_ms),
            ("completed_monotonic_ms", self.completed_monotonic_ms),
        ):
            _validate_nonnegative_finite(name, value)
        if self.first_audio_monotonic_ms < self.started_monotonic_ms:
            raise ValueError("first TTS audio cannot precede its start")
        if self.completed_monotonic_ms < self.first_audio_monotonic_ms:
            raise ValueError("TTS completion cannot precede first audio")
        if (
            not isinstance(self.retry_count, int)
            or isinstance(self.retry_count, bool)
            or self.retry_count not in {0, 1}
        ):
            raise ValueError("retry_count must be zero or one")
        if not isinstance(self.response_chunks, tuple):
            raise ValueError("response_chunks must be a tuple")
        cumulative_audio_bytes = 0
        previous_received_ms = self.started_monotonic_ms
        for expected_index, chunk in enumerate(self.response_chunks):
            if not isinstance(chunk, TTSResponseChunkMetric):
                raise ValueError(
                    "response_chunks must contain TTSResponseChunkMetric records"
                )
            if chunk.response_index != expected_index:
                raise ValueError(
                    "response chunk indices must be contiguous from zero"
                )
            cumulative_audio_bytes += chunk.audio_bytes
            if chunk.cumulative_audio_bytes != cumulative_audio_bytes:
                raise ValueError(
                    "response chunk cumulative byte counts must reconcile"
                )
            if chunk.received_monotonic_ms < previous_received_ms:
                raise ValueError(
                    "response chunk timestamps must be nondecreasing"
                )
            if chunk.received_monotonic_ms > self.completed_monotonic_ms:
                raise ValueError(
                    "response chunk timestamp cannot follow TTS completion"
                )
            if chunk.retry_count != self.retry_count:
                raise ValueError(
                    "response chunk retry count must match its segment"
                )
            previous_received_ms = chunk.received_monotonic_ms
        if self.response_chunks:
            if self.response_chunks[0].received_monotonic_ms != (
                self.first_audio_monotonic_ms
            ):
                raise ValueError(
                    "first response chunk timestamp must match first TTS audio"
                )
            if cumulative_audio_bytes != len(self.audio):
                raise ValueError(
                    "response chunk byte counts must match synthesized audio"
                )

    @property
    def sequence_id(self) -> int:
        return self.translation.sequence_id

    @property
    def parent_sequence_id(self) -> int:
        return self.translation.parent_sequence_id

    @property
    def subsequence_id(self) -> int:
        return self.translation.subsequence_id

    @property
    def subsequence_count(self) -> int:
        return self.translation.subsequence_count

    @property
    def order_key(self) -> Tuple[int, int, int]:
        return self.translation.order_key

    @property
    def is_final_subsequence(self) -> bool:
        return self.translation.is_final_subsequence

    @property
    def processing_duration_ms(self) -> float:
        return self.completed_monotonic_ms - self.started_monotonic_ms

    @property
    def first_audio_latency_ms(self) -> float:
        return self.first_audio_monotonic_ms - self.started_monotonic_ms

    @property
    def audio_duration_ms(self) -> float:
        bytes_per_second = (
            self.sample_rate_hz * self.channels * self.bytes_per_sample
        )
        return len(self.audio) / bytes_per_second * 1_000

    def to_dict(self, *, include_audio: bool = False) -> Dict[str, Any]:
        payload = {
            "translation": self.translation.to_dict(),
            "audio_bytes": len(self.audio),
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "bytes_per_sample": self.bytes_per_sample,
            "audio_duration_ms": self.audio_duration_ms,
            "started_monotonic_ms": self.started_monotonic_ms,
            "first_audio_monotonic_ms": self.first_audio_monotonic_ms,
            "completed_monotonic_ms": self.completed_monotonic_ms,
            "processing_duration_ms": self.processing_duration_ms,
            "first_audio_latency_ms": self.first_audio_latency_ms,
            "retry_count": self.retry_count,
        }
        if self.response_chunks:
            payload["response_chunks"] = [
                chunk.to_dict() for chunk in self.response_chunks
            ]
        if include_audio:
            payload["audio"] = self.audio
        return payload


@dataclass(frozen=True)
class SynthesizedAudioFrame:
    """One committed schema-v3 PCM frame from an active TTS request."""

    translation: TranslatedSegment
    audio_frame_id: int
    audio: bytes
    sample_rate_hz: int
    channels: int
    bytes_per_sample: int
    received_monotonic_ms: float
    retry_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.translation, TranslatedSegment):
            raise ValueError("translation must be a TranslatedSegment")
        if (
            self.translation.subsequence_id != 0
            or self.translation.subsequence_count != 1
        ):
            raise ValueError(
                "incremental audio frames require an unsplit translation"
            )
        if (
            not isinstance(self.audio_frame_id, int)
            or isinstance(self.audio_frame_id, bool)
            or self.audio_frame_id < 0
        ):
            raise ValueError("audio_frame_id must be a non-negative integer")
        if not isinstance(self.audio, bytes) or not self.audio:
            raise ValueError("synthesized frame audio must be non-empty bytes")
        for name, value in (
            ("sample_rate_hz", self.sample_rate_hz),
            ("channels", self.channels),
            ("bytes_per_sample", self.bytes_per_sample),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        pcm_sample_bytes = self.channels * self.bytes_per_sample
        if len(self.audio) % pcm_sample_bytes:
            raise ValueError("synthesized frame must align to a PCM sample")
        _validate_nonnegative_finite(
            "received_monotonic_ms", self.received_monotonic_ms
        )
        if (
            not isinstance(self.retry_count, int)
            or isinstance(self.retry_count, bool)
            or self.retry_count not in {0, 1}
        ):
            raise ValueError("retry_count must be zero or one")

    @property
    def sequence_id(self) -> int:
        return self.translation.sequence_id

    @property
    def parent_sequence_id(self) -> int:
        return self.translation.parent_sequence_id

    @property
    def frame_key(self) -> Tuple[int, int]:
        return (self.parent_sequence_id, self.audio_frame_id)

    @property
    def order_key(self) -> Tuple[int, int]:
        return self.frame_key

    @property
    def audio_duration_ms(self) -> float:
        bytes_per_second = (
            self.sample_rate_hz * self.channels * self.bytes_per_sample
        )
        return len(self.audio) / bytes_per_second * 1_000

    def to_dict(self, *, include_audio: bool = False) -> Dict[str, Any]:
        payload = {
            "parent_sequence_id": self.parent_sequence_id,
            "audio_frame_id": self.audio_frame_id,
            "audio_bytes": len(self.audio),
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "bytes_per_sample": self.bytes_per_sample,
            "audio_duration_ms": self.audio_duration_ms,
            "received_monotonic_ms": self.received_monotonic_ms,
            "retry_count": self.retry_count,
        }
        if include_audio:
            payload["audio"] = self.audio
        return payload


@dataclass(frozen=True)
class SynthesizedStreamCompletion:
    """Authoritative completion for one schema-v3 incremental TTS request."""

    translation: TranslatedSegment
    audio_frame_count: int
    audio_bytes: int
    sample_rate_hz: int
    channels: int
    bytes_per_sample: int
    started_monotonic_ms: float
    first_audio_monotonic_ms: float
    completed_monotonic_ms: float
    retry_count: int = 0
    response_chunks: Tuple[TTSResponseChunkMetric, ...] = ()
    # Appended after the existing schema-3 fields to preserve positional use.
    atomic_fallback_applied: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.translation, TranslatedSegment):
            raise ValueError("translation must be a TranslatedSegment")
        if (
            self.translation.subsequence_id != 0
            or self.translation.subsequence_count != 1
        ):
            raise ValueError(
                "stream completion requires an unsplit translation"
            )
        for name, value in (
            ("audio_frame_count", self.audio_frame_count),
            ("audio_bytes", self.audio_bytes),
            ("sample_rate_hz", self.sample_rate_hz),
            ("channels", self.channels),
            ("bytes_per_sample", self.bytes_per_sample),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        pcm_sample_bytes = self.channels * self.bytes_per_sample
        if self.audio_bytes % pcm_sample_bytes:
            raise ValueError("stream completion bytes must align to a PCM sample")
        for name, value in (
            ("started_monotonic_ms", self.started_monotonic_ms),
            ("first_audio_monotonic_ms", self.first_audio_monotonic_ms),
            ("completed_monotonic_ms", self.completed_monotonic_ms),
        ):
            _validate_nonnegative_finite(name, value)
        if self.first_audio_monotonic_ms < self.started_monotonic_ms:
            raise ValueError("first TTS audio cannot precede its start")
        if self.completed_monotonic_ms < self.first_audio_monotonic_ms:
            raise ValueError("TTS completion cannot precede first audio")
        if (
            not isinstance(self.retry_count, int)
            or isinstance(self.retry_count, bool)
            or self.retry_count not in {0, 1}
        ):
            raise ValueError("retry_count must be zero or one")
        if not isinstance(self.atomic_fallback_applied, bool):
            raise ValueError("atomic_fallback_applied must be a boolean")
        if not isinstance(self.response_chunks, tuple):
            raise ValueError("response_chunks must be a tuple")
        cumulative_audio_bytes = 0
        previous_received_ms = self.started_monotonic_ms
        for expected_index, chunk in enumerate(self.response_chunks):
            if not isinstance(chunk, TTSResponseChunkMetric):
                raise ValueError(
                    "response_chunks must contain TTSResponseChunkMetric records"
                )
            if chunk.response_index != expected_index:
                raise ValueError(
                    "response chunk indices must be contiguous from zero"
                )
            cumulative_audio_bytes += chunk.audio_bytes
            if chunk.cumulative_audio_bytes != cumulative_audio_bytes:
                raise ValueError(
                    "response chunk cumulative byte counts must reconcile"
                )
            if chunk.received_monotonic_ms < previous_received_ms:
                raise ValueError(
                    "response chunk timestamps must be nondecreasing"
                )
            if chunk.received_monotonic_ms > self.completed_monotonic_ms:
                raise ValueError(
                    "response chunk timestamp cannot follow TTS completion"
                )
            if chunk.retry_count != self.retry_count:
                raise ValueError(
                    "response chunk retry count must match its completion"
                )
            previous_received_ms = chunk.received_monotonic_ms
        if self.response_chunks:
            if self.response_chunks[0].received_monotonic_ms != (
                self.first_audio_monotonic_ms
            ):
                raise ValueError(
                    "first response chunk timestamp must match first TTS audio"
                )
            if cumulative_audio_bytes != self.audio_bytes:
                raise ValueError(
                    "response chunk byte counts must match stream completion"
                )

    @property
    def sequence_id(self) -> int:
        return self.translation.sequence_id

    @property
    def parent_sequence_id(self) -> int:
        return self.translation.parent_sequence_id

    @property
    def subsequence_id(self) -> int:
        return self.translation.subsequence_id

    @property
    def subsequence_count(self) -> int:
        return self.translation.subsequence_count

    @property
    def order_key(self) -> Tuple[int, int, int]:
        return self.translation.order_key

    @property
    def processing_duration_ms(self) -> float:
        return self.completed_monotonic_ms - self.started_monotonic_ms

    @property
    def first_audio_latency_ms(self) -> float:
        return self.first_audio_monotonic_ms - self.started_monotonic_ms

    @property
    def audio_duration_ms(self) -> float:
        bytes_per_second = (
            self.sample_rate_hz * self.channels * self.bytes_per_sample
        )
        return self.audio_bytes / bytes_per_second * 1_000

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "parent_sequence_id": self.parent_sequence_id,
            "audio_frame_count": self.audio_frame_count,
            "audio_bytes": self.audio_bytes,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "bytes_per_sample": self.bytes_per_sample,
            "audio_duration_ms": self.audio_duration_ms,
            "started_monotonic_ms": self.started_monotonic_ms,
            "first_audio_monotonic_ms": self.first_audio_monotonic_ms,
            "completed_monotonic_ms": self.completed_monotonic_ms,
            "processing_duration_ms": self.processing_duration_ms,
            "first_audio_latency_ms": self.first_audio_latency_ms,
            "retry_count": self.retry_count,
            "atomic_fallback_applied": self.atomic_fallback_applied,
        }
        if self.response_chunks:
            payload["response_chunks"] = [
                chunk.to_dict() for chunk in self.response_chunks
            ]
        return payload


@dataclass(frozen=True)
class StagedOutputEvent:
    """One ordered audio result or the single terminal pipeline event."""

    kind: StagedOutputEventKind
    segment: Optional[SynthesizedSegment] = None
    stage: str = ""
    error: str = ""
    # Appended after the legacy fields to preserve positional construction.
    frame: Optional[SynthesizedAudioFrame] = None
    completion: Optional[SynthesizedStreamCompletion] = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StagedOutputEventKind):
            raise ValueError("kind must be a StagedOutputEventKind")
        if self.kind is StagedOutputEventKind.AUDIO:
            if not isinstance(self.segment, SynthesizedSegment):
                raise ValueError("audio output requires a synthesized segment")
            if self.frame is not None or self.completion is not None:
                raise ValueError("audio output cannot carry streaming payloads")
            if self.stage or self.error:
                raise ValueError("audio output cannot carry terminal error fields")
        elif self.kind is StagedOutputEventKind.AUDIO_FRAME:
            if not isinstance(self.frame, SynthesizedAudioFrame):
                raise ValueError("audio-frame output requires a synthesized frame")
            if (
                self.segment is not None
                or self.completion is not None
                or self.stage
                or self.error
            ):
                raise ValueError("audio-frame output requires only a frame")
        elif self.kind is StagedOutputEventKind.PARENT_COMPLETE:
            if not isinstance(self.completion, SynthesizedStreamCompletion):
                raise ValueError(
                    "parent-complete output requires a stream completion"
                )
            if (
                self.segment is not None
                or self.frame is not None
                or self.stage
                or self.error
            ):
                raise ValueError(
                    "parent-complete output requires only a completion"
                )
        elif self.kind is StagedOutputEventKind.ERROR:
            if (
                self.segment is not None
                or self.frame is not None
                or self.completion is not None
                or not self.stage.strip()
                or not self.error.strip()
            ):
                raise ValueError("error output requires only stage and error text")
        elif (
            self.segment is not None
            or self.frame is not None
            or self.completion is not None
            or self.stage
            or self.error
        ):
            raise ValueError("complete output cannot carry a payload")


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
    subsequence_id: Optional[int] = None
    subsequence_count: Optional[int] = None
    asr_final_id: Optional[int] = None
    contributing_final_ids: Tuple[int, ...] = ()
    emission_reason: Optional[EmissionReason] = None
    source_start_ms: Optional[float] = None
    source_end_ms: Optional[float] = None
    queue_depth: Optional[int] = None
    queue_capacity: Optional[int] = None
    queue_residence_ms: float = 0.0
    processing_duration_ms: float = 0.0
    blocked_put_ms: float = 0.0
    text_chars: int = 0
    parent_text_chars: Optional[int] = None
    audio_bytes: int = 0
    audio_duration_ms: float = 0.0
    retry_count: int = 0
    error_code: str = ""
    # Appended after the legacy fields to preserve positional construction.
    audio_frame_id: Optional[int] = None
    audio_frame_count: Optional[int] = None
    atomic_fallback_applied: Optional[bool] = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id is required")
        if not self.stage or not self.event:
            raise ValueError("stage and event are required")
        _validate_nonnegative_finite("monotonic_ms", self.monotonic_ms)
        if self.sequence_id is not None and self.sequence_id < 0:
            raise ValueError("sequence_id must be non-negative")
        if (self.subsequence_id is None) != (self.subsequence_count is None):
            raise ValueError(
                "subsequence_id and subsequence_count must be provided together"
            )
        if self.subsequence_id is not None:
            if self.sequence_id is None:
                raise ValueError(
                    "subsequence identity requires a parent sequence_id"
                )
            _validate_subsequence_identity(
                self.subsequence_id,
                self.subsequence_count,
            )
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
        if self.queue_capacity is not None and self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if (
            self.queue_depth is not None
            and self.queue_capacity is not None
            and self.queue_depth > self.queue_capacity
        ):
            raise ValueError("queue_depth cannot exceed queue_capacity")
        if min(self.text_chars, self.audio_bytes, self.retry_count) < 0:
            raise ValueError("event counters must be non-negative")
        if self.audio_frame_id is not None and (
            not isinstance(self.audio_frame_id, int)
            or isinstance(self.audio_frame_id, bool)
            or self.audio_frame_id < 0
        ):
            raise ValueError("audio_frame_id must be a non-negative integer")
        if self.audio_frame_count is not None and (
            not isinstance(self.audio_frame_count, int)
            or isinstance(self.audio_frame_count, bool)
            or self.audio_frame_count <= 0
        ):
            raise ValueError("audio_frame_count must be a positive integer")
        if (
            self.audio_frame_id is not None
            and self.audio_frame_count is not None
            and self.audio_frame_id >= self.audio_frame_count
        ):
            raise ValueError("audio_frame_id must be less than audio_frame_count")
        if (
            (self.audio_frame_id is not None or self.audio_frame_count is not None)
            and self.sequence_id is None
        ):
            raise ValueError("audio frame identity requires a sequence_id")
        if (
            self.atomic_fallback_applied is not None
            and not isinstance(self.atomic_fallback_applied, bool)
        ):
            raise ValueError("atomic_fallback_applied must be a boolean")
        if self.parent_text_chars is not None and (
            not isinstance(self.parent_text_chars, int)
            or isinstance(self.parent_text_chars, bool)
            or self.parent_text_chars <= 0
        ):
            raise ValueError("parent_text_chars must be a positive integer")
        _validate_nonnegative_finite("audio_duration_ms", self.audio_duration_ms)
        _validate_nonnegative_finite(
            "queue_residence_ms", self.queue_residence_ms
        )
        _validate_nonnegative_finite(
            "processing_duration_ms", self.processing_duration_ms
        )
        _validate_nonnegative_finite("blocked_put_ms", self.blocked_put_ms)
        _validate_source_range(self.source_start_ms, self.source_end_ms)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if (
            self.subsequence_id is not None
            or self.audio_frame_id is not None
            or self.audio_frame_count is not None
        ):
            payload["parent_sequence_id"] = self.sequence_id
        if self.audio_frame_id is None:
            payload.pop("audio_frame_id")
        if self.audio_frame_count is None:
            payload.pop("audio_frame_count")
        if self.atomic_fallback_applied is None:
            payload.pop("atomic_fallback_applied")
        if self.emission_reason is not None:
            payload["emission_reason"] = self.emission_reason.value
        return payload

    @property
    def parent_sequence_id(self) -> Optional[int]:
        return self.sequence_id


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


def _validate_asr_timing_diagnostics(
    *,
    word_count: int,
    first_word_start_ms: Optional[float],
    last_word_end_ms: Optional[float],
    timing_basis: str,
    word_timing_shape_diagnostics: Optional[
        ASRWordTimingShapeDiagnostics
    ],
) -> None:
    if (
        not isinstance(word_count, int)
        or isinstance(word_count, bool)
        or word_count < 0
    ):
        raise ValueError("word_count must be a non-negative integer")
    if timing_basis not in ASR_TIMING_BASES:
        raise ValueError(f"unsupported ASR timing basis: {timing_basis!r}")
    if (
        word_timing_shape_diagnostics is not None
        and word_timing_shape_diagnostics.word_entry_count != word_count
    ):
        raise ValueError(
            "word timing shape diagnostics must match word_count"
        )
    if first_word_start_ms is not None:
        _validate_nonnegative_finite(
            "first_word_start_ms", first_word_start_ms
        )
    if last_word_end_ms is not None:
        _validate_nonnegative_finite("last_word_end_ms", last_word_end_ms)
    complete_offsets = (
        first_word_start_ms is not None and last_word_end_ms is not None
    )
    if (
        complete_offsets
        and last_word_end_ms <= first_word_start_ms
    ):
        raise ValueError(
            "last_word_end_ms must follow first_word_start_ms"
        )
    if timing_basis == ASR_TIMING_BASIS_WORD_OFFSETS:
        if word_count == 0 or not complete_offsets:
            raise ValueError(
                "word_offsets timing requires words and complete offsets"
            )
        if word_timing_shape_diagnostics is not None:
            envelope = word_timing_shape_diagnostics.envelope
            if (
                envelope is None
                or not envelope.usable
                or envelope.start.finite_value_ms
                != first_word_start_ms
                or envelope.end.finite_value_ms != last_word_end_ms
            ):
                raise ValueError(
                    "word_offsets timing must match the raw timing envelope"
                )
        return
    if timing_basis == ASR_TIMING_BASIS_INCOMPLETE_WORD_OFFSETS:
        if word_count == 0 or complete_offsets:
            raise ValueError(
                "incomplete_word_offsets timing requires words and a "
                "missing boundary"
            )
        if (
            word_timing_shape_diagnostics is not None
            and (
                word_timing_shape_diagnostics.envelope is None
                or word_timing_shape_diagnostics.envelope.usable
            )
        ):
            raise ValueError(
                "incomplete_word_offsets requires an unusable raw envelope"
            )
        if word_timing_shape_diagnostics is not None:
            envelope = word_timing_shape_diagnostics.envelope
            if envelope is None:
                raise ValueError(
                    "incomplete_word_offsets requires a raw envelope"
                )
            expected_first = (
                envelope.start.finite_value_ms
                if envelope.start.numeric_class in {"zero", "positive"}
                else None
            )
            expected_last = (
                envelope.end.finite_value_ms
                if envelope.end.numeric_class in {"zero", "positive"}
                else None
            )
            if (
                expected_first is not None
                and expected_last is not None
                and expected_last <= expected_first
            ):
                expected_last = None
            if (
                first_word_start_ms != expected_first
                or last_word_end_ms != expected_last
            ):
                raise ValueError(
                    "incomplete word offsets must match the raw envelope"
                )
        return
    if word_count or first_word_start_ms is not None or last_word_end_ms is not None:
        raise ValueError(
            f"{timing_basis} timing cannot carry word-offset diagnostics"
        )


def _validate_counter_values(values: Dict[str, int]) -> None:
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        for value in values.values()
    ):
        raise ValueError("timing counters must be non-negative integers")


def _validate_word_timing_shape(
    *,
    start: ASRBoundaryObservation,
    end: ASRBoundaryObservation,
    numeric_relation: str,
    shape: str,
) -> None:
    if not isinstance(start, ASRBoundaryObservation) or not isinstance(
        end, ASRBoundaryObservation
    ):
        raise ValueError("word timing boundaries must be typed observations")
    if numeric_relation not in ASR_WORD_TIMING_RELATIONS:
        raise ValueError(
            f"unsupported word timing relation: {numeric_relation!r}"
        )
    if shape not in ASR_WORD_TIMING_SHAPES:
        raise ValueError(f"unsupported word timing shape: {shape!r}")
    expected_relation, expected_shape = _expected_word_timing_shape(
        start,
        end,
    )
    if (
        numeric_relation != expected_relation
        or shape != expected_shape
    ):
        raise ValueError(
            "word timing relation and shape must match the observations"
        )


def _expected_word_timing_shape(
    start: ASRBoundaryObservation,
    end: ASRBoundaryObservation,
) -> Tuple[str, str]:
    numeric_classes = {start.numeric_class, end.numeric_class}
    if "not_available" in numeric_classes:
        return "not_comparable", "absent_boundary"
    if "unparseable" in numeric_classes:
        return "not_comparable", "unparseable_boundary"
    if "nonfinite" in numeric_classes:
        return "not_comparable", "nonfinite_boundary"
    start_value = start.finite_value_ms
    end_value = end.finite_value_ms
    if start_value is None or end_value is None:
        return "not_comparable", "unparseable_boundary"
    if end_value > start_value:
        relation = "end_after_start"
    elif end_value == start_value:
        relation = "equal"
    else:
        relation = "end_before_start"
    if "negative" in numeric_classes:
        return relation, "negative_boundary"
    if relation == "equal":
        return relation, "zero_length"
    if relation == "end_before_start":
        return relation, "reversed"
    return relation, "valid"


def _validate_subsequence_identity(
    subsequence_id: int,
    subsequence_count: int,
) -> None:
    for name, value in (
        ("subsequence_id", subsequence_id),
        ("subsequence_count", subsequence_count),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
    if subsequence_count <= 0:
        raise ValueError("subsequence_count must be positive")
    if subsequence_id < 0 or subsequence_id >= subsequence_count:
        raise ValueError(
            "subsequence_id must be within the subsequence_count range"
        )


def _validate_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _validate_nonnegative_finite(name: str, value: float) -> None:
    _validate_finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
