#!/usr/bin/env python3
"""Quantify frame-boundary loss required to enforce a playback freshness cap."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

from freshness_trace import (
    PUBLIC_SUMMARY_LABEL,
    PUBLIC_TRACE_LABEL,
    ParentFreshnessTrace,
    load_parent_freshness_trace,
)
from playback_simulation import simulate_parent_freshness_cap


DEFAULT_CAPS_SECONDS = (5.0, 8.0, 10.0)
DEFAULT_GUARD_SECONDS = 0.1
DEFAULT_GUARD_SENSITIVITY_SECONDS = (0.0, 0.05, 0.1, 0.25)


def _round(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: _round(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(item, digits) for item in value]
    return value


def _normalize_positive(values: Sequence[float]) -> tuple[float, ...]:
    result: list[float] = []
    for raw in values:
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("freshness caps must be finite and positive")
        if value not in result:
            result.append(value)
    if not result:
        raise ValueError("at least one freshness cap is required")
    return tuple(result)


def _normalize_nonnegative(values: Sequence[float]) -> tuple[float, ...]:
    result: list[float] = []
    for raw in values:
        if isinstance(raw, bool):
            raise ValueError("cancellation guards must be numeric")
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError("cancellation guards must be finite and non-negative")
        if value not in result:
            result.append(value)
    if not result:
        raise ValueError("at least one cancellation guard is required")
    return tuple(result)


def _drop_shapes(trace: ParentFreshnessTrace, dropped_keys: set[tuple[int, int]]) -> dict[str, Any]:
    frames_by_parent: dict[int, list[Any]] = {}
    for frame in trace.frames:
        frames_by_parent.setdefault(frame.parent_sequence_id, []).append(frame)

    shape_counts = {
        "fully_dropped": 0,
        "partial_prefix": 0,
        "partial_suffix": 0,
        "partial_internal_contiguous": 0,
        "partial_fragmented": 0,
        "untouched": 0,
    }
    partial_rows = []
    for parent_id, frames in frames_by_parent.items():
        dropped = [
            frame for frame in frames
            if (parent_id, frame.audio_frame_id) in dropped_keys
        ]
        ids = [frame.audio_frame_id for frame in dropped]
        if not dropped:
            shape_counts["untouched"] += 1
            continue
        if len(dropped) == len(frames):
            shape_counts["fully_dropped"] += 1
            continue
        contiguous = ids == list(range(ids[0], ids[-1] + 1))
        if ids == list(range(0, len(ids))):
            shape = "partial_prefix"
        elif ids == list(range(len(frames) - len(ids), len(frames))):
            shape = "partial_suffix"
        elif contiguous:
            shape = "partial_internal_contiguous"
        else:
            shape = "partial_fragmented"
        shape_counts[shape] += 1
        partial_rows.append(
            {
                "parent_sequence_id": parent_id,
                "parent_frame_count": len(frames),
                "dropped_frame_count": len(dropped),
                "first_dropped_frame_id": ids[0],
                "last_dropped_frame_id": ids[-1],
                "dropped_source_duration_seconds": sum(
                    frame.duration_seconds for frame in dropped
                ),
                "shape": shape,
            }
        )

    run_count = 0
    max_run_frames = 0
    max_run_seconds = 0.0
    current_frames = 0
    current_seconds = 0.0
    for frame in trace.frames:
        if (frame.parent_sequence_id, frame.audio_frame_id) in dropped_keys:
            if current_frames == 0:
                run_count += 1
            current_frames += 1
            current_seconds += frame.duration_seconds
            max_run_frames = max(max_run_frames, current_frames)
            max_run_seconds = max(max_run_seconds, current_seconds)
        else:
            current_frames = 0
            current_seconds = 0.0
    return _round(
        {
            "parent_shape_counts": shape_counts,
            "partial_parents": partial_rows,
            "dropped_frame_run_count": run_count,
            "max_consecutive_dropped_frames": max_run_frames,
            "max_dropped_source_duration_per_run_seconds": max_run_seconds,
        }
    )


def _scenario(
    trace: ParentFreshnessTrace,
    *,
    hard_cap_seconds: float,
    cancellation_guard_seconds: float,
    strategy: str,
) -> dict[str, Any]:
    simulation = simulate_parent_freshness_cap(
        trace.frames,
        input_end_seconds=trace.input_end_seconds,
        hard_cap_seconds=hard_cap_seconds,
        strategy=strategy,
        cancellation_guard_seconds=cancellation_guard_seconds,
        adaptive=True,
    )
    summary = _round(asdict(simulation.summary))
    dropped_keys = {
        (frame.parent_sequence_id, frame.audio_frame_id)
        for frame in simulation.dropped_frames
    }
    drop_shapes = _drop_shapes(trace, dropped_keys)
    shape_counts = drop_shapes["parent_shape_counts"]
    single_tail_contract_holds = (
        strategy != "truncate_parent_tail"
        or (
            shape_counts["partial_prefix"] == 0
            and shape_counts["partial_internal_contiguous"] == 0
            and shape_counts["partial_fragmented"] == 0
        )
    )
    return {
        "hard_cap_seconds": hard_cap_seconds,
        "cancellation_guard_seconds": cancellation_guard_seconds,
        "summary": summary,
        "drop_shapes": drop_shapes,
        "invariants": {
            "frame_accounting_reconciles": (
                summary["frames_retained"] + summary["frames_dropped"]
                == summary["frames_received"]
            ),
            "byte_accounting_reconciles": (
                summary["retained_audio_bytes"] + summary["dropped_audio_bytes"]
                == summary["total_audio_bytes"]
            ),
            "partial_parent_drop_is_possible": True,
            "single_tail_contract_holds": single_tail_contract_holds,
            "live_audio_changed": False,
        },
    }


def build_analysis(
    trace: ParentFreshnessTrace,
    *,
    caps_seconds: Sequence[float] = DEFAULT_CAPS_SECONDS,
    cancellation_guard_seconds: float = DEFAULT_GUARD_SECONDS,
    guard_sensitivity_seconds: Sequence[float] = DEFAULT_GUARD_SENSITIVITY_SECONDS,
    strategy: str = "oldest_frame_first",
) -> dict[str, Any]:
    if strategy not in {"oldest_frame_first", "truncate_parent_tail"}:
        raise ValueError("unsupported frame-cap strategy")
    caps = _normalize_positive(caps_seconds)
    guard = _normalize_nonnegative([cancellation_guard_seconds])[0]
    guards = _normalize_nonnegative(guard_sensitivity_seconds)
    scenarios = [
        _scenario(
            trace,
            hard_cap_seconds=cap,
            cancellation_guard_seconds=guard,
            strategy=strategy,
        )
        for cap in caps
    ]
    return _round(
        {
            "schema_version": 1,
            "analysis_type": (
                "schema3_frame_boundary_freshness_cap"
                if strategy == "oldest_frame_first"
                else "schema3_parent_tail_truncation_freshness_cap"
            ),
            "source": {
                "trace_csv": PUBLIC_TRACE_LABEL,
                "summary_json": PUBLIC_SUMMARY_LABEL,
                "trace_sha256": trace.trace_sha256,
                "summary_sha256": trace.summary_sha256,
            },
            "semantics": {
                "offline_counterfactual_only": True,
                "live_audio_changed": False,
                "intentionally_lossy": True,
                "strategy": strategy,
                "eviction_unit": (
                    "not_yet_audible_schema3_pcm_frame"
                    if strategy == "oldest_frame_first"
                    else "single_not_yet_audible_parent_suffix"
                ),
                "partial_parent_and_mid_speech_loss_allowed": (
                    strategy == "oldest_frame_first"
                ),
                "single_suffix_loss_allowed": (
                    strategy == "truncate_parent_tail"
                ),
                "future_frames_suppressed_through_parent_completion": (
                    strategy == "truncate_parent_tail"
                ),
                "fade_or_boundary_repair_modeled": False,
                "semantic_quality_proven": False,
                "deployment_candidate": False,
                "queue_cap_scope": "browser_scheduled_translated_audio_only",
            },
            "captured_trace": {
                "frames": len(trace.frames),
                "parents": len(
                    {frame.parent_sequence_id for frame in trace.frames}
                ),
                "translated_audio_seconds": sum(
                    frame.duration_seconds for frame in trace.frames
                ),
                "input_end_seconds": trace.input_end_seconds,
            },
            "scenarios": scenarios,
            "guard_sensitivity": {
                "hard_cap_seconds": max(caps),
                "scenarios": [
                    _scenario(
                        trace,
                        hard_cap_seconds=max(caps),
                        cancellation_guard_seconds=item,
                        strategy=strategy,
                    )
                    for item in guards
                ],
            },
            "privacy": {
                "contains_pcm": False,
                "contains_transcript_or_translation_text": False,
                "contains_input_path_or_filename": False,
                "contains_endpoint_or_session_identifier": False,
                "contains_wall_clock_timestamp": False,
                "contains_numeric_parent_and_frame_ids": True,
            },
        }
    )


def render_markdown(analysis: dict[str, Any]) -> str:
    trace = analysis["captured_trace"]
    strategy = analysis["semantics"]["strategy"]
    is_tail = strategy == "truncate_parent_tail"
    lines = [
        (
            "# Schema-3 parent-tail truncation counterfactual"
            if is_tail
            else "# Schema-3 frame-boundary freshness-cap counterfactual"
        ),
        "",
        (
            "> Destructive offline model only: no live playback changed. "
            + (
                "This policy retains one prefix and suppresses the remaining "
                "suffix of an overloaded parent."
                if is_tail
                else "This policy can cut translated speech inside a parent."
            )
            + " It has no listening-quality approval."
        ),
        "",
        (
            f"Validated trace: {trace['frames']:,} frames, {trace['parents']} "
            f"parents, {trace['translated_audio_seconds']:.3f}s translated audio."
        ),
        "",
        "| Cap | Hard cap achieved | Audio retained | Frames dropped | Full parents dropped | Partial parents | Queue p95 | Peak queue | Tail |",
        "|---:|:---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario in analysis["scenarios"]:
        summary = scenario["summary"]
        lines.append(
            "| {cap:.0f}s | {achieved} | {retained:.2f}% | {frames} | {full} | {partial} | {p95:.3f}s | {peak:.3f}s | {tail:.3f}s |".format(
                cap=scenario["hard_cap_seconds"],
                achieved="yes" if summary["hard_cap_achieved"] else "**no**",
                retained=summary["retained_source_percent"],
                frames=summary["frames_dropped"],
                full=summary["parents_dropped"],
                partial=summary["parents_partially_dropped"],
                p95=summary["time_weighted_queue_p95_seconds"],
                peak=summary["peak_queue_depth_seconds"],
                tail=summary["listener_tail_seconds"],
            )
        )
    primary = analysis["scenarios"][-1]
    shapes = primary["drop_shapes"]["parent_shape_counts"]
    lines.extend(
        [
            "",
            "## Largest-cap damage shape",
            "",
            (
                f"At {primary['hard_cap_seconds']:.0f}s, the counterfactual fully "
                f"dropped {shapes['fully_dropped']} parents and partially cut "
                f"{primary['summary']['parents_partially_dropped']} parents: "
                f"{shapes['partial_prefix']} prefixes, "
                f"{shapes['partial_suffix']} suffixes, "
                f"{shapes['partial_internal_contiguous']} internal contiguous "
                f"gaps, and {shapes['partial_fragmented']} fragmented gaps."
            ),
            "",
            (
                "A hard-cap pass here establishes technical queue capacity "
                "only. "
                + (
                    "Any internal or fragmented gaps violate the single-tail "
                    "contract. Even clean suffix loss requires an explicit "
                    "fade/restart design before live use."
                    if is_tail
                    else "Internal or fragmented frame loss is strong evidence "
                    "against deployment without an explicit fade/restart design."
                )
            ),
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate destructive frame-boundary freshness caps"
    )
    parser.add_argument("--results-csv", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--freshness-cap", type=float, action="append")
    parser.add_argument(
        "--strategy",
        choices=("oldest_frame_first", "truncate_parent_tail"),
        default="oldest_frame_first",
    )
    parser.add_argument(
        "--cancellation-guard-ms",
        type=float,
        default=DEFAULT_GUARD_SECONDS * 1_000,
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    trace = load_parent_freshness_trace(args.results_csv, args.summary_json)
    try:
        analysis = build_analysis(
            trace,
            caps_seconds=(
                args.freshness_cap
                if args.freshness_cap is not None
                else DEFAULT_CAPS_SECONDS
            ),
            cancellation_guard_seconds=args.cancellation_guard_ms / 1_000,
            strategy=args.strategy,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    output_dir = args.results_csv.parent
    default_stem = (
        "schema3_parent_tail_cap_analysis"
        if args.strategy == "truncate_parent_tail"
        else "schema3_frame_cap_analysis"
    )
    json_output = args.json_output or output_dir / f"{default_stem}.json"
    markdown_output = (
        args.markdown_output or output_dir / f"{default_stem}.md"
    )
    if json_output.resolve() == markdown_output.resolve():
        raise SystemExit("JSON and Markdown output paths must differ")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    markdown_output.write_text(render_markdown(analysis), encoding="utf-8")
    print(f"Wrote {json_output}")
    print(f"Wrote {markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
