#!/usr/bin/env python3
"""Compare fixed 1.00x and adaptive playback on captured S2S event traces."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from playback_simulation import (
    DEFAULT_PLAYBACK_POLICY,
    AudioChunk,
    PlaybackPolicy,
    PlaybackSummary,
    simulate_playback,
)


SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
REQUIRED_COLUMNS = {
    "source",
    "stage",
    "timestamp_ms",
    "chunk_index",
    "audio_bytes",
}


@dataclass(frozen=True)
class PlaybackTrace:
    path: Path
    input_end_seconds: float
    chunks: tuple[AudioChunk, ...]
    sha256: str


def load_event_trace(
    path: Path,
    *,
    sample_rate: int = SAMPLE_RATE,
    bytes_per_sample: int = BYTES_PER_SAMPLE,
) -> PlaybackTrace:
    """Load client send/receive events from a batch-test CSV."""

    if sample_rate <= 0 or bytes_per_sample <= 0:
        raise ValueError("sample_rate and bytes_per_sample must be positive")

    hasher = hashlib.sha256()
    with path.open("rb") as binary_handle:
        for block in iter(lambda: binary_handle.read(1024 * 1024), b""):
            hasher.update(block)
    digest = hasher.hexdigest()
    received: list[tuple[float, int, AudioChunk]] = []
    input_end_seconds: float | None = None

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{path}: missing required columns: {', '.join(sorted(missing))}"
            )

        for row_number, row in enumerate(reader, start=2):
            if row["source"] != "client":
                continue
            try:
                timestamp_seconds = float(row["timestamp_ms"]) / 1000.0
                chunk_index = int(row["chunk_index"])
                audio_bytes = int(row["audio_bytes"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{path}:{row_number}: invalid numeric event value"
                ) from exc

            if not math.isfinite(timestamp_seconds) or timestamp_seconds < 0:
                raise ValueError(
                    f"{path}:{row_number}: timestamp must be finite and non-negative"
                )

            if row["stage"] == "chunk_sent":
                input_end_seconds = max(
                    input_end_seconds or 0.0, timestamp_seconds
                )
            elif row["stage"] == "audio_received":
                if audio_bytes <= 0:
                    raise ValueError(
                        f"{path}:{row_number}: audio_received bytes must be positive"
                    )
                received.append(
                    (
                        timestamp_seconds,
                        row_number,
                        AudioChunk(
                            arrival_seconds=timestamp_seconds,
                            duration_seconds=(
                                audio_bytes / (sample_rate * bytes_per_sample)
                            ),
                            audio_bytes=audio_bytes,
                            source_index=chunk_index,
                        ),
                    )
                )

    if input_end_seconds is None:
        raise ValueError(f"{path}: no client chunk_sent events found")
    if not received:
        raise ValueError(f"{path}: no client audio_received events found")

    received.sort(key=lambda item: (item[0], item[1]))
    return PlaybackTrace(
        path=path,
        input_end_seconds=input_end_seconds,
        chunks=tuple(item[2] for item in received),
        sha256=digest,
    )


def _round_floats(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: _round_floats(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, digits) for item in value]
    return value


def _compact_summary(summary: PlaybackSummary) -> dict[str, Any]:
    return _round_floats(asdict(summary))


def _load_recorded_tail(csv_path: Path) -> float | None:
    summary_path = csv_path.with_name(
        csv_path.name.removesuffix("_results.csv") + "_summary.json"
    )
    if not summary_path.exists():
        return None
    with summary_path.open(encoding="utf-8") as handle:
        summary = json.load(handle)
    value = summary.get("playback_tail_sec")
    return float(value) if value is not None else None


def analyze_trace(
    trace: PlaybackTrace,
    *,
    policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
    recorded_fixed_tail_seconds: float | None = None,
) -> dict[str, Any]:
    fixed = simulate_playback(
        trace.chunks,
        input_end_seconds=trace.input_end_seconds,
        adaptive=False,
        policy=policy,
    ).summary
    adaptive = simulate_playback(
        trace.chunks,
        input_end_seconds=trace.input_end_seconds,
        adaptive=True,
        policy=policy,
    ).summary
    reduction = fixed.listener_tail_seconds - adaptive.listener_tail_seconds
    reduction_percent = (
        reduction / fixed.listener_tail_seconds * 100.0
        if fixed.listener_tail_seconds > 0
        else 0.0
    )
    fixed_delta = (
        fixed.listener_tail_seconds - recorded_fixed_tail_seconds
        if recorded_fixed_tail_seconds is not None
        else None
    )

    return _round_floats(
        {
            "trace_csv": trace.path.name,
            "trace_sha256": trace.sha256,
            "input_end_seconds": trace.input_end_seconds,
            "translated_audio_seconds": fixed.total_source_duration_seconds,
            "recorded_fixed_listener_tail_seconds": recorded_fixed_tail_seconds,
            "reproduced_fixed_tail_delta_seconds": fixed_delta,
            "fixed_1x": _compact_summary(fixed),
            "adaptive": _compact_summary(adaptive),
            "comparison": {
                "listener_tail_reduction_seconds": reduction,
                "listener_tail_reduction_percent": reduction_percent,
                "peak_queue_reduction_seconds": (
                    fixed.peak_queue_depth_seconds
                    - adaptive.peak_queue_depth_seconds
                ),
            },
        }
    )


def build_analysis(
    csv_paths: Sequence[Path],
    *,
    policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
    validate_recorded: bool = True,
    validation_tolerance_seconds: float = 0.005,
) -> dict[str, Any]:
    traces = []
    for path in sorted(csv_paths):
        trace = load_event_trace(path)
        recorded_tail = _load_recorded_tail(path)
        result = analyze_trace(
            trace,
            policy=policy,
            recorded_fixed_tail_seconds=recorded_tail,
        )
        delta = result["reproduced_fixed_tail_delta_seconds"]
        if (
            validate_recorded
            and delta is not None
            and abs(delta) > validation_tolerance_seconds
        ):
            raise ValueError(
                f"{path}: reproduced fixed tail differs from recorded result "
                f"by {delta:.6f}s"
            )
        traces.append(result)

    if not traces:
        raise ValueError("no event CSV traces were supplied")

    fixed_total = sum(item["fixed_1x"]["listener_tail_seconds"] for item in traces)
    adaptive_total = sum(item["adaptive"]["listener_tail_seconds"] for item in traces)
    reduction_total = fixed_total - adaptive_total

    return {
        "schema_version": 1,
        "source_format": "batch_latency_test client event CSV",
        "audio_format": {
            "sample_rate_hz": SAMPLE_RATE,
            "channels": 1,
            "bytes_per_sample": BYTES_PER_SAMPLE,
        },
        "policy": _round_floats(asdict(policy)),
        "semantics": {
            "preserve_every_chunk": True,
            "limit_is_sla_alarm_not_drop_boundary": True,
            "queue_time_above_threshold_is_exact_between_arrivals": True,
            "arrival_queue_percentiles_use_nearest_rank": True,
        },
        "traces": traces,
        "aggregate": _round_floats(
            {
                "trace_count": len(traces),
                "fixed_listener_tail_seconds": fixed_total,
                "adaptive_listener_tail_seconds": adaptive_total,
                "listener_tail_reduction_seconds": reduction_total,
                "listener_tail_reduction_percent": (
                    reduction_total / fixed_total * 100.0
                    if fixed_total > 0
                    else 0.0
                ),
            }
        ),
    }


def _display_name(filename: str) -> str:
    path = Path(filename)
    filename = path.name
    normalized = filename.lower().replace("_", "-")
    display = filename.removesuffix("_results.csv")
    for index in range(1, 4):
        if f"long-form-{index:02d}" in normalized or f"sample-{index:02d}" in normalized:
            display = f"Long-form sample {index:02d}"
            break
    if path.parent.name.startswith("repeat-"):
        return f"{display} ({path.parent.name})"
    return display


def render_markdown(analysis: dict[str, Any]) -> str:
    policy = analysis["policy"]
    lines = [
        "# Nemotron 3 adaptive playback simulation",
        "",
        (
            "This deterministic replay uses client-side translated-audio arrival "
            "timestamps and PCM byte counts from the captured Nemotron 3 traces."
        ),
        "",
        (
            f"Policy: target {policy['target_queue_seconds']:.0f}s, urgent "
            f"{policy['urgent_queue_seconds']:.0f}s, SLA limit "
            f"{policy['limit_queue_seconds']:.0f}s; rates "
            f"{policy['normal_rate']:.2f}x / {policy['catch_up_rate']:.2f}x / "
            f"{policy['urgent_rate']:.2f}x. Release hysteresis is "
            f"{policy['catch_up_release_seconds']:.0f}s / "
            f"{policy['urgent_release_seconds']:.0f}s."
        ),
        "",
        (
            "| Trace | Chunks | Fixed tail | Adaptive tail | Reduction | "
            "Adaptive peak queue | Time >10s | Accelerated audio |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for trace in analysis["traces"]:
        fixed = trace["fixed_1x"]
        adaptive = trace["adaptive"]
        comparison = trace["comparison"]
        lines.append(
            "| {name} | {chunks:,} | {fixed:.3f}s | {adaptive:.3f}s | "
            "{reduction:.1f}% | {peak:.3f}s | {above:.3f}s | {accelerated:.1f}% |".format(
                name=_display_name(trace["trace_csv"]),
                chunks=adaptive["chunks_scheduled"],
                fixed=fixed["listener_tail_seconds"],
                adaptive=adaptive["listener_tail_seconds"],
                reduction=comparison["listener_tail_reduction_percent"],
                peak=adaptive["peak_queue_depth_seconds"],
                above=adaptive["seconds_above_limit"],
                accelerated=adaptive["accelerated_source_percent"],
            )
        )

    aggregate = analysis["aggregate"]
    lines.extend(
        [
            "",
            (
                f"Across all {aggregate['trace_count']} replays, listener tail fell from "
                f"{aggregate['fixed_listener_tail_seconds']:.3f}s to "
                f"{aggregate['adaptive_listener_tail_seconds']:.3f}s "
                f"({aggregate['listener_tail_reduction_percent']:.1f}% reduction)."
            ),
            "",
            (
                "Every translated chunk is retained. The 10-second value is an "
                "audience-latency SLA alarm, not a hard cap: if translated audio "
                "arrives faster than 1.10x playback can consume it, the queue may "
                "still exceed 10 seconds."
            ),
            "",
            (
                "`Fixed tail` is reproduced from the event trace and matches the "
                "previously recorded browser queue calculation. It is distinct "
                "from whole-file output/input duration drift."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay captured translation audio through the playback policy"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("test_results_nemotron"),
        help="directory containing *_results.csv traces",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("test_results_nemotron/playback_policy_analysis.json"),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("test_results_nemotron/playback_policy_analysis.md"),
    )
    parser.add_argument(
        "--skip-recorded-validation",
        action="store_true",
        help="do not compare reproduced fixed tails with adjacent summary JSON",
    )
    args = parser.parse_args()

    csv_paths = list(args.input_dir.glob("*_results.csv"))
    analysis = build_analysis(
        csv_paths,
        validate_recorded=not args.skip_recorded_validation,
    )

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(analysis, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.write_text(render_markdown(analysis), encoding="utf-8")
    print(f"Wrote {args.json_output}")
    print(f"Wrote {args.markdown_output}")


if __name__ == "__main__":
    main()
