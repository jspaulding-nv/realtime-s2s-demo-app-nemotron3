#!/usr/bin/env python3
"""Analyze lossy whole-parent freshness policies on a schema-3 S2S trace."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from freshness_trace import (
    PUBLIC_SUMMARY_LABEL,
    PUBLIC_TRACE_LABEL,
    ParentFreshnessTrace,
    load_parent_freshness_trace,
)
from playback_simulation import (
    DEFAULT_PLAYBACK_POLICY,
    AudioChunk,
    FreshnessStrategy,
    simulate_parent_freshness_cap,
    simulate_playback,
)


DEFAULT_FRESHNESS_CAPS_SECONDS = (5.0, 8.0, 10.0)
DEFAULT_CANCELLATION_GUARD_SECONDS = 0.1
DEFAULT_GUARD_SENSITIVITY_SECONDS = (0.0, 0.05, 0.1, 0.25)
DEFAULT_FRESHNESS_STRATEGIES: tuple[FreshnessStrategy, ...] = (
    "oldest_first",
    "jump_to_latest_complete",
)


def _round_floats(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: _round_floats(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, digits) for item in value]
    return value


def normalize_freshness_caps(
    values: Sequence[float] | None,
) -> tuple[float, ...]:
    """Validate caps, deduplicate them, and preserve first-seen order."""

    normalized: list[float] = []
    seen: set[float] = set()
    for raw_value in (
        DEFAULT_FRESHNESS_CAPS_SECONDS if values is None else values
    ):
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("freshness caps must be numbers") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError("freshness caps must be finite and positive")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("at least one freshness cap is required")
    return tuple(normalized)


def normalize_freshness_strategies(
    values: Sequence[str] | None,
) -> tuple[FreshnessStrategy, ...]:
    """Validate strategy names, deduplicate them, and preserve order."""

    supported = set(DEFAULT_FRESHNESS_STRATEGIES)
    normalized: list[FreshnessStrategy] = []
    seen: set[str] = set()
    for value in DEFAULT_FRESHNESS_STRATEGIES if values is None else values:
        if value not in supported:
            raise ValueError(
                "freshness strategy must be 'oldest_first' or "
                "'jump_to_latest_complete'"
            )
        if value not in seen:
            normalized.append(value)  # type: ignore[arg-type]
            seen.add(value)
    if not normalized:
        raise ValueError("at least one freshness strategy is required")
    return tuple(normalized)


def normalize_cancellation_guards(
    values: Sequence[float] | None,
    *,
    default: Sequence[float],
) -> tuple[float, ...]:
    """Validate non-negative guard durations and preserve first-seen order."""

    normalized: list[float] = []
    seen: set[float] = set()
    for raw_value in default if values is None else values:
        if isinstance(raw_value, bool):
            raise ValueError("cancellation guards must be numbers")
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("cancellation guards must be numbers") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(
                "cancellation guards must be finite and non-negative"
            )
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("at least one cancellation guard is required")
    return tuple(normalized)


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _parent_durations(trace: ParentFreshnessTrace) -> dict[int, float]:
    durations: dict[int, float] = {}
    for frame in trace.frames:
        durations[frame.parent_sequence_id] = (
            durations.get(frame.parent_sequence_id, 0.0)
            + frame.duration_seconds
        )
    return durations


def _baseline_summary(trace: ParentFreshnessTrace) -> dict[str, Any]:
    simulation = simulate_playback(
        tuple(
            AudioChunk(
                arrival_seconds=frame.arrival_seconds,
                duration_seconds=frame.duration_seconds,
                audio_bytes=frame.audio_bytes,
                source_index=frame.source_index,
            )
            for frame in trace.frames
        ),
        input_end_seconds=trace.input_end_seconds,
        adaptive=True,
    )
    summary = simulation.summary
    return _round_floats(
        {
            "frames_scheduled": summary.chunks_scheduled,
            "parents": len(
                {frame.parent_sequence_id for frame in trace.frames}
            ),
            "translated_audio_seconds": summary.total_source_duration_seconds,
            "listener_tail_seconds": summary.listener_tail_seconds,
            "peak_queue_depth_seconds": summary.peak_queue_depth_seconds,
            "time_weighted_queue_p50_seconds": (
                summary.time_weighted_queue_p50_seconds
            ),
            "time_weighted_queue_p95_seconds": (
                summary.time_weighted_queue_p95_seconds
            ),
            "seconds_above_10_seconds": summary.seconds_above_limit,
            "percent_playback_window_above_10_seconds": (
                summary.percent_playback_window_above_limit
            ),
            "accelerated_source_percent": summary.accelerated_source_percent,
            "urgent_source_percent": summary.urgent_source_percent,
            "max_continuous_urgent_playback_seconds": (
                summary.max_continuous_urgent_playback_seconds
            ),
            "chunks_dropped": summary.chunks_dropped,
        }
    )


def _scenario(
    trace: ParentFreshnessTrace,
    *,
    hard_cap_seconds: float,
    strategy: FreshnessStrategy,
    cancellation_guard_seconds: float,
) -> dict[str, Any]:
    simulation = simulate_parent_freshness_cap(
        trace.frames,
        input_end_seconds=trace.input_end_seconds,
        hard_cap_seconds=hard_cap_seconds,
        strategy=strategy,
        cancellation_guard_seconds=cancellation_guard_seconds,
        adaptive=True,
    )
    summary = _round_floats(asdict(simulation.summary))
    residual_decisions = [
        _round_floats(asdict(decision))
        for decision in simulation.decisions
        if decision.residual_hard_cap_breach
    ]

    decisions_by_trigger = {
        (
            decision.source_index,
            decision.parent_sequence_id,
            decision.audio_frame_id,
        ): decision
        for decision in simulation.decisions
    }
    evictions = []
    for eviction in simulation.evictions:
        item = asdict(eviction)
        decision = decisions_by_trigger[
            (
                eviction.trigger_source_index,
                eviction.trigger_parent_sequence_id,
                eviction.trigger_audio_frame_id,
            )
        ]
        item["protected_parent_sequence_id"] = (
            eviction.eligible_parent_sequence_ids[-1]
            if (
                strategy == "jump_to_latest_complete"
                and eviction.eligible_parent_sequence_ids
            )
            else None
        )
        item["residual_over_cap_seconds"] = decision.residual_over_cap_seconds
        evictions.append(_round_floats(item))

    return {
        "strategy": strategy,
        "hard_cap_seconds": hard_cap_seconds,
        "summary": summary,
        "evictions": evictions,
        "residual_breach_decisions": residual_decisions,
        "whole_parent_invariants": {
            "partial_parent_drops": summary["parents_partially_dropped"],
            "all_drops_are_complete_whole_parents": (
                summary["parents_partially_dropped"] == 0
            ),
            "frame_accounting_reconciles": (
                summary["frames_retained"] + summary["frames_dropped"]
                == summary["frames_received"]
            ),
            "byte_accounting_reconciles": (
                summary["retained_audio_bytes"] + summary["dropped_audio_bytes"]
                == summary["total_audio_bytes"]
            ),
        },
    }


def build_freshness_cap_analysis(
    trace: ParentFreshnessTrace,
    *,
    freshness_caps: Sequence[float] | None = None,
    strategies: Sequence[str] | None = None,
    cancellation_guard_seconds: float = DEFAULT_CANCELLATION_GUARD_SECONDS,
    guard_sensitivity_seconds: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Build a privacy-safe offline comparison for one validated trace."""

    caps = normalize_freshness_caps(freshness_caps)
    normalized_strategies = normalize_freshness_strategies(strategies)
    primary_guard = normalize_cancellation_guards(
        [cancellation_guard_seconds],
        default=(DEFAULT_CANCELLATION_GUARD_SECONDS,),
    )[0]
    sensitivity_guards = normalize_cancellation_guards(
        guard_sensitivity_seconds,
        default=DEFAULT_GUARD_SENSITIVITY_SECONDS,
    )
    parent_durations = _parent_durations(trace)
    duration_values = list(parent_durations.values())

    return _round_floats(
        {
            "schema_version": 1,
            "analysis_type": "schema3_whole_parent_freshness_cap",
            "source": {
                "trace_csv": PUBLIC_TRACE_LABEL,
                "summary_json": PUBLIC_SUMMARY_LABEL,
                "trace_sha256": trace.trace_sha256,
                "summary_sha256": trace.summary_sha256,
            },
            "audio_format": {
                "sample_rate_hz": trace.sample_rate_hz,
                "channels": trace.channels,
                "bytes_per_sample": trace.bytes_per_sample,
            },
            "policy": asdict(DEFAULT_PLAYBACK_POLICY),
            "semantics": {
                "offline_counterfactual_only": True,
                "intentionally_lossy": True,
                "adaptive_playback_precedes_eviction": True,
                "only_complete_not_yet_audible_parents_are_evictable": True,
                "audible_or_incomplete_parents_are_retained": True,
                "cancellation_guard_seconds": primary_guard,
                "cancellation_guard_semantics": (
                    "a parent is protected unless every scheduled frame starts "
                    "strictly after decision time plus the guard"
                ),
                "eviction_unit": "complete translated parent",
                "queue_cap_scope": (
                    "browser scheduled translated-audio playback queue"
                ),
                "not_measured_by_queue_cap": [
                    "ASR/NMT/TTS processing before browser receipt",
                    "source phrase or punchline to translated audibility",
                    "room reaction synchronization",
                ],
                "hard_cap_may_be_unachievable": (
                    "an audible, incomplete, or protected parent can itself "
                    "extend beyond the selected queue cap"
                ),
                "parent_duration_scope": (
                    "unaccelerated source PCM media duration; it is context, "
                    "not scheduled queue depth"
                ),
                "live_browser_support_present": False,
                "live_browser_blockers": [
                    "binary PCM currently has no client-visible parent/frame metadata",
                    "parent completion currently has no client-visible marker",
                    (
                        "the browser schedules PCM immediately without a "
                        "retained parent queue"
                    ),
                ],
                "privacy": (
                    "output contains hashes, fixed role labels, numeric IDs, "
                    "timing, counts, byte totals, and durations only"
                ),
            },
            "captured_trace": {
                "input_end_seconds": trace.input_end_seconds,
                "frame_count": len(trace.frames),
                "parent_count": len(parent_durations),
                "translated_audio_seconds": sum(duration_values),
                "parent_source_pcm_duration_seconds": {
                    "min": min(duration_values),
                    "p50": _nearest_rank(duration_values, 0.50),
                    "p95": _nearest_rank(duration_values, 0.95),
                    "max": max(duration_values),
                },
                "parents_with_source_pcm_longer_than_cap": {
                    f"{cap:g}_seconds": sum(
                        duration > cap for duration in duration_values
                    )
                    for cap in caps
                },
            },
            "adaptive_no_drop_baseline": _baseline_summary(trace),
            "strategies": {
                "oldest_first": (
                    "evict the oldest eligible complete parent, compact, "
                    "and repeat only until the cap is met or none remain"
                ),
                "jump_to_latest_complete": (
                    "protect the newest eligible complete parent and evict "
                    "all older eligible complete parents on a breach"
                ),
            },
            "scenarios": [
                _scenario(
                    trace,
                    hard_cap_seconds=cap,
                    strategy=strategy,
                    cancellation_guard_seconds=primary_guard,
                )
                for cap in caps
                for strategy in normalized_strategies
            ],
            "cancellation_guard_sensitivity": {
                "strategy": "oldest_first",
                "hard_cap_seconds": max(caps),
                "guards_seconds": list(sensitivity_guards),
                "scenarios": [
                    {
                        "cancellation_guard_seconds": guard,
                        "summary": _scenario(
                            trace,
                            hard_cap_seconds=max(caps),
                            strategy="oldest_first",
                            cancellation_guard_seconds=guard,
                        )["summary"],
                    }
                    for guard in sensitivity_guards
                ],
            },
        }
    )


