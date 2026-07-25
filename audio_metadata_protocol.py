"""Strict, privacy-safe receiver for opt-in translated-audio metadata.

Protocol version 1 is observation-only: one JSON ``audio_frame`` header must
immediately precede each binary PCM message, and one
``audio_parent_complete`` marker must close each parent.  This module validates
only numeric identity, source timing, and PCM-format fields; it deliberately
rejects unknown fields so capture artifacts cannot accidentally retain text or
other identifying data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


AUDIO_METADATA_PROTOCOL_VERSION = 1

_FRAME_FIELDS = {
    "type",
    "protocolVersion",
    "streamGeneration",
    "parentSequenceId",
    "audioFrameId",
    "audioBytes",
    "sampleRateHz",
    "channels",
    "bytesPerSample",
    "sourceStartMs",
    "sourceEndMs",
}
_PARENT_COMPLETE_FIELDS = {
    "type",
    "protocolVersion",
    "streamGeneration",
    "parentSequenceId",
    "audioFrameCount",
    "audioBytes",
    "sourceStartMs",
    "sourceEndMs",
}


class AudioMetadataProtocolError(ValueError):
    """Raised when the negotiated metadata stream violates its contract."""


def _require_exact_fields(
    payload: dict[str, Any],
    expected: set[str],
    message_type: str,
) -> None:
    observed = set(payload)
    if observed == expected:
        return
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    details = []
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if unexpected:
        details.append(f"unexpected {', '.join(unexpected)}")
    raise AudioMetadataProtocolError(
        f"{message_type} fields are invalid ({'; '.join(details)})"
    )


def _require_int(
    value: Any,
    field_name: str,
    *,
    minimum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise AudioMetadataProtocolError(
            f"{field_name} must be an integer at least {minimum}"
        )
    return value


def _optional_finite_nonnegative(
    value: Any,
    field_name: str,
) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise AudioMetadataProtocolError(
            f"{field_name} must be null or a finite non-negative number"
        )
    return float(value)


def _source_range(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    start_ms = _optional_finite_nonnegative(
        payload["sourceStartMs"],
        "sourceStartMs",
    )
    end_ms = _optional_finite_nonnegative(
        payload["sourceEndMs"],
        "sourceEndMs",
    )
    if start_ms is not None and end_ms is not None and end_ms < start_ms:
        raise AudioMetadataProtocolError(
            "sourceEndMs cannot precede sourceStartMs"
        )
    return start_ms, end_ms


def _validate_common(payload: dict[str, Any]) -> tuple[int, int]:
    version = _require_int(
        payload["protocolVersion"],
        "protocolVersion",
        minimum=1,
    )
    if version != AUDIO_METADATA_PROTOCOL_VERSION:
        raise AudioMetadataProtocolError(
            "protocolVersion must equal the negotiated version 1"
        )
    generation = _require_int(
        payload["streamGeneration"],
        "streamGeneration",
        minimum=1,
    )
    return version, generation


def validate_audio_frame(payload: Any) -> dict[str, Any]:
    """Return a normalized frame header or raise fail-closed."""

    if not isinstance(payload, dict):
        raise AudioMetadataProtocolError("audio_frame must be an object")
    _require_exact_fields(payload, _FRAME_FIELDS, "audio_frame")
    if payload.get("type") != "audio_frame":
        raise AudioMetadataProtocolError("audio_frame type is invalid")
    version, generation = _validate_common(payload)
    parent_id = _require_int(
        payload["parentSequenceId"],
        "parentSequenceId",
        minimum=0,
    )
    frame_id = _require_int(
        payload["audioFrameId"],
        "audioFrameId",
        minimum=0,
    )
    audio_bytes = _require_int(
        payload["audioBytes"],
        "audioBytes",
        minimum=1,
    )
    sample_rate_hz = _require_int(
        payload["sampleRateHz"],
        "sampleRateHz",
        minimum=1,
    )
    channels = _require_int(
        payload["channels"],
        "channels",
        minimum=1,
    )
    bytes_per_sample = _require_int(
        payload["bytesPerSample"],
        "bytesPerSample",
        minimum=1,
    )
    if audio_bytes % (channels * bytes_per_sample):
        raise AudioMetadataProtocolError(
            "audioBytes must align to the declared PCM sample size"
        )
    source_start_ms, source_end_ms = _source_range(payload)
    return {
        "type": "audio_frame",
        "protocolVersion": version,
        "streamGeneration": generation,
        "parentSequenceId": parent_id,
        "audioFrameId": frame_id,
        "audioBytes": audio_bytes,
        "sampleRateHz": sample_rate_hz,
        "channels": channels,
        "bytesPerSample": bytes_per_sample,
        "sourceStartMs": source_start_ms,
        "sourceEndMs": source_end_ms,
    }


def validate_audio_parent_complete(payload: Any) -> dict[str, Any]:
    """Return a normalized parent marker or raise fail-closed."""

    if not isinstance(payload, dict):
        raise AudioMetadataProtocolError(
            "audio_parent_complete must be an object"
        )
    _require_exact_fields(
        payload,
        _PARENT_COMPLETE_FIELDS,
        "audio_parent_complete",
    )
    if payload.get("type") != "audio_parent_complete":
        raise AudioMetadataProtocolError(
            "audio_parent_complete type is invalid"
        )
    version, generation = _validate_common(payload)
    parent_id = _require_int(
        payload["parentSequenceId"],
        "parentSequenceId",
        minimum=0,
    )
    frame_count = _require_int(
        payload["audioFrameCount"],
        "audioFrameCount",
        minimum=1,
    )
    audio_bytes = _require_int(
        payload["audioBytes"],
        "audioBytes",
        minimum=1,
    )
    source_start_ms, source_end_ms = _source_range(payload)
    return {
        "type": "audio_parent_complete",
        "protocolVersion": version,
        "streamGeneration": generation,
        "parentSequenceId": parent_id,
        "audioFrameCount": frame_count,
        "audioBytes": audio_bytes,
        "sourceStartMs": source_start_ms,
        "sourceEndMs": source_end_ms,
    }


@dataclass
class AudioMetadataTracker:
    """Validate header/binary pairing and complete-parent reconciliation."""

    enabled: bool
    protocol_version: int | None = None
    stream_generation: int | None = None
    stream_sample_rate_hz: int | None = None
    stream_channels: int | None = None
    stream_bytes_per_sample: int | None = None
    pending_frame: dict[str, Any] | None = None
    next_parent_sequence_id: int = 0
    active_parent_sequence_id: int | None = None
    active_frame_count: int = 0
    active_audio_bytes: int = 0
    active_sample_rate_hz: int | None = None
    active_channels: int | None = None
    active_bytes_per_sample: int | None = None
    active_source_start_ms: float | None = None
    active_source_end_ms: float | None = None
    paired_frames: list[dict[str, Any]] = field(default_factory=list)
    completed_parents: list[dict[str, Any]] = field(default_factory=list)
    terminal_received: bool = False

    def __post_init__(self) -> None:
        if self.enabled:
            if self.protocol_version != AUDIO_METADATA_PROTOCOL_VERSION:
                raise AudioMetadataProtocolError(
                    "enabled tracker requires protocol version 1"
                )
        elif self.protocol_version is not None:
            raise AudioMetadataProtocolError(
                "disabled tracker cannot declare a protocol version"
            )

    def _require_generation(self, generation: int) -> None:
        if self.stream_generation is None:
            self.stream_generation = generation
        elif generation != self.stream_generation:
            raise AudioMetadataProtocolError(
                "streamGeneration changed within one stream"
            )

    def accept_control(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Consume a control message, returning normalized metadata if present."""

        message_type = payload.get("type")
        if self.terminal_received and message_type in {
            "audio_frame",
            "audio_parent_complete",
        }:
            raise AudioMetadataProtocolError(
                "audio metadata arrived after the completed terminal"
            )
        if message_type not in {"audio_frame", "audio_parent_complete"}:
            if self.pending_frame is not None:
                raise AudioMetadataProtocolError(
                    "audio_frame must be followed immediately by binary PCM"
                )
            return None
        if not self.enabled:
            raise AudioMetadataProtocolError(
                "received audio metadata without negotiating protocol version 1"
            )
        if self.pending_frame is not None:
            raise AudioMetadataProtocolError(
                "audio_frame must be followed immediately by binary PCM"
            )
        if message_type == "audio_frame":
            frame = validate_audio_frame(payload)
            self._require_generation(frame["streamGeneration"])
            if self.stream_sample_rate_hz is None:
                self.stream_sample_rate_hz = frame["sampleRateHz"]
                self.stream_channels = frame["channels"]
                self.stream_bytes_per_sample = frame["bytesPerSample"]
            elif (
                frame["sampleRateHz"] != self.stream_sample_rate_hz
                or frame["channels"] != self.stream_channels
                or frame["bytesPerSample"]
                != self.stream_bytes_per_sample
            ):
                raise AudioMetadataProtocolError(
                    "PCM format must remain stable within a stream"
                )
            parent_id = frame["parentSequenceId"]
            if self.active_parent_sequence_id is None:
                if parent_id != self.next_parent_sequence_id:
                    raise AudioMetadataProtocolError(
                        "parentSequenceId must be contiguous and ordered from zero"
                    )
                if frame["audioFrameId"] != 0:
                    raise AudioMetadataProtocolError(
                        "the first audioFrameId in a parent must be zero"
                    )
            else:
                if parent_id != self.active_parent_sequence_id:
                    raise AudioMetadataProtocolError(
                        "a new parent began before the active parent completed"
                    )
                if frame["audioFrameId"] != self.active_frame_count:
                    raise AudioMetadataProtocolError(
                        "audioFrameId must be contiguous within its parent"
                    )
                if (
                    frame["sampleRateHz"] != self.active_sample_rate_hz
                    or frame["channels"] != self.active_channels
                    or frame["bytesPerSample"]
                    != self.active_bytes_per_sample
                    or frame["sourceStartMs"]
                    != self.active_source_start_ms
                    or frame["sourceEndMs"] != self.active_source_end_ms
                ):
                    raise AudioMetadataProtocolError(
                        "PCM format and source range must remain stable "
                        "within a parent"
                    )
            self.pending_frame = frame
            return frame

        completion = validate_audio_parent_complete(payload)
        self._require_generation(completion["streamGeneration"])
        if self.active_parent_sequence_id is None:
            raise AudioMetadataProtocolError(
                "audio_parent_complete has no active parent"
            )
        if completion["parentSequenceId"] != self.active_parent_sequence_id:
            raise AudioMetadataProtocolError(
                "audio_parent_complete references the wrong parent"
            )
        if completion["audioFrameCount"] != self.active_frame_count:
            raise AudioMetadataProtocolError(
                "audio_parent_complete frame count does not reconcile"
            )
        if completion["audioBytes"] != self.active_audio_bytes:
            raise AudioMetadataProtocolError(
                "audio_parent_complete byte count does not reconcile"
            )
        if (
            completion["sourceStartMs"] != self.active_source_start_ms
            or completion["sourceEndMs"] != self.active_source_end_ms
        ):
            raise AudioMetadataProtocolError(
                "audio_parent_complete source range does not reconcile"
            )
        self.completed_parents.append(completion)
        self.next_parent_sequence_id += 1
        self.active_parent_sequence_id = None
        self.active_frame_count = 0
        self.active_audio_bytes = 0
        self.active_sample_rate_hz = None
        self.active_channels = None
        self.active_bytes_per_sample = None
        self.active_source_start_ms = None
        self.active_source_end_ms = None
        return completion

    def accept_binary_size(
        self,
        audio_bytes: int,
    ) -> dict[str, Any] | None:
        """Pair one binary message length with its preceding frame header."""

        if (
            not isinstance(audio_bytes, int)
            or isinstance(audio_bytes, bool)
            or audio_bytes <= 0
        ):
            raise AudioMetadataProtocolError(
                "binary PCM byte count must be a positive integer"
            )
        if not self.enabled:
            return None
        if self.terminal_received:
            raise AudioMetadataProtocolError(
                "binary PCM arrived after the completed terminal"
            )
        if self.pending_frame is None:
            raise AudioMetadataProtocolError(
                "binary PCM arrived without an audio_frame header"
            )
        frame = self.pending_frame
        if audio_bytes != frame["audioBytes"]:
            raise AudioMetadataProtocolError(
                "binary PCM byte count does not match audio_frame"
            )
        if self.active_parent_sequence_id is None:
            self.active_parent_sequence_id = frame["parentSequenceId"]
            self.active_sample_rate_hz = frame["sampleRateHz"]
            self.active_channels = frame["channels"]
            self.active_bytes_per_sample = frame["bytesPerSample"]
            self.active_source_start_ms = frame["sourceStartMs"]
            self.active_source_end_ms = frame["sourceEndMs"]
        self.active_frame_count += 1
        self.active_audio_bytes += audio_bytes
        self.paired_frames.append(frame)
        self.pending_frame = None
        return frame

    def accept_binary(self, payload: bytes) -> dict[str, Any] | None:
        """Pair one binary PCM message with its immediately preceding header."""

        if not isinstance(payload, bytes):
            raise AudioMetadataProtocolError("binary PCM must be bytes")
        return self.accept_binary_size(len(payload))

    def assert_terminal_ready(self) -> None:
        """Reject a completed terminal while any parent remains incomplete."""

        if not self.enabled:
            return
        if self.terminal_received:
            raise AudioMetadataProtocolError(
                "duplicate completed terminal"
            )
        if self.pending_frame is not None:
            raise AudioMetadataProtocolError(
                "completed terminal arrived while binary PCM was pending"
            )
        if self.active_parent_sequence_id is not None:
            raise AudioMetadataProtocolError(
                "completed terminal arrived before audio_parent_complete"
            )
        self.terminal_received = True
