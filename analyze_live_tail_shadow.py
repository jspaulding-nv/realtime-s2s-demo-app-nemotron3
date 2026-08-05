#!/usr/bin/env python3
"""Fail closed over a browser tail-freshness shadow evidence artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from playback_simulation import (
    ParentAudioFrame,
    PlaybackPolicy,
    simulate_parent_freshness_cap,
)


SCHEMA = "tail-freshness-shadow/v1"
STRATEGY = "truncate_parent_tail"
ABS_TOLERANCE = 1e-6

TOP_LEVEL_KEYS = {
    "schema",
    "strategy",
    "status",
    "evidenceValid",
    "observationOnly",
    "liveAudioChanged",
    "containsPcm",
    "containsTranscriptOrTranslationText",
    "hardCapSeconds",
    "cancellationGuardSeconds",
    "adaptivePlayback",
    "playbackPolicy",
    "summary",
    "parents",
    "decisions",
}
DECISION_KEYS = {
    "streamGeneration",
    "parentSequenceId",
    "audioFrameId",
    "schedulePerformanceMs",
    "audioContextTimeAtScheduleSeconds",
    "audioBytes",
    "sourceDurationSeconds",
    "queueBeforeTruncationSeconds",
    "queueAfterTruncationSeconds",
    "peakQueueAfterTruncationSeconds",
    "playbackRate",
    "playbackMode",
    "truncationTriggered",
    "suppressedArrival",
    "droppedFrameCountThisEvent",
    "droppedSourceDurationThisEventSeconds",
    "residualOverCapSeconds",
}
PARENT_KEYS = {
    "parentSequenceId",
    "frameCount",
    "retainedFrameCount",
    "droppedFrameCount",
    "sourceDurationSeconds",
    "retainedSourceDurationSeconds",
    "droppedSourceDurationSeconds",
    "firstDroppedFrameId",
    "shape",
}
SUMMARY_KEYS = {
    "framesReceived",
    "framesRetained",
    "framesDropped",
    "parentsReceived",
    "parentsTruncated",
    "parentsFullyDropped",
    "totalSourceDurationSeconds",
    "retainedSourceDurationSeconds",
    "droppedSourceDurationSeconds",
    "retainedSourcePercent",
    "lastDecisionQueueSeconds",
    "peakQueueBeforeTruncationSeconds",
    "peakQueueAfterTruncationSeconds",
    "truncationTriggerCount",
    "suppressedArrivalCount",
    "residualBreachEvents",
    "peakResidualOverCapSeconds",
    "maxDroppedParentSuffixSeconds",
    "hardCapAchieved",
    "singleTailContractHolds",
}
POLICY_KEYS = {
    "targetQueueSeconds",
    "urgentQueueSeconds",
    "limitQueueSeconds",
    "catchUpReleaseSeconds",
    "urgentReleaseSeconds",
    "normalRate",
    "catchUpRate",
    "urgentRate",
}


class ShadowEvidenceError(ValueError):
    """Raised when browser shadow evidence is incomplete or inconsistent."""


def _record(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ShadowEvidenceError(f"{context} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, Any], expected: set[str], context: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ShadowEvidenceError(
            f"{context} fields differ; missing={missing}, extra={extra}"
        )


def _number(
    value: Any,
    context: str,
    *,
    minimum: float = 0.0,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShadowEvidenceError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ShadowEvidenceError(f"{context} must be finite")
    if result < minimum or (positive and result <= minimum):
        qualifier = "positive" if positive else f">= {minimum}"
        raise ShadowEvidenceError(f"{context} must be {qualifier}")
    return result


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ShadowEvidenceError(
            f"{context} must be an integer >= {minimum}"
        )
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ShadowEvidenceError(f"{context} must be boolean")
    return value


def _close(left: float, right: float, context: str) -> None:
    if not math.isclose(left, right, rel_tol=1e-9, abs_tol=ABS_TOLERANCE):
        raise ShadowEvidenceError(
            f"{context} differs: observed {left}, replayed {right}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _policy(value: Any) -> PlaybackPolicy:
    item = _record(value, "playbackPolicy")
    _exact_keys(item, POLICY_KEYS, "playbackPolicy")
    return PlaybackPolicy(
        target_queue_seconds=_number(
            item["targetQueueSeconds"], "playbackPolicy.targetQueueSeconds"
        ),
        urgent_queue_seconds=_number(
            item["urgentQueueSeconds"], "playbackPolicy.urgentQueueSeconds"
        ),
        limit_queue_seconds=_number(
            item["limitQueueSeconds"], "playbackPolicy.limitQueueSeconds"
        ),
        catch_up_release_seconds=_number(
            item["catchUpReleaseSeconds"],
            "playbackPolicy.catchUpReleaseSeconds",
        ),
        urgent_release_seconds=_number(
            item["urgentReleaseSeconds"],
            "playbackPolicy.urgentReleaseSeconds",
        ),
        normal_rate=_number(
            item["normalRate"], "playbackPolicy.normalRate", positive=True
        ),
        catch_up_rate=_number(
            item["catchUpRate"], "playbackPolicy.catchUpRate", positive=True
        ),
        urgent_rate=_number(
            item["urgentRate"], "playbackPolicy.urgentRate", positive=True
        ),
    )


def _validate_header(evidence: Mapping[str, Any]) -> tuple[float, float, bool]:
    _exact_keys(evidence, TOP_LEVEL_KEYS, "evidence")
    expected_literals = {
        "schema": SCHEMA,
        "strategy": STRATEGY,
        "status": "complete",
        "evidenceValid": True,
        "observationOnly": True,
        "liveAudioChanged": False,
        "containsPcm": False,
        "containsTranscriptOrTranslationText": False,
    }
    for field, expected in expected_literals.items():
        if evidence[field] != expected:
            raise ShadowEvidenceError(f"{field} must equal {expected!r}")
    hard_cap = _number(
        evidence["hardCapSeconds"], "hardCapSeconds", positive=True
    )
    guard = _number(
        evidence["cancellationGuardSeconds"],
        "cancellationGuardSeconds",
    )
    adaptive = _boolean(evidence["adaptivePlayback"], "adaptivePlayback")
    return hard_cap, guard, adaptive


def _validate_parents(value: Any) -> tuple[list[Mapping[str, Any]], dict[int, int]]:
    if not isinstance(value, list) or not value:
        raise ShadowEvidenceError("parents must be a non-empty array")
    parents: list[Mapping[str, Any]] = []
    frame_counts: dict[int, int] = {}
    for index, raw in enumerate(value):
        item = _record(raw, f"parents[{index}]")
        _exact_keys(item, PARENT_KEYS, f"parents[{index}]")
        parent_id = _integer(
            item["parentSequenceId"], f"parents[{index}].parentSequenceId"
        )
        if parent_id != index:
            raise ShadowEvidenceError(
                "parentSequenceId values must be contiguous and zero-based"
            )
        frame_count = _integer(
            item["frameCount"], f"parents[{index}].frameCount", minimum=1
        )
        retained = _integer(
            item["retainedFrameCount"],
            f"parents[{index}].retainedFrameCount",
        )
        dropped = _integer(
            item["droppedFrameCount"],
            f"parents[{index}].droppedFrameCount",
        )
        if retained + dropped != frame_count:
            raise ShadowEvidenceError(
                f"parents[{index}] frame accounting does not reconcile"
            )
        total_duration = _number(
            item["sourceDurationSeconds"],
            f"parents[{index}].sourceDurationSeconds",
            positive=True,
        )
        retained_duration = _number(
            item["retainedSourceDurationSeconds"],
            f"parents[{index}].retainedSourceDurationSeconds",
        )
        dropped_duration = _number(
            item["droppedSourceDurationSeconds"],
            f"parents[{index}].droppedSourceDurationSeconds",
        )
        _close(
            retained_duration + dropped_duration,
            total_duration,
            f"parents[{index}] duration accounting",
        )
        shape = item["shape"]
        expected_shape = (
            "untouched"
            if dropped == 0
            else "fully_dropped"
            if retained == 0
            else "partial_suffix"
        )
        if shape != expected_shape:
            raise ShadowEvidenceError(f"parents[{index}].shape is inconsistent")
        first_dropped = item["firstDroppedFrameId"]
        expected_first = None if dropped == 0 else retained
        if first_dropped != expected_first:
            raise ShadowEvidenceError(
                f"parents[{index}].firstDroppedFrameId is not one suffix"
            )
        parents.append(item)
        frame_counts[parent_id] = frame_count
    return parents, frame_counts


def _validate_decisions(
    value: Any, frame_counts: Mapping[int, int]
) -> tuple[list[Mapping[str, Any]], list[ParentAudioFrame]]:
    if not isinstance(value, list) or not value:
        raise ShadowEvidenceError("decisions must be a non-empty array")
    decisions: list[Mapping[str, Any]] = []
    frames: list[ParentAudioFrame] = []
    expected_parent = 0
    expected_frame = 0
    generation: int | None = None
    previous_context = -math.inf
    for index, raw in enumerate(value):
        item = _record(raw, f"decisions[{index}]")
        _exact_keys(item, DECISION_KEYS, f"decisions[{index}]")
        current_generation = _integer(
            item["streamGeneration"],
            f"decisions[{index}].streamGeneration",
            minimum=1,
        )
        if generation is None:
            generation = current_generation
        elif generation != current_generation:
            raise ShadowEvidenceError("streamGeneration changed inside evidence")
        parent_id = _integer(
            item["parentSequenceId"],
            f"decisions[{index}].parentSequenceId",
        )
        frame_id = _integer(
            item["audioFrameId"], f"decisions[{index}].audioFrameId"
        )
        if parent_id != expected_parent or frame_id != expected_frame:
            raise ShadowEvidenceError(
                "decision parent/frame identities are not in wire order"
            )
        parent_frame_count = frame_counts.get(parent_id)
        if parent_frame_count is None:
            raise ShadowEvidenceError("decision refers to an unknown parent")
        expected_frame += 1
        if expected_frame == parent_frame_count:
            expected_parent += 1
            expected_frame = 0
        at_seconds = _number(
            item["audioContextTimeAtScheduleSeconds"],
            f"decisions[{index}].audioContextTimeAtScheduleSeconds",
        )
        if at_seconds + ABS_TOLERANCE < previous_context:
            raise ShadowEvidenceError("AudioContext decision clock moved backward")
        previous_context = at_seconds
        _number(
            item["schedulePerformanceMs"],
            f"decisions[{index}].schedulePerformanceMs",
        )
        audio_bytes = _integer(
            item["audioBytes"], f"decisions[{index}].audioBytes", minimum=1
        )
        duration = _number(
            item["sourceDurationSeconds"],
            f"decisions[{index}].sourceDurationSeconds",
            positive=True,
        )
        for field in (
            "queueBeforeTruncationSeconds",
            "queueAfterTruncationSeconds",
            "peakQueueAfterTruncationSeconds",
            "droppedSourceDurationThisEventSeconds",
            "residualOverCapSeconds",
        ):
            _number(item[field], f"decisions[{index}].{field}")
        _number(
            item["playbackRate"],
            f"decisions[{index}].playbackRate",
            positive=True,
        )
        if item["playbackMode"] not in {
            "normal", "catch-up", "urgent", "over-limit"
        }:
            raise ShadowEvidenceError(
                f"decisions[{index}].playbackMode is invalid"
            )
        _boolean(
            item["truncationTriggered"],
            f"decisions[{index}].truncationTriggered",
        )
        _boolean(
            item["suppressedArrival"],
            f"decisions[{index}].suppressedArrival",
        )
        _integer(
            item["droppedFrameCountThisEvent"],
            f"decisions[{index}].droppedFrameCountThisEvent",
        )
        frames.append(
            ParentAudioFrame(
                arrival_seconds=at_seconds,
                duration_seconds=duration,
                parent_sequence_id=parent_id,
                audio_frame_id=frame_id,
                parent_frame_count=parent_frame_count,
                audio_bytes=audio_bytes,
                source_index=index,
            )
        )
        decisions.append(item)
    if expected_parent != len(frame_counts) or expected_frame != 0:
        raise ShadowEvidenceError("decision ledger does not complete every parent")
    return decisions, frames


def _validate_summary(value: Any) -> Mapping[str, Any]:
    summary = _record(value, "summary")
    _exact_keys(summary, SUMMARY_KEYS, "summary")
    integer_fields = {
        "framesReceived",
        "framesRetained",
        "framesDropped",
        "parentsReceived",
        "parentsTruncated",
        "parentsFullyDropped",
        "truncationTriggerCount",
        "suppressedArrivalCount",
        "residualBreachEvents",
    }
    boolean_fields = {"hardCapAchieved", "singleTailContractHolds"}
    for field in SUMMARY_KEYS:
        if field in integer_fields:
            _integer(summary[field], f"summary.{field}")
        elif field in boolean_fields:
            _boolean(summary[field], f"summary.{field}")
        else:
            _number(summary[field], f"summary.{field}")
    if not summary["singleTailContractHolds"]:
        raise ShadowEvidenceError("singleTailContractHolds must be true")
    return summary


def analyze_evidence(path: Path) -> dict[str, Any]:
    try:
        evidence = _record(
            json.loads(path.read_text(encoding="utf-8")), "evidence"
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowEvidenceError(f"could not read evidence: {exc}") from exc
    hard_cap, guard, adaptive = _validate_header(evidence)
    policy = _policy(evidence["playbackPolicy"])
    if not math.isclose(policy.limit_queue_seconds, hard_cap, abs_tol=1e-9):
        raise ShadowEvidenceError(
            "hardCapSeconds must match playbackPolicy.limitQueueSeconds"
        )
    parents, frame_counts = _validate_parents(evidence["parents"])
    decisions, frames = _validate_decisions(
        evidence["decisions"], frame_counts
    )
    summary = _validate_summary(evidence["summary"])

    replay = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=frames[-1].arrival_seconds,
        hard_cap_seconds=hard_cap,
        strategy=STRATEGY,
        cancellation_guard_seconds=guard,
        adaptive=adaptive,
        policy=policy,
    )
    eviction_by_source = {
        item.trigger_source_index: item for item in replay.evictions
    }
    running_peak_after = 0.0
    suppressed_parents: set[int] = set()
    for index, (observed, expected) in enumerate(
        zip(decisions, replay.decisions, strict=True)
    ):
        frame = frames[index]
        expected_suppressed = frame.parent_sequence_id in suppressed_parents
        if observed["suppressedArrival"] is not expected_suppressed:
            raise ShadowEvidenceError(
                f"decisions[{index}].suppressedArrival differs from replay"
            )
        expected_trigger = (
            not expected_suppressed
            and expected.queue_before_eviction_seconds
            > hard_cap + 1e-9
        )
        if observed["truncationTriggered"] is not expected_trigger:
            raise ShadowEvidenceError(
                f"decisions[{index}].truncationTriggered differs from replay"
            )
        _close(
            _number(
                observed["queueBeforeTruncationSeconds"],
                f"decisions[{index}].queueBeforeTruncationSeconds",
            ),
            expected.queue_before_eviction_seconds,
            f"decisions[{index}] queue before truncation",
        )
        _close(
            _number(
                observed["queueAfterTruncationSeconds"],
                f"decisions[{index}].queueAfterTruncationSeconds",
            ),
            expected.queue_after_eviction_seconds,
            f"decisions[{index}] queue after truncation",
        )
        running_peak_after = max(
            running_peak_after, expected.queue_after_eviction_seconds
        )
        _close(
            _number(
                observed["peakQueueAfterTruncationSeconds"],
                f"decisions[{index}].peakQueueAfterTruncationSeconds",
            ),
            running_peak_after,
            f"decisions[{index}] running queue peak",
        )
        _close(
            _number(
                observed["residualOverCapSeconds"],
                f"decisions[{index}].residualOverCapSeconds",
            ),
            expected.residual_over_cap_seconds,
            f"decisions[{index}] residual over cap",
        )
        eviction = eviction_by_source.get(index)
        observed_drop_count = _integer(
            observed["droppedFrameCountThisEvent"],
            f"decisions[{index}].droppedFrameCountThisEvent",
        )
        observed_drop_duration = _number(
            observed["droppedSourceDurationThisEventSeconds"],
            f"decisions[{index}].droppedSourceDurationThisEventSeconds",
        )
        expected_drop_count = 0 if eviction is None else eviction.dropped_frame_count
        expected_drop_duration = (
            0.0 if eviction is None else eviction.dropped_source_duration_seconds
        )
        if observed_drop_count != expected_drop_count:
            raise ShadowEvidenceError(
                f"decisions[{index}] dropped frame count differs from replay"
            )
        _close(
            observed_drop_duration,
            expected_drop_duration,
            f"decisions[{index}] dropped duration",
        )
        if (
            frame.parent_sequence_id in expected.dropped_parent_sequence_ids
            and frame.audio_frame_id + 1 < frame.parent_frame_count
        ):
            suppressed_parents.add(frame.parent_sequence_id)
        if frame.audio_frame_id + 1 == frame.parent_frame_count:
            suppressed_parents.discard(frame.parent_sequence_id)

    replay_summary = replay.summary
    expected_integer_summary = {
        "framesReceived": replay_summary.frames_received,
        "framesRetained": replay_summary.frames_retained,
        "framesDropped": replay_summary.frames_dropped,
        "parentsReceived": replay_summary.parents_received,
        "parentsTruncated": replay_summary.parents_partially_dropped,
        "parentsFullyDropped": replay_summary.parents_dropped,
        "residualBreachEvents": replay_summary.residual_breach_events,
    }
    for field, expected in expected_integer_summary.items():
        if summary[field] != expected:
            raise ShadowEvidenceError(f"summary.{field} differs from replay")
    expected_float_summary = {
        "totalSourceDurationSeconds": replay_summary.total_source_duration_seconds,
        "retainedSourceDurationSeconds": (
            replay_summary.retained_source_duration_seconds
        ),
        "droppedSourceDurationSeconds": replay_summary.dropped_source_duration_seconds,
        "retainedSourcePercent": replay_summary.retained_source_percent,
        "lastDecisionQueueSeconds": (
            replay.decisions[-1].queue_after_eviction_seconds
        ),
        "peakQueueBeforeTruncationSeconds": (
            replay_summary.peak_queue_before_eviction_seconds
        ),
        "peakQueueAfterTruncationSeconds": replay_summary.peak_queue_depth_seconds,
        "peakResidualOverCapSeconds": (
            replay_summary.peak_residual_over_cap_seconds
        ),
    }
    for field, expected in expected_float_summary.items():
        _close(
            _number(summary[field], f"summary.{field}"),
            expected,
            f"summary.{field}",
        )
    if summary["hardCapAchieved"] != replay_summary.hard_cap_achieved:
        raise ShadowEvidenceError("summary.hardCapAchieved differs from replay")
    if summary["truncationTriggerCount"] != sum(
        bool(item["truncationTriggered"]) for item in decisions
    ):
        raise ShadowEvidenceError("summary.truncationTriggerCount is inconsistent")
    if summary["suppressedArrivalCount"] != sum(
        bool(item["suppressedArrival"]) for item in decisions
    ):
        raise ShadowEvidenceError("summary.suppressedArrivalCount is inconsistent")
    _close(
        _number(
            summary["maxDroppedParentSuffixSeconds"],
            "summary.maxDroppedParentSuffixSeconds",
        ),
        max(
            float(parent["droppedSourceDurationSeconds"])
            for parent in parents
        ),
        "summary.maxDroppedParentSuffixSeconds",
    )

    dropped_keys = {
        (frame.parent_sequence_id, frame.audio_frame_id)
        for frame in replay.dropped_frames
    }
    for parent in parents:
        parent_id = int(parent["parentSequenceId"])
        frame_count = int(parent["frameCount"])
        dropped_ids = [
            frame_id
            for frame_id in range(frame_count)
            if (parent_id, frame_id) in dropped_keys
        ]
        observed_first = parent["firstDroppedFrameId"]
        expected_first = dropped_ids[0] if dropped_ids else None
        if observed_first != expected_first:
            raise ShadowEvidenceError(
                f"parent {parent_id} dropped suffix differs from replay"
            )
        if int(parent["droppedFrameCount"]) != len(dropped_ids):
            raise ShadowEvidenceError(
                f"parent {parent_id} dropped frame count differs from replay"
            )

    return {
        "schema_version": 1,
        "analysis_type": "live_tail_freshness_shadow_validation",
        "valid": True,
        "source_sha256": _sha256(path),
        "configuration": {
            "strategy": STRATEGY,
            "hard_cap_seconds": hard_cap,
            "cancellation_guard_seconds": guard,
            "adaptive_playback": adaptive,
            "observation_only": True,
            "live_audio_changed": False,
        },
        "result": {
            "frames_received": replay_summary.frames_received,
            "frames_retained": replay_summary.frames_retained,
            "parents_received": replay_summary.parents_received,
            "frames_dropped": replay_summary.frames_dropped,
            "parents_truncated": replay_summary.parents_partially_dropped,
            "parents_fully_dropped": replay_summary.parents_dropped,
            "total_source_duration_seconds": (
                replay_summary.total_source_duration_seconds
            ),
            "retained_source_duration_seconds": (
                replay_summary.retained_source_duration_seconds
            ),
            "dropped_source_duration_seconds": (
                replay_summary.dropped_source_duration_seconds
            ),
            "retained_source_percent": replay_summary.retained_source_percent,
            "truncation_trigger_count": summary["truncationTriggerCount"],
            "suppressed_arrival_count": summary["suppressedArrivalCount"],
            "max_dropped_parent_suffix_seconds": summary[
                "maxDroppedParentSuffixSeconds"
            ],
            "last_decision_queue_seconds": summary[
                "lastDecisionQueueSeconds"
            ],
            "peak_queue_before_truncation_seconds": (
                replay_summary.peak_queue_before_eviction_seconds
            ),
            "peak_queue_after_truncation_seconds": (
                replay_summary.peak_queue_depth_seconds
            ),
            "residual_breach_events": replay_summary.residual_breach_events,
            "peak_residual_over_cap_seconds": (
                replay_summary.peak_residual_over_cap_seconds
            ),
            "hard_cap_achieved": replay_summary.hard_cap_achieved,
            "single_tail_contract_holds": True,
        },
        "validation": {
            "schema_and_privacy_contract": True,
            "frame_and_parent_accounting": True,
            "independent_python_replay_matches": True,
            "single_tail_loss_shape": True,
        },
        "privacy": {
            "contains_pcm": False,
            "contains_transcript_or_translation_text": False,
            "contains_input_path_or_filename": False,
            "contains_endpoint_or_session_identifier": False,
            "contains_wall_clock_timestamp": False,
        },
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    config = report["configuration"]
    result = report["result"]
    return "\n".join(
        [
            "# Live tail-freshness shadow validation",
            "",
            "> Observation-only numeric evidence; live audio was unchanged.",
            "",
            f"- Valid: **{'yes' if report['valid'] else 'no'}**",
            f"- Strategy: `{config['strategy']}`",
            f"- Queue cap: {config['hard_cap_seconds']:.3f} s",
            f"- Frames / parents: {result['frames_received']:,} / {result['parents_received']}",
            f"- Audio retained: {result['retained_source_percent']:.2f}%",
            f"- Audio removed: {result['dropped_source_duration_seconds']:.3f} s",
            f"- Parents truncated / fully dropped: {result['parents_truncated']} / {result['parents_fully_dropped']}",
            f"- Longest removed suffix: {result['max_dropped_parent_suffix_seconds']:.3f} s",
            f"- Peak projected queue: {result['peak_queue_after_truncation_seconds']:.3f} s",
            f"- Residual cap breaches: {result['residual_breach_events']}",
            f"- Hard cap achieved: {'yes' if result['hard_cap_achieved'] else 'no'}",
            f"- Single-tail contract: {'pass' if result['single_tail_contract_holds'] else 'fail'}",
            "- Independent Python replay: match",
            "",
        ]
    )


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and independently replay live tail shadow evidence"
    )
    parser.add_argument("--evidence-json", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = analyze_evidence(args.evidence_json)
    except ShadowEvidenceError as exc:
        raise SystemExit(f"invalid tail shadow evidence: {exc}") from exc
    stem = args.evidence_json.with_suffix("")
    json_output = args.json_output or stem.with_name(f"{stem.name}.analysis.json")
    markdown_output = args.markdown_output or stem.with_name(
        f"{stem.name}.analysis.md"
    )
    if json_output.resolve() == markdown_output.resolve():
        raise SystemExit("JSON and Markdown output paths must differ")
    _write_private(json_output, json.dumps(report, indent=2) + "\n")
    _write_private(markdown_output, render_markdown(report))
    print(f"Wrote {json_output}")
    print(f"Wrote {markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
