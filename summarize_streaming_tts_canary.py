#!/usr/bin/env python3
"""Compare matched atomic and incremental TTS-publication canary captures.

The input captures remain operational evidence and may contain local paths or
session identifiers.  This program validates those fields but emits only
privacy-safe aggregate metrics and boolean provenance conclusions.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence


ARMS = (("atomic", 1, False), ("streaming", 3, True))
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_TIME_DIGITS = 3
MATCH_TOLERANCE_SECONDS = 1e-6
MATERIAL_AUDIO_DIFFERENCE_PERCENT = 1.0
INTENDED_CONFIG_DIFFERENCES = (
    "audioMetadataProtocolVersions",
    "stagedConfig.telemetrySchemaVersion",
    "stagedConfig.ttsIncrementalPublishEnabled",
    "stagedConfig.ttsIncrementalFrameMs",
    "stagedConfig.ttsIncrementalAtomicFallbackMaxChars",
)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: required JSON could not be read") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}: JSON root must be an object")
    return value


def _object(value: Any, *, field: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: {field} must be an object")
    return value


def _list(value: Any, *, field: str, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label}: {field} must be a list")
    return value


def _integer(
    value: Any,
    *,
    field: str,
    label: str,
    positive: bool = False,
) -> int:
    minimum = 1 if positive else 0
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{label}: {field} must be a {qualifier} integer")
    return value


def _number(
    value: Any,
    *,
    field: str,
    label: str,
    positive: bool = False,
    nonnegative: bool = True,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or (positive and value <= 0)
        or (nonnegative and value < 0)
    ):
        qualifier = (
            "positive" if positive else "non-negative" if nonnegative else ""
        )
        raise ValueError(
            f"{label}: {field} must be {qualifier + ' ' if qualifier else ''}"
            "finite"
        )
    return float(value)


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot summarize an empty metric")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _distribution(values: Iterable[float]) -> dict[str, float]:
    materialized = list(values)
    if not materialized:
        raise ValueError("cannot summarize an empty metric")
    return {
        "p50": _nearest_rank(materialized, 0.50),
        "p95": _nearest_rank(materialized, 0.95),
        "max": max(materialized),
    }


def _optional_distribution(
    values: Iterable[float],
) -> dict[str, float] | None:
    observed = list(values)
    return _distribution(observed) if observed else None


def _round_floats(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {
            key: _round_floats(item, digits) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, digits) for item in value]
    return value


def _optional_time(value: Any, *, field: str, label: str) -> float | None:
    if value is None:
        return None
    return round(
        _number(value, field=field, label=label),
        SOURCE_TIME_DIGITS,
    )


def _run_info(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("run_info: required run_info.txt could not be read") from exc
    result: dict[str, str] = {}
    for line in lines:
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    prefix_sha256 = result.get("prefix_sha256", "")
    if re.fullmatch(r"[0-9a-f]{64}", prefix_sha256) is None:
        raise ValueError("run_info: prefix_sha256 must be a SHA-256 digest")
    fallback_max_chars = result.get(
        "incremental_atomic_fallback_max_chars",
        "0",
    )
    if not fallback_max_chars.isdigit():
        raise ValueError(
            "run_info: incremental_atomic_fallback_max_chars must be "
            "a non-negative integer"
        )
    if result.get("streaming_audio_metadata_protocol_version") != "1":
        raise ValueError(
            "run_info: streaming_audio_metadata_protocol_version must be 1"
        )
    return result


def _backend_provenance(
    config: dict[str, Any],
    *,
    schema: int,
    incremental: bool,
    label: str,
) -> dict[str, Any]:
    if config.get("pipelineMode") != "staged":
        raise ValueError(f"{label}: backend pipeline mode must be staged")
    expected_metadata_versions = [1] if incremental else []
    if (
        config.get("audioMetadataProtocolVersions")
        != expected_metadata_versions
    ):
        raise ValueError(
            f"{label}: audio metadata protocol capability mismatch"
        )
    for field in ("sampleRate", "chunkSize", "channels"):
        _integer(config.get(field), field=field, label=label, positive=True)

    models = _object(config.get("modelConfig"), field="modelConfig", label=label)
    for service_name in ("asr", "nmt", "tts"):
        service = _object(
            models.get(service_name),
            field=f"modelConfig.{service_name}",
            label=label,
        )
        image = service.get("image")
        endpoint = service.get("endpoint")
        digest = service.get("imageDigest")
        if (
            not isinstance(image, str)
            or not image
            or image.endswith(":latest")
        ):
            raise ValueError(
                f"{label}: modelConfig.{service_name}.image must be pinned"
            )
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError(
                f"{label}: modelConfig.{service_name}.endpoint is missing"
            )
        if (
            not isinstance(digest, str)
            or IMAGE_DIGEST_PATTERN.fullmatch(digest) is None
        ):
            raise ValueError(
                f"{label}: modelConfig.{service_name}.imageDigest must be "
                "an immutable digest"
            )

    staged = _object(
        config.get("stagedConfig"),
        field="stagedConfig",
        label=label,
    )
    if staged.get("telemetrySchemaVersion") != schema:
        raise ValueError(f"{label}: backend telemetry schema mismatch")
    if staged.get("ttsSubsegmentMaxChars") != 0:
        raise ValueError(f"{label}: post-NMT TTS splitting must be disabled")
    if staged.get("ttsResponseChunkTelemetryEnabled") is not True:
        raise ValueError(
            f"{label}: TTS response-chunk telemetry must be enabled"
        )
    reported_incremental = staged.get(
        "ttsIncrementalPublishEnabled",
        False,
    )
    if reported_incremental is not incremental:
        raise ValueError(f"{label}: incremental-publication flag mismatch")
    reported_handoff = staged.get(
        "ttsPublisherHandoffTelemetryEnabled"
    )
    if (
        reported_handoff is not None
        and reported_handoff is not incremental
    ):
        raise ValueError(f"{label}: publisher-handoff flag mismatch")
    if incremental:
        _integer(
            staged.get("ttsIncrementalFrameMs"),
            field="ttsIncrementalFrameMs",
            label=label,
            positive=True,
        )
        fallback_max_chars = staged.get(
            "ttsIncrementalAtomicFallbackMaxChars",
            0,
        )
        if (
            not isinstance(fallback_max_chars, int)
            or isinstance(fallback_max_chars, bool)
            or fallback_max_chars < 0
        ):
            raise ValueError(
                f"{label}: ttsIncrementalAtomicFallbackMaxChars must be "
                "a non-negative integer"
            )
    elif staged.get("ttsIncrementalAtomicFallbackMaxChars", 0) != 0:
        raise ValueError(
            f"{label}: atomic control cannot enable incremental fallback"
        )

    projection = copy.deepcopy(config)
    projection.pop("audioMetadataProtocolVersions", None)
    projection_staged = projection["stagedConfig"]
    for field in (
        "telemetrySchemaVersion",
        "ttsIncrementalPublishEnabled",
        "ttsIncrementalFrameMs",
        "ttsIncrementalAtomicFallbackMaxChars",
        "ttsPublisherHandoffTelemetryEnabled",
    ):
        projection_staged.pop(field, None)
    return projection


def _upstream_structure(
    events: list[dict[str, Any]],
    *,
    label: str,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], float]:
    pipeline_starts = [
        event
        for event in events
        if (event.get("stage"), event.get("event"))
        == ("pipeline", "started")
    ]
    if len(pipeline_starts) != 1:
        raise ValueError(f"{label}: exactly one pipeline start is required")
    pipeline_start_ms = _number(
        pipeline_starts[0].get("monotonic_ms"),
        field="pipeline start",
        label=label,
    )

    finals: list[tuple[Any, ...]] = []
    segments: list[tuple[Any, ...]] = []
    nmt_records: list[tuple[Any, ...]] = []
    nmt_by_parent: dict[int, dict[str, Any]] = {}
    for index, event in enumerate(events):
        event_label = f"{label}: event {index}"
        event_type = (event.get("stage"), event.get("event"))
        if event_type == ("asr", "final"):
            final_id = _integer(
                event.get("asr_final_id"),
                field="asr_final_id",
                label=event_label,
            )
            finals.append(
                (
                    final_id,
                    _integer(
                        event.get("text_chars"),
                        field="text_chars",
                        label=event_label,
                        positive=True,
                    ),
                    _optional_time(
                        event.get("source_start_ms"),
                        field="source_start_ms",
                        label=event_label,
                    ),
                    _optional_time(
                        event.get("source_end_ms"),
                        field="source_end_ms",
                        label=event_label,
                    ),
                )
            )
        elif event_type in {
            ("segmenter", "emitted"),
            ("nmt", "completed"),
        }:
            parent_id = _integer(
                event.get("sequence_id"),
                field="sequence_id",
                label=event_label,
            )
            contributing = _list(
                event.get("contributing_final_ids"),
                field="contributing_final_ids",
                label=event_label,
            )
            contributing_ids = tuple(
                _integer(
                    item,
                    field="contributing_final_ids",
                    label=event_label,
                )
                for item in contributing
            )
            reason = event.get("emission_reason")
            if not isinstance(reason, str) or not reason:
                raise ValueError(
                    f"{event_label}: emission_reason must be non-empty"
                )
            record = (
                parent_id,
                contributing_ids,
                reason,
                _integer(
                    event.get("text_chars"),
                    field="text_chars",
                    label=event_label,
                    positive=True,
                ),
                _optional_time(
                    event.get("source_start_ms"),
                    field="source_start_ms",
                    label=event_label,
                ),
                _optional_time(
                    event.get("source_end_ms"),
                    field="source_end_ms",
                    label=event_label,
                ),
            )
            if event_type[0] == "segmenter":
                segments.append(record)
            else:
                if parent_id in nmt_by_parent:
                    raise ValueError(f"{label}: duplicate NMT parent")
                nmt_records.append(record)
                nmt_by_parent[parent_id] = event

    if not finals or not segments or not nmt_records:
        raise ValueError(f"{label}: upstream evidence is incomplete")
    if [item[0] for item in finals] != list(range(len(finals))):
        raise ValueError(f"{label}: ASR final IDs are not contiguous")
    expected_parents = list(range(len(segments)))
    if [item[0] for item in segments] != expected_parents:
        raise ValueError(f"{label}: segment parent IDs are not contiguous")
    if [item[0] for item in nmt_records] != expected_parents:
        raise ValueError(f"{label}: NMT parent IDs are not contiguous")
    return (
        {
            "asr_finals": finals,
            "segments": segments,
            "nmt_parents": nmt_records,
        },
        nmt_by_parent,
        pipeline_start_ms,
    )


def _event_parent_map(
    events: list[dict[str, Any]],
    *,
    stage: str,
    event_name: str,
    parent_count: int,
    label: str,
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for event in events:
        if (event.get("stage"), event.get("event")) != (stage, event_name):
            continue
        parent = _integer(
            event.get("sequence_id"),
            field=f"{stage}/{event_name}.sequence_id",
            label=label,
        )
        if parent in result:
            raise ValueError(f"{label}: duplicate {stage}/{event_name} parent")
        result[parent] = event
    if sorted(result) != list(range(parent_count)):
        raise ValueError(
            f"{label}: {stage}/{event_name} parent structure is incomplete"
        )
    return result


def _elapsed_seconds(
    later: dict[str, Any],
    earlier: dict[str, Any],
    *,
    field: str,
    label: str,
) -> float:
    later_ms = _number(
        later.get("monotonic_ms"),
        field=f"{field}.later",
        label=label,
    )
    earlier_ms = _number(
        earlier.get("monotonic_ms"),
        field=f"{field}.earlier",
        label=label,
    )
    value = (later_ms - earlier_ms) / 1_000.0
    if value < -1e-6:
        raise ValueError(f"{label}: {field} is negative")
    return max(0.0, value)


def _fallback_metadata(
    staged: dict[str, Any],
    backend_config: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    parent_count: int,
    incremental: bool,
    label: str,
) -> dict[str, Any]:
    """Normalize old schema-3 evidence and reconcile new fallback fields."""

    max_chars = staged.get(
        "tts_incremental_atomic_fallback_max_chars",
        0,
    )
    fallback_count = staged.get(
        "tts_incremental_atomic_fallback_parent_count",
        0,
    )
    if (
        not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
        or max_chars < 0
    ):
        raise ValueError(f"{label}: fallback max chars is invalid")
    if (
        not isinstance(fallback_count, int)
        or isinstance(fallback_count, bool)
        or fallback_count < 0
    ):
        raise ValueError(f"{label}: fallback parent count is invalid")
    raw_ids = _list(
        staged.get(
            "tts_incremental_atomic_fallback_parent_sequence_ids",
            [],
        ),
        field="tts_incremental_atomic_fallback_parent_sequence_ids",
        label=label,
    )
    fallback_ids = [
        _integer(
            parent_id,
            field="fallback parent sequence ID",
            label=label,
        )
        for parent_id in raw_ids
    ]
    if (
        fallback_count != len(fallback_ids)
        or fallback_ids != sorted(set(fallback_ids))
        or any(parent_id >= parent_count for parent_id in fallback_ids)
    ):
        raise ValueError(f"{label}: fallback parent metadata is inconsistent")
    if max_chars == 0 and fallback_ids:
        raise ValueError(
            f"{label}: fallback parents require a positive threshold"
        )
    configured_max_chars = (
        _object(
            backend_config.get("stagedConfig"),
            field="stagedConfig",
            label=label,
        ).get("ttsIncrementalAtomicFallbackMaxChars", 0)
        if incremental
        else 0
    )
    if configured_max_chars != max_chars:
        raise ValueError(
            f"{label}: fallback threshold does not match API config"
        )

    normalized_layers: list[list[tuple[int, int, int, int, bool]]] = []
    for field in (
        "produced_parent_summaries",
        "completed_parent_summaries",
        "websocket_completed_parent_summaries",
    ):
        summaries = _list(staged.get(field), field=field, label=label)
        normalized: list[tuple[int, int, int, int, bool]] = []
        for index, raw_summary in enumerate(summaries):
            summary = _object(
                raw_summary,
                field=f"{field}[{index}]",
                label=label,
            )
            fallback_applied = summary.get(
                "atomic_fallback_applied",
                False,
            )
            if not isinstance(fallback_applied, bool):
                raise ValueError(
                    f"{label}: {field}[{index}] fallback flag is invalid"
                )
            normalized.append(
                (
                    _integer(
                        summary.get("parent_sequence_id"),
                        field=f"{field}.parent_sequence_id",
                        label=label,
                    ),
                    _integer(
                        summary.get("audio_frame_count"),
                        field=f"{field}.audio_frame_count",
                        label=label,
                        positive=True,
                    ),
                    _integer(
                        summary.get("audio_bytes"),
                        field=f"{field}.audio_bytes",
                        label=label,
                        positive=True,
                    ),
                    _integer(
                        summary.get("retry_count"),
                        field=f"{field}.retry_count",
                        label=label,
                    ),
                    fallback_applied,
                )
            )
        normalized_layers.append(normalized)
    if normalized_layers[1:] != normalized_layers[:-1]:
        raise ValueError(f"{label}: fallback parent summary layers differ")
    canonical = normalized_layers[0]
    if [item[0] for item in canonical] != list(range(parent_count)):
        raise ValueError(f"{label}: fallback parent summaries are incomplete")
    flagged_ids = [item[0] for item in canonical if item[4]]
    if flagged_ids != fallback_ids:
        raise ValueError(
            f"{label}: fallback flags do not match fallback parent IDs"
        )
    if max_chars > 0:
        started_text_chars: dict[int, int] = {}
        for event in events:
            if (event.get("stage"), event.get("event")) != (
                "tts",
                "started",
            ):
                continue
            parent_id = _integer(
                event.get("sequence_id"),
                field="tts/started.sequence_id",
                label=label,
            )
            if parent_id in started_text_chars:
                raise ValueError(f"{label}: duplicate tts/started parent")
            started_text_chars[parent_id] = _integer(
                event.get("text_chars"),
                field="tts/started.text_chars",
                label=label,
                positive=True,
            )
        if sorted(started_text_chars) != list(range(parent_count)):
            raise ValueError(
                f"{label}: fallback policy requires tts/started text_chars "
                "for every parent"
            )
        policy_fallback_ids = [
            parent_id
            for parent_id in range(parent_count)
            if started_text_chars[parent_id] <= max_chars
        ]
        if policy_fallback_ids != fallback_ids:
            raise ValueError(
                f"{label}: fallback IDs do not match configured text "
                "threshold"
            )

    expected_event_flags = [
        (parent_id, parent_id in set(fallback_ids))
        for parent_id in range(parent_count)
    ]
    for event_type in (
        ("tts", "completed"),
        ("output", "parent_complete_enqueued"),
        ("output", "parent_complete_dequeued"),
    ):
        records = []
        for event in events:
            if (event.get("stage"), event.get("event")) != event_type:
                continue
            fallback_applied = event.get(
                "atomic_fallback_applied",
                False,
            )
            if not isinstance(fallback_applied, bool):
                raise ValueError(
                    f"{label}: {event_type} fallback flag is invalid"
                )
            records.append((event.get("sequence_id"), fallback_applied))
        if records != expected_event_flags:
            raise ValueError(
                f"{label}: {event_type} fallback flags are inconsistent"
            )
    return {
        "max_chars": max_chars,
        "parent_count": fallback_count,
        "parent_sequence_ids": fallback_ids,
    }


def _boundary_seconds(
    event: dict[str, Any],
    *,
    pipeline_start_ms: float,
    label: str,
) -> float:
    event_ms = _number(
        event.get("monotonic_ms"),
        field="monotonic_ms",
        label=label,
    )
    source_end_ms = _number(
        event.get("source_end_ms"),
        field="source_end_ms",
        label=label,
    )
    return (event_ms - pipeline_start_ms - source_end_ms) / 1_000.0


def _tts_metrics(
    staged: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    parent_count: int,
    nmt_by_parent: dict[int, dict[str, Any]],
    pipeline_start_ms: float,
    schema: int,
    incremental: bool,
    fallback_parent_ids: set[int],
    label: str,
) -> tuple[dict[str, Any], dict[int, int]]:
    started = _event_parent_map(
        events,
        stage="tts",
        event_name="started",
        parent_count=parent_count,
        label=label,
    )
    first = _event_parent_map(
        events,
        stage="tts",
        event_name="first_audio",
        parent_count=parent_count,
        label=label,
    )
    completed = _event_parent_map(
        events,
        stage="tts",
        event_name="completed",
        parent_count=parent_count,
        label=label,
    )
    completed_bytes: dict[int, int] = {}
    for parent, event in completed.items():
        completed_bytes[parent] = _integer(
            event.get("audio_bytes"),
            field="tts/completed.audio_bytes",
            label=label,
            positive=True,
        )

    sidecar = _object(
        staged.get("tts_response_chunk_telemetry"),
        field="tts_response_chunk_telemetry",
        label=label,
    )
    if sidecar.get("schema_version") != 1:
        raise ValueError(f"{label}: unsupported response telemetry schema")
    if sidecar.get("segments_observed") != parent_count:
        raise ValueError(f"{label}: response parent count mismatch")
    chunks = _list(
        sidecar.get("chunks"),
        field="tts_response_chunk_telemetry.chunks",
        label=label,
    )
    if sidecar.get("response_chunk_count") != len(chunks):
        raise ValueError(f"{label}: response chunk count mismatch")
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        item = _object(chunk, field="response chunk", label=label)
        parent = _integer(
            item.get("parent_sequence_id"),
            field="response parent_sequence_id",
            label=label,
        )
        if item.get("subsequence_id") != 0 or item.get("subsequence_count") != 1:
            raise ValueError(f"{label}: TTS response was unexpectedly split")
        grouped[parent].append(item)
    if sorted(grouped) != list(range(parent_count)):
        raise ValueError(f"{label}: response parent identities are incomplete")

    first_response_seconds: list[float] = []
    last_response_seconds: list[float] = []
    response_counts: list[float] = []
    for parent, observed in grouped.items():
        declared = len(observed)
        cumulative = 0
        for index, item in enumerate(observed):
            if (
                item.get("response_index") != index
                or item.get("response_count") != declared
            ):
                raise ValueError(f"{label}: response indices are inconsistent")
            cumulative += _integer(
                item.get("audio_bytes"),
                field="response audio_bytes",
                label=label,
                positive=True,
            )
            if item.get("cumulative_audio_bytes") != cumulative:
                raise ValueError(
                    f"{label}: cumulative response bytes are inconsistent"
                )
        if cumulative != completed_bytes[parent]:
            raise ValueError(
                f"{label}: response bytes do not match TTS completion"
            )
        first_response_seconds.append(
            _number(
                observed[0].get("since_request_start_ms"),
                field="first response latency",
                label=label,
            )
            / 1_000.0
        )
        last_response_seconds.append(
            _number(
                observed[-1].get("since_request_start_ms"),
                field="last response latency",
                label=label,
            )
            / 1_000.0
        )
        response_counts.append(float(declared))

    websocket_events = _list(
        staged.get("websocket_send_events"),
        field="websocket_send_events",
        label=label,
    )
    websocket_by_parent: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in websocket_events:
        item = _object(event, field="websocket send event", label=label)
        parent_field = (
            "parent_sequence_id"
            if schema == 3
            else "sequence_id"
        )
        parent = _integer(
            item.get(parent_field),
            field=parent_field,
            label=label,
        )
        websocket_by_parent[parent].append(item)
    if sorted(websocket_by_parent) != list(range(parent_count)):
        raise ValueError(f"{label}: WebSocket parent identities are incomplete")

    nmt_to_request = []
    request_to_first = []
    request_to_full = []
    first_to_full = []
    boundary_to_request = []
    boundary_to_first = []
    boundary_to_full = []
    boundary_to_first_websocket = []
    first_to_websocket = []
    first_websocket_lead_over_full = []
    fallback_completion_to_first_websocket = []
    for parent in range(parent_count):
        first_ws = websocket_by_parent[parent][0]
        request = started[parent]
        first_audio = first[parent]
        full_audio = completed[parent]
        nmt_to_request.append(
            _elapsed_seconds(
                request,
                nmt_by_parent[parent],
                field="NMT completion to TTS request",
                label=label,
            )
        )
        request_to_first.append(
            _elapsed_seconds(
                first_audio,
                request,
                field="TTS request to first response",
                label=label,
            )
        )
        request_to_full.append(
            _elapsed_seconds(
                full_audio,
                request,
                field="TTS request to full response",
                label=label,
            )
        )
        first_to_full.append(
            _elapsed_seconds(
                full_audio,
                first_audio,
                field="TTS first to full response",
                label=label,
            )
        )
        boundary_to_request.append(
            _boundary_seconds(
                request,
                pipeline_start_ms=pipeline_start_ms,
                label=label,
            )
        )
        boundary_to_first.append(
            _boundary_seconds(
                first_audio,
                pipeline_start_ms=pipeline_start_ms,
                label=label,
            )
        )
        boundary_to_full.append(
            _boundary_seconds(
                full_audio,
                pipeline_start_ms=pipeline_start_ms,
                label=label,
            )
        )
        source_end_ms = _number(
            request.get("source_end_ms"),
            field="TTS source_end_ms",
            label=label,
        )
        ws_ms = _number(
            first_ws.get("sent_monotonic_ms"),
            field="WebSocket sent_monotonic_ms",
            label=label,
        )
        boundary_to_first_websocket.append(
            (ws_ms - pipeline_start_ms - source_end_ms) / 1_000.0
        )
        first_to_websocket_seconds = (
            (
                ws_ms
                - _number(
                    first_audio.get("monotonic_ms"),
                    field="TTS first monotonic_ms",
                    label=label,
                )
            )
            / 1_000.0
        )
        if first_to_websocket_seconds < -1e-6:
            raise ValueError(
                f"{label}: WebSocket publication preceded first TTS response"
            )
        full_audio_ms = _number(
            full_audio.get("monotonic_ms"),
            field="TTS full monotonic_ms",
            label=label,
        )
        lead_seconds = (full_audio_ms - ws_ms) / 1_000.0
        if incremental and parent in fallback_parent_ids:
            if lead_seconds > 1e-6:
                raise ValueError(
                    f"{label}: atomic fallback frame was published before "
                    "TTS completion"
                )
            fallback_completion_to_first_websocket.append(-lead_seconds)
        else:
            first_to_websocket.append(first_to_websocket_seconds)
            first_websocket_lead_over_full.append(lead_seconds)

    return (
        {
            "requests": parent_count,
            "response_chunks": len(chunks),
            "websocket_audio_messages": len(websocket_events),
            "nmt_complete_to_request_seconds": _distribution(nmt_to_request),
            "request_to_first_response_seconds": _distribution(request_to_first),
            "request_to_last_response_seconds": _distribution(
                last_response_seconds
            ),
            "request_to_full_response_seconds": _distribution(request_to_full),
            "first_to_full_response_seconds": _distribution(first_to_full),
            "first_response_to_first_websocket_seconds": (
                _optional_distribution(
                first_to_websocket
                )
            ),
            "first_websocket_lead_over_full_response_seconds": (
                _optional_distribution(
                first_websocket_lead_over_full
                )
            ),
            "direct_incremental_parent_count": (
                parent_count - len(fallback_parent_ids)
                if incremental
                else 0
            ),
            "excluded_atomic_fallback_parent_count": (
                len(fallback_parent_ids) if incremental else 0
            ),
            "atomic_fallback_completion_to_first_websocket_seconds": (
                _optional_distribution(
                    fallback_completion_to_first_websocket
                )
            ),
            "source_boundary_to_request_seconds": _distribution(
                boundary_to_request
            ),
            "source_boundary_to_first_response_seconds": _distribution(
                boundary_to_first
            ),
            "source_boundary_to_full_response_seconds": _distribution(
                boundary_to_full
            ),
            "source_boundary_to_first_websocket_seconds": _distribution(
                boundary_to_first_websocket
            ),
            "response_count_per_request": _distribution(response_counts),
        },
        completed_bytes,
    )


def _playback_metrics(
    analysis: dict[str, Any],
    *,
    output_seconds: float,
    audio_messages: int,
    expected_csv_name: str,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if analysis.get("schema_version") not in {1, 2}:
        raise ValueError(f"{label}: unsupported playback schema")
    policy = _object(analysis.get("policy"), field="policy", label=label)
    traces = _list(analysis.get("traces"), field="traces", label=label)
    if len(traces) != 1:
        raise ValueError(f"{label}: exactly one playback trace is required")
    trace = _object(traces[0], field="trace", label=label)
    if trace.get("trace_csv") != expected_csv_name:
        raise ValueError(f"{label}: playback trace does not match capture")
    translated = _number(
        trace.get("translated_audio_seconds"),
        field="translated_audio_seconds",
        label=label,
        positive=True,
    )
    if not math.isclose(translated, output_seconds, abs_tol=0.005):
        raise ValueError(f"{label}: playback duration does not match capture")

    result: dict[str, Any] = {}
    for mode in ("fixed_1x", "adaptive"):
        source = _object(trace.get(mode), field=mode, label=label)
        if source.get("chunks_scheduled") != audio_messages:
            raise ValueError(
                f"{label}: scheduled playback chunks do not match audio messages"
            )
        if source.get("chunks_dropped") != 0:
            raise ValueError(f"{label}: playback analysis dropped audio")
        result[mode] = {
            key: _number(source.get(key), field=f"{mode}.{key}", label=label)
            for key in (
                "listener_tail_seconds",
                "time_weighted_queue_p50_seconds",
                "time_weighted_queue_p95_seconds",
                "peak_queue_depth_seconds",
                "percent_playback_window_above_limit",
                "urgent_source_percent",
            )
        }
    return result, copy.deepcopy(policy)


def _audio_metadata_observation(
    root: dict[str, Any],
    *,
    incremental: bool,
    audio_messages: int,
    parent_count: int,
    label: str,
) -> dict[str, Any]:
    observation = _object(
        root.get("audio_metadata_observation"),
        field="audio_metadata_observation",
        label=label,
    )
    freshness = _object(
        observation.get("source_end_to_receipt"),
        field="audio_metadata_observation.source_end_to_receipt",
        label=label,
    )
    if observation.get("playback_behavior_changed") is not False:
        raise ValueError(
            f"{label}: metadata observation must not change playback"
        )
    if observation.get("contains_transcript_or_translation_text") is not False:
        raise ValueError(
            f"{label}: metadata observation text privacy flag failed"
        )
    if freshness.get("semantic_boundary_proven") is not False:
        raise ValueError(
            f"{label}: metadata observation cannot claim a semantic boundary"
        )
    if freshness.get("actual_audibility_proven") is not False:
        raise ValueError(
            f"{label}: metadata observation cannot claim actual audibility"
        )

    if not incremental:
        if (
            observation.get("protocol_version") is not None
            or observation.get("stream_generation") is not None
            or observation.get("input_sample_zero_timestamp_ms") is not None
            or observation.get("paired_frames") != 0
            or observation.get("completed_parents") != 0
            or freshness.get("availability")
            != "protocol_not_negotiated"
            or freshness.get("sample_count") != 0
            or any(
                freshness.get(field) is not None
                for field in ("p50_ms", "p95_ms", "max_ms")
            )
        ):
            raise ValueError(
                f"{label}: atomic control must remain on legacy audio"
            )
        return {
            "protocol_version": None,
            "negotiated": False,
            "wire_integrity_passed": True,
            "paired_frames": 0,
            "completed_parents": 0,
            "source_end_to_receipt": {
                "availability": "protocol_not_negotiated",
                "sample_count": 0,
                "p50_ms": None,
                "p95_ms": None,
                "max_ms": None,
                "semantic_boundary_proven": False,
                "actual_audibility_proven": False,
            },
        }

    if observation.get("protocol_version") != 1:
        raise ValueError(
            f"{label}: streaming arm must negotiate metadata protocol v1"
        )
    generation = _integer(
        observation.get("stream_generation"),
        field="audio_metadata_observation.stream_generation",
        label=label,
        positive=True,
    )
    _number(
        observation.get("input_sample_zero_timestamp_ms"),
        field="audio_metadata_observation.input_sample_zero_timestamp_ms",
        label=label,
    )
    paired_frames = _integer(
        observation.get("paired_frames"),
        field="audio_metadata_observation.paired_frames",
        label=label,
        positive=True,
    )
    completed_parents = _integer(
        observation.get("completed_parents"),
        field="audio_metadata_observation.completed_parents",
        label=label,
        positive=True,
    )
    if paired_frames != audio_messages:
        raise ValueError(
            f"{label}: metadata paired-frame count mismatch"
        )
    if completed_parents != parent_count:
        raise ValueError(
            f"{label}: metadata completed-parent count mismatch"
        )
    if freshness.get("clock") != "client_monotonic":
        raise ValueError(
            f"{label}: metadata freshness clock is invalid"
        )
    if freshness.get("source_offset_origin") != "input_pcm_sample_zero":
        raise ValueError(
            f"{label}: metadata source-offset origin is invalid"
        )
    availability = freshness.get("availability")
    allowed_availability = {
        "available_asr_source_range_end_offset",
        "available_audio_processed_end_offset_not_semantic_boundary",
        "unavailable_missing_source_end_offsets",
        "unavailable_missing_input_sample_zero",
    }
    if availability not in allowed_availability:
        raise ValueError(
            f"{label}: metadata freshness availability is invalid"
        )
    sample_count = _integer(
        freshness.get("sample_count"),
        field="audio_metadata_observation.source_end_to_receipt.sample_count",
        label=label,
    )
    if sample_count > paired_frames:
        raise ValueError(
            f"{label}: metadata freshness sample count exceeds frame count"
        )
    distributions: dict[str, float | None]
    if sample_count:
        distributions = {
            field: _number(
                freshness.get(f"{field}_ms"),
                field=(
                    "audio_metadata_observation.source_end_to_receipt."
                    f"{field}_ms"
                ),
                label=label,
                nonnegative=False,
            )
            for field in ("p50", "p95", "max")
        }
        if not (
            distributions["p50"]
            <= distributions["p95"]
            <= distributions["max"]
        ):
            raise ValueError(
                f"{label}: metadata freshness distribution is unordered"
            )
        if not str(availability).startswith("available_"):
            raise ValueError(
                f"{label}: metadata freshness samples require availability"
            )
    else:
        distributions = {"p50": None, "p95": None, "max": None}
        if any(
            freshness.get(f"{field}_ms") is not None
            for field in ("p50", "p95", "max")
        ):
            raise ValueError(
                f"{label}: unavailable metadata freshness must have null metrics"
            )

    return {
        "protocol_version": 1,
        "negotiated": True,
        "stream_generation": generation,
        "wire_integrity_passed": True,
        "input_sample_zero_recorded": True,
        "paired_frames": paired_frames,
        "completed_parents": completed_parents,
        "source_end_to_receipt": {
            "availability": availability,
            "sample_count": sample_count,
            "p50_ms": distributions["p50"],
            "p95_ms": distributions["p95"],
            "max_ms": distributions["max"],
            "semantic_boundary_proven": False,
            "actual_audibility_proven": False,
        },
    }


def _load_arm(
    arm_dir: Path,
    *,
    schema: int,
    incremental: bool,
    arm_name: str,
) -> dict[str, Any]:
    summaries = sorted(arm_dir.glob("*_summary.json"))
    if len(summaries) != 1:
        raise ValueError(f"{arm_name}: exactly one capture summary is required")
    summary_path = summaries[0]
    root = _load_json(summary_path, label=arm_name)
    integrity = _object(
        root.get("staged_integrity"),
        field="staged_integrity",
        label=arm_name,
    )
    if (
        integrity.get("applicable") is not True
        or integrity.get("passed") is not True
        or integrity.get("errors") != []
    ):
        raise ValueError(f"{arm_name}: staged integrity did not pass")
    if (
        root.get("pipeline_mode") != "staged"
        or root.get("translation_completed") is not True
        or root.get("input_completed") is not True
        or root.get("connection_lost") is not False
        or root.get("drain_timed_out") is not False
        or root.get("server_error") not in {"", None}
    ):
        raise ValueError(f"{arm_name}: capture did not complete cleanly")

    config = _object(
        root.get("backend_config"),
        field="backend_config",
        label=arm_name,
    )
    config_projection = _backend_provenance(
        config,
        schema=schema,
        incremental=incremental,
        label=arm_name,
    )
    staged = _object(
        root.get("staged_pipeline"),
        field="staged_pipeline",
        label=arm_name,
    )
    if (
        staged.get("telemetry_schema_version") != schema
        or staged.get("tts_subsegmentation_enabled") is not False
        or staged.get("tts_response_chunk_telemetry_enabled") is not True
    ):
        raise ValueError(f"{arm_name}: staged feature flags are inconsistent")
    if (staged.get("tts_incremental_publish_enabled", False) is not incremental):
        raise ValueError(f"{arm_name}: staged incremental flag is inconsistent")
    reported_handoff = staged.get(
        "tts_publisher_handoff_telemetry_enabled"
    )
    if (
        reported_handoff is not None
        and reported_handoff is not incremental
    ):
        raise ValueError(
            f"{arm_name}: staged publisher-handoff flag is inconsistent"
        )
    if (
        staged.get("state") != "closed"
        or staged.get("outcome") != "complete"
        or staged.get("failure") is not None
        or staged.get("cleanup_errors") != []
        or staged.get("incomplete_sequence_ids") != []
    ):
        raise ValueError(f"{arm_name}: staged pipeline did not close cleanly")

    raw_events = _list(staged.get("events"), field="events", label=arm_name)
    events = [
        _object(item, field=f"events[{index}]", label=arm_name)
        for index, item in enumerate(raw_events)
    ]
    upstream, nmt_by_parent, pipeline_start_ms = _upstream_structure(
        events,
        label=arm_name,
    )
    parent_count = len(upstream["segments"])
    if staged.get("segments_emitted") != parent_count:
        raise ValueError(f"{arm_name}: emitted parent count mismatch")
    if staged.get("completed_sequence_ids") != list(range(parent_count)):
        raise ValueError(f"{arm_name}: completed parent IDs mismatch")
    fallback = (
        _fallback_metadata(
            staged,
            config,
            events,
            parent_count=parent_count,
            incremental=incremental,
            label=arm_name,
        )
        if incremental
        else {
            "max_chars": 0,
            "parent_count": 0,
            "parent_sequence_ids": [],
        }
    )

    tts, completed_bytes = _tts_metrics(
        staged,
        events,
        parent_count=parent_count,
        nmt_by_parent=nmt_by_parent,
        pipeline_start_ms=pipeline_start_ms,
        schema=schema,
        incremental=incremental,
        fallback_parent_ids=set(fallback["parent_sequence_ids"]),
        label=arm_name,
    )
    received_bytes = _integer(
        root.get("total_received_bytes"),
        field="total_received_bytes",
        label=arm_name,
        positive=True,
    )
    if sum(completed_bytes.values()) != received_bytes:
        raise ValueError(f"{arm_name}: total TTS bytes do not match capture")
    audio_messages = _integer(
        root.get("audio_responses"),
        field="audio_responses",
        label=arm_name,
        positive=True,
    )
    if audio_messages != tts["websocket_audio_messages"]:
        raise ValueError(f"{arm_name}: WebSocket audio message count mismatch")
    if not incremental and audio_messages != parent_count:
        raise ValueError(f"{arm_name}: atomic message count mismatch")
    if incremental and staged.get("audio_frames_produced") != audio_messages:
        raise ValueError(f"{arm_name}: streaming frame count mismatch")
    audio_metadata = _audio_metadata_observation(
        root,
        incremental=incremental,
        audio_messages=audio_messages,
        parent_count=parent_count,
        label=arm_name,
    )

    sample_rate = config["sampleRate"]
    channels = config["channels"]
    output_seconds = _number(
        root.get("output_duration_sec"),
        field="output_duration_sec",
        label=arm_name,
        positive=True,
    )
    expected_seconds = received_bytes / (sample_rate * channels * 2)
    if not math.isclose(output_seconds, expected_seconds, abs_tol=1e-6):
        raise ValueError(f"{arm_name}: PCM bytes and duration do not reconcile")
    input_seconds = _number(
        root.get("input_duration_sec"),
        field="input_duration_sec",
        label=arm_name,
        positive=True,
    )

    playback, playback_policy = _playback_metrics(
        _load_json(
            arm_dir / "playback_policy_analysis.json",
            label=f"{arm_name}/playback",
        ),
        output_seconds=output_seconds,
        audio_messages=audio_messages,
        expected_csv_name=summary_path.name.removesuffix("_summary.json")
        + "_results.csv",
        label=f"{arm_name}/playback",
    )

    input_reference = root.get("audio_path")
    if not isinstance(input_reference, str) or not input_reference:
        raise ValueError(f"{arm_name}: input reference is missing")
    return {
        "arm": arm_name,
        "telemetry_schema_version": schema,
        "incremental_publication_enabled": incremental,
        "incremental_atomic_fallback_max_chars": fallback["max_chars"],
        "incremental_atomic_fallback_parent_count": fallback["parent_count"],
        "parent_translation_calls": parent_count,
        "audio_messages": audio_messages,
        "input_duration_seconds": input_seconds,
        "output_audio_bytes": received_bytes,
        "output_audio_seconds": output_seconds,
        "output_to_input_duration_ratio": output_seconds / input_seconds,
        "first_translated_audio_seconds": _number(
            root.get("first_audio_latency_sec"),
            field="first_audio_latency_sec",
            label=arm_name,
        ),
        "service_tail_lag_seconds": _number(
            root.get("tail_lag_sec"),
            field="tail_lag_sec",
            label=arm_name,
        ),
        "captured_listener_tail_seconds": _number(
            root.get("playback_tail_sec"),
            field="playback_tail_sec",
            label=arm_name,
        ),
        "audio_metadata_observation": audio_metadata,
        "tts": tts,
        "playback": playback,
        "_match": {
            "input_reference": input_reference,
            "input_duration_seconds": input_seconds,
            "chunks_sent": _integer(
                root.get("chunks_sent"),
                field="chunks_sent",
                label=arm_name,
                positive=True,
            ),
            "backend_config": config_projection,
            "playback_policy": playback_policy,
            "upstream": upstream,
            "parent_audio_bytes": [
                completed_bytes[parent] for parent in range(parent_count)
            ],
        },
    }


def _difference(
    atomic: float,
    streaming: float,
) -> dict[str, float | None]:
    return {
        "streaming_minus_atomic": streaming - atomic,
        "reduction": atomic - streaming,
        "reduction_percent": (
            (atomic - streaming) / atomic * 100.0 if atomic > 0 else None
        ),
    }


def _optional_difference(
    atomic: float | None,
    streaming: float | None,
) -> dict[str, float | None]:
    if atomic is None or streaming is None:
        return {
            "streaming_minus_atomic": None,
            "reduction": None,
            "reduction_percent": None,
        }
    return _difference(atomic, streaming)


def build_canary_summary(input_dir: Path) -> dict[str, Any]:
    """Load both arms, fail closed on matching, and return safe aggregates."""

    run_info = _run_info(input_dir / "run_info.txt")
    prefix_path = input_dir / "shared-prefix.wav"
    try:
        actual_prefix_sha256 = hashlib.sha256(prefix_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("shared source prefix could not be read") from exc
    if actual_prefix_sha256 != run_info["prefix_sha256"]:
        raise ValueError("shared source prefix digest does not match run_info")

    loaded = [
        _load_arm(
            input_dir / arm_name,
            schema=schema,
            incremental=incremental,
            arm_name=arm_name,
        )
        for arm_name, schema, incremental in ARMS
    ]
    atomic, streaming = loaded
    expected_fallback_max_chars = int(
        run_info.get("incremental_atomic_fallback_max_chars", "0")
    )
    if (
        streaming["incremental_atomic_fallback_max_chars"]
        != expected_fallback_max_chars
    ):
        raise ValueError(
            "streaming: fallback threshold does not match run_info"
        )
    atomic_match = atomic["_match"]
    streaming_match = streaming["_match"]
    if atomic_match["input_reference"] != streaming_match["input_reference"]:
        raise ValueError("streaming: input reference does not match atomic")
    if Path(atomic_match["input_reference"]).resolve() != prefix_path.resolve():
        raise ValueError("capture input reference is not the shared prefix")
    for field in ("chunks_sent", "backend_config", "playback_policy", "upstream"):
        if atomic_match[field] != streaming_match[field]:
            raise ValueError(f"streaming: matched {field} evidence differs")
    if not math.isclose(
        atomic_match["input_duration_seconds"],
        streaming_match["input_duration_seconds"],
        abs_tol=MATCH_TOLERANCE_SECONDS,
        rel_tol=0.0,
    ):
        raise ValueError("streaming: input duration does not match atomic")
    atomic_parent_bytes = atomic_match["parent_audio_bytes"]
    streaming_parent_bytes = streaming_match["parent_audio_bytes"]
    if len(atomic_parent_bytes) != len(streaming_parent_bytes):
        raise ValueError("streaming: TTS parent count does not match atomic")
    for arm in loaded:
        arm.pop("_match")

    comparison = {
        "first_translated_audio": _difference(
            atomic["first_translated_audio_seconds"],
            streaming["first_translated_audio_seconds"],
        ),
        "captured_listener_tail": _difference(
            atomic["captured_listener_tail_seconds"],
            streaming["captured_listener_tail_seconds"],
        ),
        "service_tail_lag": _difference(
            atomic["service_tail_lag_seconds"],
            streaming["service_tail_lag_seconds"],
        ),
        "output_audio_bytes": _difference(
            float(atomic["output_audio_bytes"]),
            float(streaming["output_audio_bytes"]),
        ),
        "output_audio_seconds": _difference(
            atomic["output_audio_seconds"],
            streaming["output_audio_seconds"],
        ),
        "output_to_input_duration_ratio": _difference(
            atomic["output_to_input_duration_ratio"],
            streaming["output_to_input_duration_ratio"],
        ),
        "first_websocket_publication_p95": _difference(
            atomic["tts"]["source_boundary_to_first_websocket_seconds"]["p95"],
            streaming["tts"]["source_boundary_to_first_websocket_seconds"]["p95"],
        ),
        "first_response_withheld_p95": _optional_difference(
            (
                atomic["tts"][
                    "first_response_to_first_websocket_seconds"
                ]["p95"]
                if atomic["tts"][
                    "first_response_to_first_websocket_seconds"
                ]
                else None
            ),
            (
                streaming["tts"][
                    "first_response_to_first_websocket_seconds"
                ]["p95"]
                if streaming["tts"][
                    "first_response_to_first_websocket_seconds"
                ]
                else None
            ),
        ),
        "tts_request_to_first_response_p95": _difference(
            atomic["tts"]["request_to_first_response_seconds"]["p95"],
            streaming["tts"]["request_to_first_response_seconds"]["p95"],
        ),
        "tts_request_to_full_response_p95": _difference(
            atomic["tts"]["request_to_full_response_seconds"]["p95"],
            streaming["tts"]["request_to_full_response_seconds"]["p95"],
        ),
        "fixed_queue_p95": _difference(
            atomic["playback"]["fixed_1x"][
                "time_weighted_queue_p95_seconds"
            ],
            streaming["playback"]["fixed_1x"][
                "time_weighted_queue_p95_seconds"
            ],
        ),
        "adaptive_queue_p95": _difference(
            atomic["playback"]["adaptive"][
                "time_weighted_queue_p95_seconds"
            ],
            streaming["playback"]["adaptive"][
                "time_weighted_queue_p95_seconds"
            ],
        ),
        "adaptive_listener_tail": _difference(
            atomic["playback"]["adaptive"]["listener_tail_seconds"],
            streaming["playback"]["adaptive"]["listener_tail_seconds"],
        ),
    }
    byte_difference_percent = (
        abs(
            streaming["output_audio_bytes"]
            - atomic["output_audio_bytes"]
        )
        / atomic["output_audio_bytes"]
        * 100.0
    )
    duration_difference_percent = (
        abs(
            streaming["output_audio_seconds"]
            - atomic["output_audio_seconds"]
        )
        / atomic["output_audio_seconds"]
        * 100.0
    )
    parent_byte_difference_percent = [
        abs(streaming_bytes - atomic_bytes) / atomic_bytes * 100.0
        for atomic_bytes, streaming_bytes in zip(
            atomic_parent_bytes,
            streaming_parent_bytes,
        )
    ]
    parent_byte_difference_distribution = _distribution(
        parent_byte_difference_percent
    )
    materially_different_audio = max(
        byte_difference_percent,
        duration_difference_percent,
        parent_byte_difference_distribution["max"],
    ) > MATERIAL_AUDIO_DIFFERENCE_PERCENT
    playback_status = (
        "inconclusive_confounded_by_translated_audio_difference"
        if materially_different_audio
        else "descriptive_matched_workload_comparison"
    )
    return _round_floats(
        {
            "schema_version": 1,
            "source_format": (
                "validated matched batch captures and playback analyses"
            ),
            "privacy": {
                "contains_transcript_text": False,
                "contains_audio": False,
                "contains_input_paths_or_filenames": False,
                "contains_endpoints": False,
                "contains_session_ids": False,
            },
            "matched_design": {
                "passed": True,
                "shared_prefix_content_hash_matched": True,
                "input_reference_matched": True,
                "input_duration_and_chunks_matched": True,
                "backend_config_and_model_provenance_matched": True,
                "playback_policy_matched": True,
                "asr_final_structure_matched": True,
                "parent_segmentation_matched": True,
                "nmt_parent_structure_matched": True,
                "incremental_atomic_fallback_threshold_matched": True,
                "atomic_legacy_and_streaming_metadata_v1_verified": True,
                "incremental_atomic_fallback_max_chars": (
                    expected_fallback_max_chars
                ),
                "intended_backend_config_differences": list(
                    INTENDED_CONFIG_DIFFERENCES
                ),
            },
            "arms": loaded,
            "comparison": comparison,
            "audio_output_comparability": {
                "material_difference_threshold_percent": (
                    MATERIAL_AUDIO_DIFFERENCE_PERCENT
                ),
                "absolute_byte_difference_percent": byte_difference_percent,
                "absolute_duration_difference_percent": (
                    duration_difference_percent
                ),
                "absolute_parent_byte_difference_percent": (
                    parent_byte_difference_distribution
                ),
                "materially_different": materially_different_audio,
                "exact_cross_arm_byte_equality_required": False,
            },
            "cross_arm_playback_conclusion": {
                "status": playback_status,
                "confounded": materially_different_audio,
                "automatic_latency_conclusion_allowed": False,
                "reason": (
                    "Queue and tail differences combine publication timing "
                    "with materially different total duration or per-parent "
                    "translated-audio bytes."
                    if materially_different_audio
                    else
                    "Queue and tail differences are descriptive; repeated "
                    "runs and listening review remain required."
                ),
            },
            "primary_incremental_evidence": {
                "available": (
                    streaming["tts"]["direct_incremental_parent_count"] > 0
                ),
                "classification": "within_arm_direct_measurement",
                "included_direct_incremental_parent_count": (
                    streaming["tts"]["direct_incremental_parent_count"]
                ),
                "excluded_atomic_fallback_parent_count": streaming["tts"][
                    "excluded_atomic_fallback_parent_count"
                ],
                "unavailable_reason": (
                    None
                    if streaming["tts"][
                        "direct_incremental_parent_count"
                    ]
                    else "all_schema_v3_parents_used_atomic_fallback"
                ),
                "first_response_to_first_websocket_seconds": streaming["tts"][
                    "first_response_to_first_websocket_seconds"
                ],
                "first_websocket_lead_over_full_response_seconds": streaming[
                    "tts"
                ]["first_websocket_lead_over_full_response_seconds"],
                "interpretation": (
                    "Positive full-response lead means schema 3 published "
                    "the first PCM before the TTS RPC completed. Atomic "
                    "fallback parents are excluded."
                ),
            },
            "audience_freshness_observation": {
                **streaming["audio_metadata_observation"][
                    "source_end_to_receipt"
                ],
                "classification": (
                    "same_client_clock_source_offset_to_pcm_receipt"
                ),
                "scheduled_playback_or_actual_audibility_measured": False,
            },
            "interpretation": {
                "positive_reduction_is_better_for_latency_or_queue_metrics": True,
                "audio_byte_and_duration_differences_are_not_quality_proof": True,
                "native_language_listening_review_required": True,
            },
        }
    )


def _seconds(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.3f}s"


def _percent(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1f}%"


def _milliseconds(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.3f}ms"


def render_markdown(summary: dict[str, Any]) -> str:
    """Render a compact comparison without copying identifying evidence."""

    arms = {arm["arm"]: arm for arm in summary["arms"]}
    atomic = arms["atomic"]
    streaming = arms["streaming"]
    comparison = summary["comparison"]
    comparability = summary["audio_output_comparability"]
    playback_conclusion = summary["cross_arm_playback_conclusion"]
    primary = summary["primary_incremental_evidence"]
    freshness = summary["audience_freshness_observation"]
    warning = (
        "Cross-arm playback queue and tail results are **inconclusive**: "
        "translated-audio duration differed materially, so those deltas mix "
        "publication timing with a different playback workload."
        if playback_conclusion["confounded"]
        else
        "Cross-arm playback metrics are descriptive matched-workload results; "
        "they are not an automatic promotion decision."
    )
    if primary["available"]:
        primary_statement = (
            "Direct within-arm schema-3 result: first PCM reached the "
            "WebSocket {delay} after the first TTS response (p95) and "
            "{lead} before the full TTS response completed (p95; positive "
            "means earlier publication). Excluded {excluded} atomic-"
            "fallback parent(s)."
        ).format(
            delay=_seconds(
                primary["first_response_to_first_websocket_seconds"]["p95"]
            ),
            lead=_seconds(
                primary[
                    "first_websocket_lead_over_full_response_seconds"
                ]["p95"]
            ),
            excluded=primary["excluded_atomic_fallback_parent_count"],
        )
    else:
        primary_statement = (
            "Direct within-arm schema-3 benefit is unavailable because all "
            f"{primary['excluded_atomic_fallback_parent_count']} parent(s) "
            "used the short-parent atomic fallback. Audience-facing metrics "
            "still include them."
        )
    lines = [
        "# Matched Incremental TTS Publication Canary",
        "",
        "The source prefix, model provenance, ASR-final structure, parent "
        "segmentation, NMT parent structure, and playback policy matched.",
        "",
        warning,
        "",
        primary_statement,
        "",
        (
            "Metadata-v1 source-end to client PCM receipt: p50 {p50}, "
            "p95 {p95}, max {maximum} across {count} frame(s) "
            "({availability}). This same-client-clock observation is not a "
            "semantic punchline boundary or proof of acoustic audibility."
        ).format(
            p50=_milliseconds(freshness["p50_ms"]),
            p95=_milliseconds(freshness["p95_ms"]),
            maximum=_milliseconds(freshness["max_ms"]),
            count=freshness["sample_count"],
            availability=freshness["availability"],
        ),
        "",
        "| Metric | Atomic | Incremental | Incremental benefit |",
        "|---|---:|---:|---:|",
        "| First translated audio | {a} | {s} | {b} |".format(
            a=_seconds(atomic["first_translated_audio_seconds"]),
            s=_seconds(streaming["first_translated_audio_seconds"]),
            b=_seconds(comparison["first_translated_audio"]["reduction"]),
        ),
        "| Captured listener tail | {a} | {s} | {b} |".format(
            a=_seconds(atomic["captured_listener_tail_seconds"]),
            s=_seconds(streaming["captured_listener_tail_seconds"]),
            b=_seconds(comparison["captured_listener_tail"]["reduction"]),
        ),
        "| Service tail lag | {a} | {s} | {b} |".format(
            a=_seconds(atomic["service_tail_lag_seconds"]),
            s=_seconds(streaming["service_tail_lag_seconds"]),
            b=_seconds(comparison["service_tail_lag"]["reduction"]),
        ),
        "| Source boundary → first WebSocket (p95) | {a} | {s} | {b} |".format(
            a=_seconds(
                atomic["tts"][
                    "source_boundary_to_first_websocket_seconds"
                ]["p95"]
            ),
            s=_seconds(
                streaming["tts"][
                    "source_boundary_to_first_websocket_seconds"
                ]["p95"]
            ),
            b=_seconds(
                comparison["first_websocket_publication_p95"]["reduction"]
            ),
        ),
        "| TTS first response withheld (p95) | {a} | {s} | {b} |".format(
            a=_seconds(
                (
                    atomic["tts"][
                        "first_response_to_first_websocket_seconds"
                    ]
                    or {}
                ).get("p95")
            ),
            s=_seconds(
                (
                    streaming["tts"][
                        "first_response_to_first_websocket_seconds"
                    ]
                    or {}
                ).get("p95")
            ),
            b=_seconds(
                comparison["first_response_withheld_p95"]["reduction"]
            ),
        ),
        "| TTS request → first response (p95) | {a} | {s} | {b} |".format(
            a=_seconds(
                atomic["tts"]["request_to_first_response_seconds"]["p95"]
            ),
            s=_seconds(
                streaming["tts"]["request_to_first_response_seconds"]["p95"]
            ),
            b=_seconds(
                comparison["tts_request_to_first_response_p95"]["reduction"]
            ),
        ),
        "| TTS request → full response (p95) | {a} | {s} | {b} |".format(
            a=_seconds(
                atomic["tts"]["request_to_full_response_seconds"]["p95"]
            ),
            s=_seconds(
                streaming["tts"]["request_to_full_response_seconds"]["p95"]
            ),
            b=_seconds(
                comparison["tts_request_to_full_response_p95"]["reduction"]
            ),
        ),
        "| Adaptive queue p95 | {a} | {s} | {b} |".format(
            a=_seconds(
                atomic["playback"]["adaptive"][
                    "time_weighted_queue_p95_seconds"
                ]
            ),
            s=_seconds(
                streaming["playback"]["adaptive"][
                    "time_weighted_queue_p95_seconds"
                ]
            ),
            b=_seconds(comparison["adaptive_queue_p95"]["reduction"]),
        ),
        "| Adaptive listener tail | {a} | {s} | {b} |".format(
            a=_seconds(
                atomic["playback"]["adaptive"]["listener_tail_seconds"]
            ),
            s=_seconds(
                streaming["playback"]["adaptive"]["listener_tail_seconds"]
            ),
            b=_seconds(comparison["adaptive_listener_tail"]["reduction"]),
        ),
        "",
        "## Audio parity",
        "",
        "- Atomic: {bytes:,} bytes / {seconds}".format(
            bytes=atomic["output_audio_bytes"],
            seconds=_seconds(atomic["output_audio_seconds"]),
        ),
        "- Incremental: {bytes:,} bytes / {seconds}".format(
            bytes=streaming["output_audio_bytes"],
            seconds=_seconds(streaming["output_audio_seconds"]),
        ),
        "- Byte difference: {delta:+.0f} ({percent})".format(
            delta=comparison["output_audio_bytes"]["streaming_minus_atomic"],
            percent=_percent(
                -comparison["output_audio_bytes"]["reduction_percent"]
                if comparison["output_audio_bytes"]["reduction_percent"]
                is not None
                else None
            ),
        ),
        "- Material-difference threshold: {threshold:.1f}%".format(
            threshold=comparability[
                "material_difference_threshold_percent"
            ]
        ),
        "- Maximum corresponding-parent byte difference: {percent}".format(
            percent=_percent(
                comparability[
                    "absolute_parent_byte_difference_percent"
                ]["max"]
            )
        ),
        "- Materially different: {value}".format(
            value="yes" if comparability["materially_different"] else "no"
        ),
        "",
        "Positive benefit values mean lower latency, tail, or queue depth. "
        "Audio-duration similarity is necessary evidence, not a listening-"
        "quality verdict; native-language review remains required.",
        "",
    ]
    return "\n".join(lines)


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = create_argument_parser().parse_args(argv)
    summary = build_canary_summary(args.input_dir)
    json_output = (
        args.json_output
        or args.input_dir / "streaming_tts_canary_comparison.json"
    )
    markdown_output = (
        args.markdown_output
        or args.input_dir / "streaming_tts_canary_comparison.md"
    )
    if json_output.resolve() == markdown_output.resolve():
        raise SystemExit("JSON and Markdown outputs must be different")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(render_markdown(summary), encoding="utf-8")
    print(f"Wrote {json_output}")
    print(f"Wrote {markdown_output}")


if __name__ == "__main__":
    main()