def render_freshness_cap_markdown(analysis: dict[str, Any]) -> str:
    """Render the compact decision report; detailed events remain in JSON."""

    captured = analysis["captured_trace"]
    baseline = analysis["adaptive_no_drop_baseline"]
    parent_duration = captured["parent_source_pcm_duration_seconds"]
    guard_ms = analysis["semantics"]["cancellation_guard_seconds"] * 1000.0
    lines = [
        "# Schema-3 whole-parent freshness-cap simulation",
        "",
        (
            "> **Lossy offline counterfactual:** these scenarios intentionally "
            "skip complete translated speech parents. No live playback behavior "
            "was changed."
        ),
        "",
        (
            f"Validated evidence: {captured['frame_count']:,} PCM frames across "
            f"{captured['parent_count']} complete parents "
            f"({captured['translated_audio_seconds']:.3f}s translated audio)."
        ),
        "",
        (
            "The cap applies only to translated audio already received and "
            "scheduled in the browser. It does not include ASR, NMT, TTS, or "
            "initial publication latency, so it is not yet a direct "
            "speaker-to-listener or punchline-delay measurement."
        ),
        "",
        (
            f"Primary scenarios use a {guard_ms:.0f} ms cancellation guard: "
            "a parent is protected when any scheduled frame begins inside "
            "that safety window."
        ),
        "",
        "## No-drop baseline",
        "",
        (
            f"Adaptive 1.00x/1.05x/1.10x playback retained every frame but had "
            f"a {baseline['time_weighted_queue_p95_seconds']:.3f}s queue p95, "
            f"{baseline['peak_queue_depth_seconds']:.3f}s peak, and "
            f"{baseline['listener_tail_seconds']:.3f}s listener tail."
        ),
        "",
        "## Loss/freshness tradeoff",
        "",
        (
            "| Strategy | Queue cap | Achieved throughout | Audio retained | "
            "Parents retained | Parents skipped | Queue p95 | Peak queue | "
            "Time above cap | Listener tail |"
        ),
        "|---|---:|:---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario in analysis["scenarios"]:
        summary = scenario["summary"]
        lines.append(
            "| {strategy} | {cap:.0f}s | {achieved} | "
            "{audio:.1f}% | {parents}/{total} | {dropped} | "
            "{p95:.3f}s | {peak:.3f}s | {above:.3f}s | {tail:.3f}s |".format(
                strategy=scenario["strategy"].replace("_", " "),
                cap=scenario["hard_cap_seconds"],
                achieved="yes" if summary["hard_cap_achieved"] else "**no**",
                audio=summary["retained_source_percent"],
                parents=summary["parents_retained"],
                total=summary["parents_received"],
                dropped=summary["parents_dropped"],
                p95=summary["time_weighted_queue_p95_seconds"],
                peak=summary["peak_queue_depth_seconds"],
                above=summary["seconds_above_hard_cap"],
                tail=summary["listener_tail_seconds"],
            )
        )

    lines.extend(
        [
            "",
            (
                "Whole-parent eviction did not achieve every cap on this "
                "capture. Unaccelerated parent source-PCM duration was "
                f"{parent_duration['p50']:.3f}s p50, "
                f"{parent_duration['p95']:.3f}s p95, and "
                f"{parent_duration['max']:.3f}s maximum. These durations are "
                "context rather than scheduled queue depth; the observed "
                "residual breaches prove that protected/incomplete audio "
                "still exceeded each tested cap."
            ),
            "",
            (
                "The JSON companion records every privacy-safe eviction and "
                "residual breach using only numeric parent/frame IDs, timing, "
                "counts, bytes, and durations."
            ),
            "",
            "## Cancellation-guard sensitivity",
            "",
            (
                "This table holds the oldest-first strategy and largest "
                "configured cap fixed while varying the practical Web Audio "
                "cancellation margin."
            ),
            "",
            (
                "| Guard | Audio retained | Parents skipped | Queue p95 | "
                "Peak queue | Time above cap | Listener tail |"
            ),
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scenario in analysis["cancellation_guard_sensitivity"]["scenarios"]:
        summary = scenario["summary"]
        lines.append(
            "| {guard:.0f} ms | {retained:.1f}% | {dropped} | "
            "{p95:.3f}s | {peak:.3f}s | {above:.3f}s | {tail:.3f}s |".format(
                guard=scenario["cancellation_guard_seconds"] * 1000.0,
                retained=summary["retained_source_percent"],
                dropped=summary["parents_dropped"],
                p95=summary["time_weighted_queue_p95_seconds"],
                peak=summary["peak_queue_depth_seconds"],
                above=summary["seconds_above_hard_cap"],
                tail=summary["listener_tail_seconds"],
            )
        )
    lines.extend(
        [
            "",
            "## Engineering gate",
            "",
            (
                "The current browser cannot enforce these policies: it receives "
                "anonymous binary PCM, has no parent-completion marker, and "
                "immediately schedules buffers without retaining a parent-aware "
                "queue. Promotion therefore requires protocol metadata and an "
                "opt-in short-lookahead scheduler before any live loss policy "
                "can be tested."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a validated schema-3 capture through lossy whole-parent "
            "freshness policies"
        )
    )
    parser.add_argument(
        "--results-csv",
        type=Path,
        required=True,
        help="captured client/backend *_results.csv",
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        help="adjacent schema-3 summary (inferred when omitted)",
    )
    parser.add_argument(
        "--freshness-cap",
        action="append",
        dest="freshness_caps",
        type=float,
        help="positive queue cap in seconds; repeat (default: 5, 8, 10)",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        choices=DEFAULT_FRESHNESS_STRATEGIES,
        help="strategy to simulate; repeat (default: both)",
    )
    parser.add_argument(
        "--cancellation-guard-ms",
        type=float,
        default=DEFAULT_CANCELLATION_GUARD_SECONDS * 1000.0,
        help=(
            "protect audio scheduled to begin within this many milliseconds "
            "(default: 100)"
        ),
    )
    parser.add_argument(
        "--guard-sensitivity-ms",
        action="append",
        type=float,
        help=(
            "cancellation guard for the largest-cap oldest-first sensitivity "
            "table; repeat (default: 0, 50, 100, 250)"
        ),
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    try:
        args.freshness_caps = normalize_freshness_caps(args.freshness_caps)
        args.strategy = normalize_freshness_strategies(args.strategy)
        args.cancellation_guard_seconds = normalize_cancellation_guards(
            [args.cancellation_guard_ms / 1000.0],
            default=(DEFAULT_CANCELLATION_GUARD_SECONDS,),
        )[0]
        args.guard_sensitivity_seconds = normalize_cancellation_guards(
            (
                [value / 1000.0 for value in args.guard_sensitivity_ms]
                if args.guard_sensitivity_ms is not None
                else None
            ),
            default=DEFAULT_GUARD_SENSITIVITY_SECONDS,
        )
    except ValueError as exc:
        parser.error(str(exc))

    output_dir = args.results_csv.parent
    if args.json_output is None:
        args.json_output = output_dir / "schema3_freshness_cap_analysis.json"
    if args.markdown_output is None:
        args.markdown_output = output_dir / "schema3_freshness_cap_analysis.md"
    if args.json_output.resolve() == args.markdown_output.resolve():
        parser.error("JSON and Markdown output paths must be different")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_cli_args(argv)
    trace = load_parent_freshness_trace(
        args.results_csv,
        args.summary_json,
    )
    analysis = build_freshness_cap_analysis(
        trace,
        freshness_caps=args.freshness_caps,
        strategies=args.strategy,
        cancellation_guard_seconds=args.cancellation_guard_seconds,
        guard_sensitivity_seconds=args.guard_sensitivity_seconds,
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(analysis, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(
        render_freshness_cap_markdown(analysis),
        encoding="utf-8",
    )
    print(f"Wrote {args.json_output}")
    print(f"Wrote {args.markdown_output}")


if __name__ == "__main__":
    main()
