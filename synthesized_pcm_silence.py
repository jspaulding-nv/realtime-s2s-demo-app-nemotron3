"""Privacy-safe streaming low-energy telemetry for synthesized PCM.

The diagnostic consumes protocol-v1 translated-audio metadata plus signed
16-bit little-endian mono PCM.  PCM is inspected synchronously and discarded:
the object retains only scalar window accumulators and numeric result rows.
Twenty-millisecond RMS windows continue across transport-frame boundaries but
never across parent boundaries.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from audio_metadata_protocol import (
    AudioMetadataProtocolError,
    validate_audio_frame,
    validate_audio_parent_complete,
)


SYNTHESIZED_PCM_SILENCE_SCHEMA_VERSION = 1
SYNTHESIZED_PCM_SILENCE_OBSERVATION_TYPE = "synthesized_pcm_silence"

SAMPLE_RATE_HZ = 16_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
DBFS_REFERENCE_AMPLITUDE = 32_768
WINDOW_MS = 20
WINDOW_SAMPLES = SAMPLE_RATE_HZ * WINDOW_MS // 1_000
THRESHOLDS_DBFS = (-60.0, -50.0, -40.0)
PRIMARY_THRESHOLD_DBFS = -50.0

_METHOD = {
    "pcm_encoding": "signed_16_bit_linear_pcm",
    "byte_order": "little_endian",
    "sample_rate_hz": SAMPLE_RATE_HZ,
    "channels": CHANNELS,
    "bytes_per_sample": BYTES_PER_SAMPLE,
    "dbfs_reference_amplitude": DBFS_REFERENCE_AMPLITUDE,
    "window_ms": WINDOW_MS,
    "window_samples": WINDOW_SAMPLES,
    "thresholds_dbfs": list(THRESHOLDS_DBFS),
    "primary_threshold_dbfs": PRIMARY_THRESHOLD_DBFS,
    "low_energy_comparison": "window_rms_dbfs_lte_threshold",
    "transport_frame_policy": (
        "windows_cross_frame_boundaries_within_parent"
    ),
    "partial_window_policy": "classify_at_parent_completion",
    "all_low_energy_policy": "canonicalize_as_leading_only",
}

_PRIVACY = {
    "contains_audio": False,
    "contains_transcript_text": False,
    "contains_translation_text": False,
    "contains_input_paths_or_filenames": False,
    "contains_endpoints": False,
    "contains_session_ids": False,
    "contains_source_timing": False,
    "numeric_telemetry_only": True,
}

_ROOT_FIELDS = {
    "schema_version",
    "observation_type",
    "method",
    "totals",
    "threshold_totals",
    "parent_threshold_rows",
    "privacy",
}
_TOTAL_FIELD_ORDER = (
    "stream_generation",
    "parent_count",
    "frame_count",
    "audio_bytes",
    "sample_count",
    "duration_ms",
    "full_window_count",
    "partial_window_count",
)
_TOTAL_FIELDS = set(_TOTAL_FIELD_ORDER)
_PARTITION_COUNT_FIELD_ORDER = (
    "active_sample_count",
    "low_energy_sample_count",
    "leading_low_energy_sample_count",
    "internal_low_energy_sample_count",
    "trailing_low_energy_sample_count",
    "internal_low_energy_run_count",
    "longest_internal_low_energy_run_samples",
)
_PARTITION_COUNT_FIELDS = set(_PARTITION_COUNT_FIELD_ORDER)
_PARTITION_MS_FIELD_ORDER = (
    "active_ms",
    "low_energy_ms",
    "leading_low_energy_ms",
    "internal_low_energy_ms",
    "trailing_low_energy_ms",
    "longest_internal_low_energy_run_ms",
)
_PARTITION_MS_FIELDS = set(_PARTITION_MS_FIELD_ORDER)
_THRESHOLD_TOTAL_FIELDS = (
    {
        "threshold_dbfs",
        "parent_count",
        "all_low_energy_parent_count",
    }
    | _PARTITION_COUNT_FIELDS
    | _PARTITION_MS_FIELDS
)
_PARENT_THRESHOLD_FIELDS = (
    {
        "stream_generation",
        "parent_sequence_id",
        "frame_count",
        "audio_bytes",
        "sample_count",
        "duration_ms",
        "full_window_count",
        "partial_window_count",
        "threshold_dbfs",
        "all_low_energy",
    }
    | _PARTITION_COUNT_FIELDS
    | _PARTITION_MS_FIELDS
)


class SynthesizedPcmSilenceError(ValueError):
    """Raised when PCM or a silence observation violates the strict contract."""


def _samples_to_ms(sample_count: int) -> float:
    return round(sample_count * 1_000.0 / SAMPLE_RATE_HZ, 6)


def _window_is_low_energy(
    sum_squares: int,
    sample_count: int,
    threshold_dbfs: float,
) -> bool:
    """Classify one RMS window; equality is low energy."""

    if sample_count <= 0:
        raise SynthesizedPcmSilenceError(
            "a low-energy window must contain at least one sample"
        )
    if sum_squares < 0:
        raise SynthesizedPcmSilenceError(
            "a low-energy window sum of squares cannot be negative"
        )
    if sum_squares == 0:
        return True
    mean_square = sum_squares / sample_count
    dbfs = 10.0 * math.log10(
        mean_square / (DBFS_REFERENCE_AMPLITUDE**2)
    )
    return dbfs <= threshold_dbfs


@dataclass
class _ThresholdState:
    threshold_dbfs: float
    active_sample_count: int = 0
    low_energy_sample_count: int = 0
    leading_low_energy_sample_count: int = 0
    internal_low_energy_sample_count: int = 0
    pending_low_energy_sample_count: int = 0
    internal_low_energy_run_count: int = 0
    longest_internal_low_energy_run_samples: int = 0
    active_seen: bool = False

    def accept_window(
        self,
        *,
        sample_count: int,
        sum_squares: int,
    ) -> None:
        is_low_energy = _window_is_low_energy(
            sum_squares,
            sample_count,
            self.threshold_dbfs,
        )
        if is_low_energy:
            self.low_energy_sample_count += sample_count
            if self.active_seen:
                self.pending_low_energy_sample_count += sample_count
            else:
                self.leading_low_energy_sample_count += sample_count
            return

        self.active_sample_count += sample_count
        if self.active_seen and self.pending_low_energy_sample_count:
            run_samples = self.pending_low_energy_sample_count
            self.internal_low_energy_sample_count += run_samples
            self.internal_low_energy_run_count += 1
            self.longest_internal_low_energy_run_samples = max(
                self.longest_internal_low_energy_run_samples,
                run_samples,
            )
        self.pending_low_energy_sample_count = 0
        self.active_seen = True

    def result(self) -> dict[str, int | float | bool]:
        trailing_samples = (
            self.pending_low_energy_sample_count if self.active_seen else 0
        )
        all_low_energy = not self.active_seen
        return {
            "threshold_dbfs": self.threshold_dbfs,
            "all_low_energy": all_low_energy,
            "active_sample_count": self.active_sample_count,
            "low_energy_sample_count": self.low_energy_sample_count,
            "leading_low_energy_sample_count": (
                self.leading_low_energy_sample_count
            ),
            "internal_low_energy_sample_count": (
                self.internal_low_energy_sample_count
            ),
            "trailing_low_energy_sample_count": trailing_samples,
            "internal_low_energy_run_count": (
                self.internal_low_energy_run_count
            ),
            "longest_internal_low_energy_run_samples": (
                self.longest_internal_low_energy_run_samples
            ),
            "active_ms": _samples_to_ms(self.active_sample_count),
            "low_energy_ms": _samples_to_ms(
                self.low_energy_sample_count
            ),
            "leading_low_energy_ms": _samples_to_ms(
                self.leading_low_energy_sample_count
            ),
            "internal_low_energy_ms": _samples_to_ms(
                self.internal_low_energy_sample_count
            ),
            "trailing_low_energy_ms": _samples_to_ms(trailing_samples),
            "longest_internal_low_energy_run_ms": _samples_to_ms(
                self.longest_internal_low_energy_run_samples
            ),
        }


@dataclass
class _ParentState:
    stream_generation: int
    parent_sequence_id: int
    source_start_ms: float | None
    source_end_ms: float | None
    frame_count: int = 0
    audio_bytes: int = 0
    sample_count: int = 0
    full_window_count: int = 0
    partial_window_count: int = 0
    thresholds: list[_ThresholdState] = field(
        default_factory=lambda: [
            _ThresholdState(threshold) for threshold in THRESHOLDS_DBFS
        ]
    )


class StreamingPcmSilenceDiagnostic:
    """Accumulate numeric low-energy evidence without retaining PCM."""

    def __init__(self) -> None:
        self._stream_generation: int | None = None
        self._next_parent_sequence_id = 0
        self._active_parent: _ParentState | None = None
        self._window_sample_count = 0
        self._window_sum_squares = 0
        self._parent_threshold_rows: list[dict[str, Any]] = []
        self._total_frame_count = 0
        self._total_audio_bytes = 0
        self._total_sample_count = 0
        self._total_full_window_count = 0
        self._total_partial_window_count = 0
        self._finalized = False

    def _require_open(self) -> None:
        if self._finalized:
            raise SynthesizedPcmSilenceError(
                "the synthesized PCM silence diagnostic is already finalized"
            )

    @staticmethod
    def _normalize_frame(metadata: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(metadata, Mapping):
            raise SynthesizedPcmSilenceError(
                "audio_frame metadata must be a mapping"
            )
        try:
            return validate_audio_frame(dict(metadata))
        except AudioMetadataProtocolError as exc:
            raise SynthesizedPcmSilenceError(str(exc)) from exc

    @staticmethod
    def _normalize_completion(
        completion: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(completion, Mapping):
            raise SynthesizedPcmSilenceError(
                "audio_parent_complete metadata must be a mapping"
            )
        try:
            return validate_audio_parent_complete(dict(completion))
        except AudioMetadataProtocolError as exc:
            raise SynthesizedPcmSilenceError(str(exc)) from exc

    def _validate_stream_and_frame_order(
        self,
        frame: dict[str, Any],
    ) -> None:
        if (
            frame["sampleRateHz"] != SAMPLE_RATE_HZ
            or frame["channels"] != CHANNELS
            or frame["bytesPerSample"] != BYTES_PER_SAMPLE
        ):
            raise SynthesizedPcmSilenceError(
                "PCM must be 16000 Hz mono signed 16-bit little-endian"
            )
        generation = frame["streamGeneration"]
        if self._stream_generation is None:
            pass
        elif generation != self._stream_generation:
            raise SynthesizedPcmSilenceError(
                "streamGeneration changed within one observation"
            )

        parent = self._active_parent
        if parent is None:
            if (
                frame["parentSequenceId"]
                != self._next_parent_sequence_id
            ):
                raise SynthesizedPcmSilenceError(
                    "parentSequenceId must be contiguous and ordered from zero"
                )
            if frame["audioFrameId"] != 0:
                raise SynthesizedPcmSilenceError(
                    "the first audioFrameId in a parent must be zero"
                )
            return

        if frame["parentSequenceId"] != parent.parent_sequence_id:
            raise SynthesizedPcmSilenceError(
                "a new parent began before the active parent completed"
            )
        if frame["audioFrameId"] != parent.frame_count:
            raise SynthesizedPcmSilenceError(
                "audioFrameId must be contiguous within its parent"
            )
        if (
            frame["sourceStartMs"] != parent.source_start_ms
            or frame["sourceEndMs"] != parent.source_end_ms
        ):
            raise SynthesizedPcmSilenceError(
                "source range must remain stable within a parent"
            )

    def _classify_window(self, sample_count: int) -> None:
        parent = self._active_parent
        if parent is None:
            raise SynthesizedPcmSilenceError(
                "cannot classify PCM without an active parent"
            )
        for threshold in parent.thresholds:
            threshold.accept_window(
                sample_count=sample_count,
                sum_squares=self._window_sum_squares,
            )
        if sample_count == WINDOW_SAMPLES:
            parent.full_window_count += 1
            self._total_full_window_count += 1
        else:
            parent.partial_window_count += 1
            self._total_partial_window_count += 1
        self._window_sample_count = 0
        self._window_sum_squares = 0

    def _consume_samples(self, pcm: bytes) -> int:
        # Both arrays below are local, transient views/copies.  Neither is
        # retained on the diagnostic object.
        samples = np.frombuffer(pcm, dtype="<i2")
        position = 0
        while position < samples.size:
            available = WINDOW_SAMPLES - self._window_sample_count
            take = min(available, int(samples.size) - position)
            widened = samples[position : position + take].astype(
                np.int64,
                copy=False,
            )
            self._window_sum_squares += int(np.dot(widened, widened))
            self._window_sample_count += take
            position += take
            if self._window_sample_count == WINDOW_SAMPLES:
                self._classify_window(WINDOW_SAMPLES)
        return int(samples.size)

    def accept_frame(
        self,
        metadata: Mapping[str, Any],
        pcm: bytes,
    ) -> None:
        """Consume one fully validated metadata/PCM pair."""

        self._require_open()
        frame = self._normalize_frame(metadata)
        if not isinstance(pcm, bytes):
            raise SynthesizedPcmSilenceError("PCM payload must be bytes")
        if len(pcm) != frame["audioBytes"]:
            raise SynthesizedPcmSilenceError(
                "PCM byte count does not match audio_frame"
            )
        if len(pcm) % BYTES_PER_SAMPLE:
            raise SynthesizedPcmSilenceError(
                "PCM payload must align to signed 16-bit samples"
            )
        self._validate_stream_and_frame_order(frame)

        if self._active_parent is None:
            if self._window_sample_count or self._window_sum_squares:
                raise SynthesizedPcmSilenceError(
                    "a parent began with an unflushed PCM window"
                )
            self._active_parent = _ParentState(
                stream_generation=frame["streamGeneration"],
                parent_sequence_id=frame["parentSequenceId"],
                source_start_ms=frame["sourceStartMs"],
                source_end_ms=frame["sourceEndMs"],
            )
            if self._stream_generation is None:
                self._stream_generation = frame["streamGeneration"]

        sample_count = self._consume_samples(pcm)
        parent = self._active_parent
        if parent is None:  # pragma: no cover - guarded above
            raise SynthesizedPcmSilenceError("active parent was lost")
        parent.frame_count += 1
        parent.audio_bytes += len(pcm)
        parent.sample_count += sample_count
        self._total_frame_count += 1
        self._total_audio_bytes += len(pcm)
        self._total_sample_count += sample_count

    def complete_parent(self, completion: Mapping[str, Any]) -> None:
        """Reconcile and close the active parent, flushing one partial window."""

        self._require_open()
        normalized = self._normalize_completion(completion)
        parent = self._active_parent
        if parent is None:
            raise SynthesizedPcmSilenceError(
                "audio_parent_complete has no active parent"
            )
        if normalized["streamGeneration"] != self._stream_generation:
            raise SynthesizedPcmSilenceError(
                "streamGeneration changed within one observation"
            )
        if normalized["parentSequenceId"] != parent.parent_sequence_id:
            raise SynthesizedPcmSilenceError(
                "audio_parent_complete references the wrong parent"
            )
        if normalized["audioFrameCount"] != parent.frame_count:
            raise SynthesizedPcmSilenceError(
                "audio_parent_complete frame count does not reconcile"
            )
        if normalized["audioBytes"] != parent.audio_bytes:
            raise SynthesizedPcmSilenceError(
                "audio_parent_complete byte count does not reconcile"
            )
        if (
            normalized["sourceStartMs"] != parent.source_start_ms
            or normalized["sourceEndMs"] != parent.source_end_ms
        ):
            raise SynthesizedPcmSilenceError(
                "audio_parent_complete source range does not reconcile"
            )

        if self._window_sample_count:
            self._classify_window(self._window_sample_count)

        base = {
            "stream_generation": parent.stream_generation,
            "parent_sequence_id": parent.parent_sequence_id,
            "frame_count": parent.frame_count,
            "audio_bytes": parent.audio_bytes,
            "sample_count": parent.sample_count,
            "duration_ms": _samples_to_ms(parent.sample_count),
            "full_window_count": parent.full_window_count,
            "partial_window_count": parent.partial_window_count,
        }
        for threshold in parent.thresholds:
            self._parent_threshold_rows.append(
                {**base, **threshold.result()}
            )

        self._next_parent_sequence_id += 1
        self._active_parent = None

    def _threshold_totals(self) -> list[dict[str, Any]]:
        totals: list[dict[str, Any]] = []
        for threshold in THRESHOLDS_DBFS:
            rows = [
                row
                for row in self._parent_threshold_rows
                if row["threshold_dbfs"] == threshold
            ]
            totals.append(
                {
                    "threshold_dbfs": threshold,
                    "parent_count": len(rows),
                    "all_low_energy_parent_count": sum(
                        bool(row["all_low_energy"]) for row in rows
                    ),
                    "active_sample_count": sum(
                        row["active_sample_count"] for row in rows
                    ),
                    "low_energy_sample_count": sum(
                        row["low_energy_sample_count"] for row in rows
                    ),
                    "leading_low_energy_sample_count": sum(
                        row["leading_low_energy_sample_count"]
                        for row in rows
                    ),
                    "internal_low_energy_sample_count": sum(
                        row["internal_low_energy_sample_count"]
                        for row in rows
                    ),
                    "trailing_low_energy_sample_count": sum(
                        row["trailing_low_energy_sample_count"]
                        for row in rows
                    ),
                    "internal_low_energy_run_count": sum(
                        row["internal_low_energy_run_count"]
                        for row in rows
                    ),
                    "longest_internal_low_energy_run_samples": max(
                        (
                            row[
                                "longest_internal_low_energy_run_samples"
                            ]
                            for row in rows
                        ),
                        default=0,
                    ),
                    "active_ms": _samples_to_ms(
                        sum(row["active_sample_count"] for row in rows)
                    ),
                    "low_energy_ms": _samples_to_ms(
                        sum(row["low_energy_sample_count"] for row in rows)
                    ),
                    "leading_low_energy_ms": _samples_to_ms(
                        sum(
                            row["leading_low_energy_sample_count"]
                            for row in rows
                        )
                    ),
                    "internal_low_energy_ms": _samples_to_ms(
                        sum(
                            row["internal_low_energy_sample_count"]
                            for row in rows
                        )
                    ),
                    "trailing_low_energy_ms": _samples_to_ms(
                        sum(
                            row["trailing_low_energy_sample_count"]
                            for row in rows
                        )
                    ),
                    "longest_internal_low_energy_run_ms": _samples_to_ms(
                        max(
                            (
                                row[
                                    "longest_internal_low_energy_run_samples"
                                ]
                                for row in rows
                            ),
                            default=0,
                        )
                    ),
                }
            )
        return totals

    def finalize(self) -> dict[str, Any]:
        """Return a validated schema-v1 observation and seal the diagnostic."""

        self._require_open()
        if self._active_parent is not None or self._window_sample_count:
            raise SynthesizedPcmSilenceError(
                "cannot finalize before the active parent completes"
            )
        parent_count = self._next_parent_sequence_id
        if parent_count == 0 or self._stream_generation is None:
            raise SynthesizedPcmSilenceError(
                "cannot finalize an empty synthesized PCM observation"
            )
        observation = {
            "schema_version": SYNTHESIZED_PCM_SILENCE_SCHEMA_VERSION,
            "observation_type": SYNTHESIZED_PCM_SILENCE_OBSERVATION_TYPE,
            "method": copy.deepcopy(_METHOD),
            "totals": {
                "stream_generation": self._stream_generation,
                "parent_count": parent_count,
                "frame_count": self._total_frame_count,
                "audio_bytes": self._total_audio_bytes,
                "sample_count": self._total_sample_count,
                "duration_ms": _samples_to_ms(self._total_sample_count),
                "full_window_count": self._total_full_window_count,
                "partial_window_count": self._total_partial_window_count,
            },
            "threshold_totals": self._threshold_totals(),
            "parent_threshold_rows": copy.deepcopy(
                self._parent_threshold_rows
            ),
            "privacy": copy.deepcopy(_PRIVACY),
        }
        validated = validate_synthesized_pcm_silence_observation(observation)
        self._finalized = True
        return validated


def _require_exact_fields(
    value: Any,
    fields: set[str],
    path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SynthesizedPcmSilenceError(f"{path} must be an object")
    observed = set(value)
    if observed != fields:
        missing = sorted(fields - observed)
        unexpected = sorted(observed - fields)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise SynthesizedPcmSilenceError(
            f"{path} fields are invalid ({'; '.join(details)})"
        )
    return value


def _require_int(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
) -> int:
    if type(value) is not int or value < minimum:
        raise SynthesizedPcmSilenceError(
            f"{path} must be an integer at least {minimum}"
        )
    return value


def _require_number(value: Any, path: str) -> float:
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
    ):
        raise SynthesizedPcmSilenceError(
            f"{path} must be a finite number"
        )
    return float(value)


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise SynthesizedPcmSilenceError(f"{path} must be a boolean")
    return value


def _require_expected_ms(value: Any, samples: int, path: str) -> float:
    observed = _require_number(value, path)
    expected = _samples_to_ms(samples)
    if not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1e-6):
        raise SynthesizedPcmSilenceError(
            f"{path} does not reconcile with its sample count"
        )
    return expected


def _validate_partition(
    row: dict[str, Any],
    *,
    path: str,
    sample_count: int,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field_name in _PARTITION_COUNT_FIELD_ORDER:
        normalized[field_name] = _require_int(
            row[field_name],
            f"{path}.{field_name}",
        )
    active = normalized["active_sample_count"]
    low = normalized["low_energy_sample_count"]
    leading = normalized["leading_low_energy_sample_count"]
    internal = normalized["internal_low_energy_sample_count"]
    trailing = normalized["trailing_low_energy_sample_count"]
    run_count = normalized["internal_low_energy_run_count"]
    longest = normalized["longest_internal_low_energy_run_samples"]
    if active + low != sample_count:
        raise SynthesizedPcmSilenceError(
            f"{path} active and low-energy samples do not partition total"
        )
    if leading + internal + trailing != low:
        raise SynthesizedPcmSilenceError(
            f"{path} low-energy regions do not partition low-energy samples"
        )
    if longest > internal:
        raise SynthesizedPcmSilenceError(
            f"{path} longest internal run exceeds internal low-energy samples"
        )
    if (run_count == 0) != (internal == 0):
        raise SynthesizedPcmSilenceError(
            f"{path} internal run count does not reconcile"
        )
    if run_count == 0 and longest != 0:
        raise SynthesizedPcmSilenceError(
            f"{path} has a longest internal run without an internal run"
        )
    if run_count > 0 and longest == 0:
        raise SynthesizedPcmSilenceError(
            f"{path} internal run must have positive length"
        )

    ms_pairs = {
        "active_ms": active,
        "low_energy_ms": low,
        "leading_low_energy_ms": leading,
        "internal_low_energy_ms": internal,
        "trailing_low_energy_ms": trailing,
        "longest_internal_low_energy_run_ms": longest,
    }
    for field_name, count in ms_pairs.items():
        normalized[field_name] = _require_expected_ms(
            row[field_name],
            count,
            f"{path}.{field_name}",
        )
    return normalized


def validate_synthesized_pcm_silence_observation(
    value: Any,
) -> dict[str, Any]:
    """Validate schema v1, returning a normalized deep copy or raising."""

    root = _require_exact_fields(value, _ROOT_FIELDS, "observation")
    if (
        _require_int(root["schema_version"], "observation.schema_version", minimum=1)
        != SYNTHESIZED_PCM_SILENCE_SCHEMA_VERSION
    ):
        raise SynthesizedPcmSilenceError(
            "observation.schema_version must equal 1"
        )
    if (
        type(root["observation_type"]) is not str
        or root["observation_type"]
        != SYNTHESIZED_PCM_SILENCE_OBSERVATION_TYPE
    ):
        raise SynthesizedPcmSilenceError(
            "observation.observation_type is invalid"
        )

    method = _require_exact_fields(
        root["method"],
        set(_METHOD),
        "observation.method",
    )
    for key, expected in _METHOD.items():
        observed = method[key]
        if key == "thresholds_dbfs":
            if (
                not isinstance(observed, list)
                or len(observed) != len(THRESHOLDS_DBFS)
                or any(
                    _require_number(item, f"observation.method.{key}[{index}]")
                    != expected[index]
                    for index, item in enumerate(observed)
                )
            ):
                raise SynthesizedPcmSilenceError(
                    "observation.method.thresholds_dbfs is invalid"
                )
        elif type(expected) is int:
            if _require_int(
                observed,
                f"observation.method.{key}",
            ) != expected:
                raise SynthesizedPcmSilenceError(
                    f"observation.method.{key} is invalid"
                )
        elif type(expected) is float:
            if (
                _require_number(observed, f"observation.method.{key}")
                != expected
            ):
                raise SynthesizedPcmSilenceError(
                    f"observation.method.{key} is invalid"
                )
        elif type(observed) is not str or observed != expected:
            raise SynthesizedPcmSilenceError(
                f"observation.method.{key} is invalid"
            )

    privacy = _require_exact_fields(
        root["privacy"],
        set(_PRIVACY),
        "observation.privacy",
    )
    for key, expected in _PRIVACY.items():
        if _require_bool(
            privacy[key],
            f"observation.privacy.{key}",
        ) is not expected:
            raise SynthesizedPcmSilenceError(
                f"observation.privacy.{key} is invalid"
            )

    totals = _require_exact_fields(
        root["totals"],
        _TOTAL_FIELDS,
        "observation.totals",
    )
    normalized_totals: dict[str, int | float] = {}
    for field_name in _TOTAL_FIELD_ORDER:
        if field_name == "duration_ms":
            normalized_totals[field_name] = _require_expected_ms(
                totals[field_name],
                int(normalized_totals["sample_count"]),
                "observation.totals.duration_ms",
            )
        else:
            normalized_totals[field_name] = _require_int(
                totals[field_name],
                f"observation.totals.{field_name}",
                minimum=1 if field_name in {
                    "stream_generation",
                    "parent_count",
                    "frame_count",
                    "audio_bytes",
                    "sample_count",
                } else 0,
            )
    if (
        normalized_totals["audio_bytes"]
        != normalized_totals["sample_count"] * BYTES_PER_SAMPLE
    ):
        raise SynthesizedPcmSilenceError(
            "observation.totals audio bytes do not reconcile"
        )

    parent_rows = root["parent_threshold_rows"]
    if not isinstance(parent_rows, list):
        raise SynthesizedPcmSilenceError(
            "observation.parent_threshold_rows must be an array"
        )
    expected_row_count = (
        normalized_totals["parent_count"] * len(THRESHOLDS_DBFS)
    )
    if len(parent_rows) != expected_row_count:
        raise SynthesizedPcmSilenceError(
            "observation.parent_threshold_rows count does not reconcile"
        )

    normalized_parent_rows: list[dict[str, Any]] = []
    parent_bases: list[dict[str, int | float]] = []
    previous_low_by_parent: dict[int, int] = {}
    for index, raw_row in enumerate(parent_rows):
        path = f"observation.parent_threshold_rows[{index}]"
        row = _require_exact_fields(
            raw_row,
            _PARENT_THRESHOLD_FIELDS,
            path,
        )
        parent_id = index // len(THRESHOLDS_DBFS)
        threshold_index = index % len(THRESHOLDS_DBFS)
        threshold = _require_number(
            row["threshold_dbfs"],
            f"{path}.threshold_dbfs",
        )
        if threshold != THRESHOLDS_DBFS[threshold_index]:
            raise SynthesizedPcmSilenceError(
                f"{path}.threshold_dbfs is out of canonical order"
            )
        if (
            _require_int(
                row["parent_sequence_id"],
                f"{path}.parent_sequence_id",
            )
            != parent_id
        ):
            raise SynthesizedPcmSilenceError(
                f"{path}.parent_sequence_id is out of canonical order"
            )
        base: dict[str, int | float] = {
            "stream_generation": _require_int(
                row["stream_generation"],
                f"{path}.stream_generation",
                minimum=1,
            ),
            "parent_sequence_id": parent_id,
            "frame_count": _require_int(
                row["frame_count"], f"{path}.frame_count", minimum=1
            ),
            "audio_bytes": _require_int(
                row["audio_bytes"], f"{path}.audio_bytes", minimum=1
            ),
            "sample_count": _require_int(
                row["sample_count"], f"{path}.sample_count", minimum=1
            ),
            "full_window_count": _require_int(
                row["full_window_count"], f"{path}.full_window_count"
            ),
            "partial_window_count": _require_int(
                row["partial_window_count"], f"{path}.partial_window_count"
            ),
        }
        base["duration_ms"] = _require_expected_ms(
            row["duration_ms"],
            int(base["sample_count"]),
            f"{path}.duration_ms",
        )
        if base["stream_generation"] != normalized_totals["stream_generation"]:
            raise SynthesizedPcmSilenceError(
                f"{path}.stream_generation does not match totals"
            )
        if base["audio_bytes"] != base["sample_count"] * BYTES_PER_SAMPLE:
            raise SynthesizedPcmSilenceError(
                f"{path}.audio_bytes does not reconcile"
            )
        full_windows, partial_samples = divmod(
            int(base["sample_count"]),
            WINDOW_SAMPLES,
        )
        if (
            base["full_window_count"] != full_windows
            or base["partial_window_count"] != int(partial_samples > 0)
        ):
            raise SynthesizedPcmSilenceError(
                f"{path} window counts do not reconcile"
            )
        if threshold_index == 0:
            parent_bases.append(base)
        elif base != parent_bases[parent_id]:
            raise SynthesizedPcmSilenceError(
                f"{path} parent fields changed across thresholds"
            )

        partition = _validate_partition(
            row,
            path=path,
            sample_count=int(base["sample_count"]),
        )
        all_low_energy = _require_bool(
            row["all_low_energy"],
            f"{path}.all_low_energy",
        )
        if all_low_energy != (partition["active_sample_count"] == 0):
            raise SynthesizedPcmSilenceError(
                f"{path}.all_low_energy does not reconcile"
            )
        if all_low_energy and (
            partition["leading_low_energy_sample_count"]
            != base["sample_count"]
            or partition["internal_low_energy_sample_count"] != 0
            or partition["trailing_low_energy_sample_count"] != 0
        ):
            raise SynthesizedPcmSilenceError(
                f"{path} violates the all-low leading-only policy"
            )
        prior_low = previous_low_by_parent.get(parent_id)
        if prior_low is not None and partition["low_energy_sample_count"] < prior_low:
            raise SynthesizedPcmSilenceError(
                f"{path} low-energy samples are not threshold-monotonic"
            )
        previous_low_by_parent[parent_id] = partition[
            "low_energy_sample_count"
        ]
        normalized_parent_rows.append(
            {
                **base,
                "threshold_dbfs": threshold,
                "all_low_energy": all_low_energy,
                **partition,
            }
        )

    for field_name in (
        "frame_count",
        "audio_bytes",
        "sample_count",
        "full_window_count",
        "partial_window_count",
    ):
        if (
            sum(int(base[field_name]) for base in parent_bases)
            != normalized_totals[field_name]
        ):
            raise SynthesizedPcmSilenceError(
                f"observation.totals.{field_name} does not reconcile"
            )

    threshold_totals = root["threshold_totals"]
    if (
        not isinstance(threshold_totals, list)
        or len(threshold_totals) != len(THRESHOLDS_DBFS)
    ):
        raise SynthesizedPcmSilenceError(
            "observation.threshold_totals must contain three rows"
        )
    normalized_threshold_totals: list[dict[str, Any]] = []
    for threshold_index, raw_total in enumerate(threshold_totals):
        path = f"observation.threshold_totals[{threshold_index}]"
        row = _require_exact_fields(
            raw_total,
            _THRESHOLD_TOTAL_FIELDS,
            path,
        )
        threshold = _require_number(row["threshold_dbfs"], f"{path}.threshold_dbfs")
        if threshold != THRESHOLDS_DBFS[threshold_index]:
            raise SynthesizedPcmSilenceError(
                f"{path}.threshold_dbfs is out of canonical order"
            )
        matching = normalized_parent_rows[
            threshold_index :: len(THRESHOLDS_DBFS)
        ]
        parent_count = _require_int(
            row["parent_count"], f"{path}.parent_count", minimum=1
        )
        if parent_count != normalized_totals["parent_count"]:
            raise SynthesizedPcmSilenceError(
                f"{path}.parent_count does not reconcile"
            )
        all_low_count = _require_int(
            row["all_low_energy_parent_count"],
            f"{path}.all_low_energy_parent_count",
        )
        if all_low_count != sum(
            bool(item["all_low_energy"]) for item in matching
        ):
            raise SynthesizedPcmSilenceError(
                f"{path}.all_low_energy_parent_count does not reconcile"
            )
        partition = _validate_partition(
            row,
            path=path,
            sample_count=normalized_totals["sample_count"],
        )
        sum_fields = _PARTITION_COUNT_FIELDS - {
            "longest_internal_low_energy_run_samples"
        }
        for field_name in sum_fields:
            if partition[field_name] != sum(
                item[field_name] for item in matching
            ):
                raise SynthesizedPcmSilenceError(
                    f"{path}.{field_name} does not reconcile"
                )
        longest = max(
            (
                item["longest_internal_low_energy_run_samples"]
                for item in matching
            ),
            default=0,
        )
        if (
            partition["longest_internal_low_energy_run_samples"]
            != longest
        ):
            raise SynthesizedPcmSilenceError(
                f"{path}.longest_internal_low_energy_run_samples "
                "does not reconcile"
            )
        normalized_threshold_totals.append(
            {
                "threshold_dbfs": threshold,
                "parent_count": parent_count,
                "all_low_energy_parent_count": all_low_count,
                **partition,
            }
        )

    return {
        "schema_version": SYNTHESIZED_PCM_SILENCE_SCHEMA_VERSION,
        "observation_type": SYNTHESIZED_PCM_SILENCE_OBSERVATION_TYPE,
        "method": copy.deepcopy(_METHOD),
        "totals": normalized_totals,
        "threshold_totals": normalized_threshold_totals,
        "parent_threshold_rows": normalized_parent_rows,
        "privacy": copy.deepcopy(_PRIVACY),
    }
