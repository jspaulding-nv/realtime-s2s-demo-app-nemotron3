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
CAPACITY_LIMIT_SECONDS = 10.0
BURST_WINDOWS_SECONDS = (30, 60, 300)
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
    input_boundary_source: str
    legacy_last_chunk_start_seconds: float | None
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
    last_chunk_sent_seconds: float | None = None
    last_chunk_end_seconds: float | None = None
    explicit_input_end_seconds: float | None = None

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
                if audio_bytes <= 0:
                    raise ValueError(
                        f"{path}:{row_number}: chunk_sent bytes must be positive"
                    )
                last_chunk_sent_seconds = max(
                    last_chunk_sent_seconds or 0.0, timestamp_seconds
                )
                chunk_end_seconds = timestamp_seconds + (
                    audio_bytes / (sample_rate * bytes_per_sample)
                )
                last_chunk_end_seconds = max(
                    last_chunk_end_seconds or 0.0, chunk_end_seconds
                )
            elif row["stage"] == "input_ended":
                explicit_input_end_seconds = max(
                    explicit_input_end_seconds or 0.0, timestamp_seconds
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

    if explicit_input_end_seconds is not None:
        input_end_seconds = explicit_input_end_seconds
        input_boundary_source = "explicit_input_ended"
        legacy_last_chunk_start_seconds = None
    else:
        input_end_seconds = last_chunk_end_seconds
        input_boundary_source = "estimated_last_chunk_end"
        legacy_last_chunk_start_seconds = last_chunk_sent_seconds
    if input_end_seconds is None:
        raise ValueError(f"{path}: no client input boundary events found")
    if not received:
        raise ValueError(f"{path}: no client audio_received events found")

    received.sort(key=lambda item: (item[0], item[1]))
    return PlaybackTrace(
        path=path,
        input_end_seconds=input_end_seconds,
        input_boundary_source=input_boundary_source,
        legacy_last_chunk_start_seconds=legacy_last_chunk_start_seconds,
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


def _dedupe_positive_floats(
    values: Sequence[float] | None,
    *,
    option_name: str,
) -> tuple[float, ...]:
    """Validate positive finite values and preserve first-seen order."""

    normalized: list[float] = []
    seen: set[float] = set()
    for raw_value in values or ():
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{option_name} values must be numbers") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"{option_name} values must be finite and positive"
            )
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return tuple(normalized)


def normalize_capacity_sweep_options(
    constant_rates: Sequence[float] | None,
    media_duration_scales: Sequence[float] | None,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Validate and normalize optional constant-rate sweep dimensions."""

    rates = _dedupe_positive_floats(
        constant_rates,
        option_name="constant rate",
    )
    scales = _dedupe_positive_floats(
        media_duration_scales,
        option_name="media duration scale",
    )
    if not rates:
        if scales:
            raise ValueError(
                "media duration scales require at least one constant rate"
            )
        return (), ()
    return rates, scales or (1.0,)


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _rolling_arrival_rate_p95(
    chunks: Sequence[AudioChunk],
    *,
    window_seconds: float,
    observation_end_seconds: float,
) -> float:
    """Return nearest-rank p95 of wall-clock media arrival rate.

    Windows start at whole wall-clock seconds and use ``[s, s + window)``.
    Every normal window is fully contained before end-of-input. A trace shorter
    than the requested window uses one window starting at zero. The denominator
    is always the full requested window, so time without translated-media
    arrivals contributes zero.
    """

    if not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("rolling window must be finite and positive")
    if (
        not math.isfinite(observation_end_seconds)
        or observation_end_seconds < 0
    ):
        raise ValueError(
            "rolling observation end must be finite and non-negative"
        )

    starts = range(
        max(0, math.floor(observation_end_seconds - window_seconds)) + 1
    )
    left = 0
    right = 0
    window_media_seconds = 0.0
    rates: list[float] = []
    for start_seconds in starts:
        end_seconds = start_seconds + window_seconds
        captured_end_seconds = min(end_seconds, observation_end_seconds)
        while (
            right < len(chunks)
            and chunks[right].arrival_seconds < captured_end_seconds
        ):
            window_media_seconds += chunks[right].duration_seconds
            right += 1
        while (
            left < right
            and chunks[left].arrival_seconds < start_seconds
        ):
            window_media_seconds -= chunks[left].duration_seconds
            left += 1
        rates.append(window_media_seconds / window_seconds)
    return _nearest_rank(rates, 0.95)


def _burst_diagnostics(trace: PlaybackTrace) -> dict[str, Any]:
    durations = [chunk.duration_seconds for chunk in trace.chunks]
    return _round_floats(
        {
            "translated_audio_chunk_duration_seconds": {
                "p50": _nearest_rank(durations, 0.50),
                "p95": _nearest_rank(durations, 0.95),
                "max": max(durations),
            },
            "rolling_translated_media_arrival_rate_p95_x_realtime": {
                f"{window}_seconds": _rolling_arrival_rate_p95(
                    trace.chunks,
                    window_seconds=float(window),
                    observation_end_seconds=trace.input_end_seconds,
                )
                for window in BURST_WINDOWS_SECONDS
            },
        }
    )


def _constant_rate_scenario(
    trace: PlaybackTrace,
    *,
    constant_rate: float,
    media_duration_scale: float,
) -> dict[str, Any]:
    scaled_chunks = tuple(
        AudioChunk(
            arrival_seconds=chunk.arrival_seconds,
            duration_seconds=chunk.duration_seconds * media_duration_scale,
            audio_bytes=chunk.audio_bytes,
            source_index=chunk.source_index,
        )
        for chunk in trace.chunks
    )
    constant_policy = PlaybackPolicy(
        target_queue_seconds=5.0,
        urgent_queue_seconds=8.0,
        limit_queue_seconds=CAPACITY_LIMIT_SECONDS,
        catch_up_release_seconds=4.0,
        urgent_release_seconds=7.0,
        normal_rate=constant_rate,
        catch_up_rate=constant_rate,
        urgent_rate=constant_rate,
    )
    summary = simulate_playback(
        scaled_chunks,
        input_end_seconds=trace.input_end_seconds,
        adaptive=False,
        policy=constant_policy,
    ).summary
    return _round_floats(
        {
            "constant_rate": constant_rate,
            "media_duration_scale": media_duration_scale,
            "no_drop_listener_tail_seconds": summary.listener_tail_seconds,
            "time_weighted_queue_p50_seconds": (
                summary.time_weighted_queue_p50_seconds
            ),
            "time_weighted_queue_p95_seconds": (
                summary.time_weighted_queue_p95_seconds
            ),
            "peak_queue_depth_seconds": summary.peak_queue_depth_seconds,
            "seconds_above_10_seconds": summary.seconds_above_limit,
            "percent_playback_window_above_10_seconds": (
                summary.percent_playback_window_above_limit
            ),
            "chunks_dropped": summary.chunks_dropped,
        }
    )


def build_capacity_sweep(
    traces: Sequence[PlaybackTrace],
    *,
    constant_rates: Sequence[float],
    media_duration_scales: Sequence[float],
) -> dict[str, Any]:
    """Build an offline, no-drop constant-rate capacity sweep."""

    rates, scales = normalize_capacity_sweep_options(
        constant_rates,
        media_duration_scales,
    )
    if not rates:
        raise ValueError("at least one constant rate is required")

    return {
        "constant_rates": list(rates),
        "media_duration_scales": list(scales),
        "semantics": {
            "offline_replay_only": True,
            "preserve_every_chunk": True,
            "arrival_timestamps_and_input_end_are_unchanged": True,
            "media_duration_scale_multiplies_each_translated_chunk": True,
            "burst_diagnostics_use_captured_unscaled_media_durations": True,
            "queue_limit_seconds": CAPACITY_LIMIT_SECONDS,
            "queue_percentiles_are_exact_time_weighted_playback_window_values": True,
            "percent_above_10_denominator": (
                "wall-clock playback window from first translated-media "
                "arrival through final playback end"
            ),
            "rolling_windows_seconds": list(BURST_WINDOWS_SECONDS),
            "rolling_window_interval": "[s, s + window)",
            "rolling_rate_denominator": (
                "the full configured window in seconds; unavailable time "
                "without translated-media arrivals contributes zero"
            ),
            "rolling_rate_samples": (
                "one sample per whole-second start from zero through the "
                "last window fully contained before end-of-input; traces "
                "shorter than the window use one start at zero"
            ),
            "rolling_windows_exclude_post_input_arrivals": True,
            "rolling_rate_p95": (
                "nearest-rank p95 across wall-clock window samples"
            ),
            "diagnostics_exclude_transcript_and_audio_content": True,
        },
        "traces": [
            {
                "trace_csv": trace.path.name,
                "trace_sha256": trace.sha256,
                "burst_diagnostics": _burst_diagnostics(trace),
                "scenarios": [
                    _constant_rate_scenario(
                        trace,
                        constant_rate=rate,
                        media_duration_scale=scale,
                    )
                    for rate in rates
                    for scale in scales
                ],
            }
            for trace in traces
        ],
    }


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
    legacy_fixed_tail = None
    legacy_fixed_delta = None
    if trace.legacy_last_chunk_start_seconds is not None:
        legacy_fixed_tail = simulate_playback(
            trace.chunks,
            input_end_seconds=trace.legacy_last_chunk_start_seconds,
            adaptive=False,
            policy=policy,
        ).summary.listener_tail_seconds
        legacy_fixed_delta = (
            legacy_fixed_tail - recorded_fixed_tail_seconds
            if recorded_fixed_tail_seconds is not None
            else None
        )

    return _round_floats(
        {
            "trace_csv": trace.path.name,
            "trace_sha256": trace.sha256,
            "input_end_seconds": trace.input_end_seconds,
            "input_boundary_source": trace.input_boundary_source,
            "legacy_last_chunk_start_seconds": (
                trace.legacy_last_chunk_start_seconds
            ),
            "translated_audio_seconds": fixed.total_source_duration_seconds,
            "recorded_fixed_listener_tail_seconds": recorded_fixed_tail_seconds,
            "reproduced_fixed_tail_delta_seconds": fixed_delta,
            "legacy_start_boundary_fixed_tail_seconds": legacy_fixed_tail,
            "legacy_start_boundary_recorded_delta_seconds": legacy_fixed_delta,
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
    constant_rates: Sequence[float] | None = None,
    media_duration_scales: Sequence[float] | None = None,
) -> dict[str, Any]:
    rates, scales = normalize_capacity_sweep_options(
        constant_rates,
        media_duration_scales,
    )
    traces = []
    loaded_traces: list[PlaybackTrace] = []
    for path in sorted(csv_paths):
        trace = load_event_trace(path)
        if rates:
            loaded_traces.append(trace)
        recorded_tail = _load_recorded_tail(path)
        result = analyze_trace(
            trace,
            policy=policy,
            recorded_fixed_tail_seconds=recorded_tail,
        )
        delta = result["reproduced_fixed_tail_delta_seconds"]
        legacy_delta = result[
            "legacy_start_boundary_recorded_delta_seconds"
        ]
        if not validate_recorded:
            validation = {
                "performed": False,
                "passed": None,
                "boundary": None,
                "note": "recorded-tail validation was disabled",
            }
        elif delta is None:
            validation = {
                "performed": False,
                "passed": None,
                "boundary": None,
                "note": "no adjacent recorded tail was available",
            }
        elif abs(delta) <= validation_tolerance_seconds:
            validation = {
                "performed": True,
                "passed": True,
                "boundary": "corrected_input_boundary",
                "note": "recorded tail matches the corrected input boundary",
            }
        elif (
            result["input_boundary_source"] == "estimated_last_chunk_end"
            and legacy_delta is not None
            and abs(legacy_delta) <= validation_tolerance_seconds
        ):
            validation = {
                "performed": True,
                "passed": True,
                "boundary": "legacy_last_chunk_start_compatibility",
                "note": (
                    "recorded tail used the historical last-chunk-start "
                    "boundary; reported fixed/adaptive metrics use the "
                    "corrected last-chunk-end boundary"
                ),
            }
        else:
            raise ValueError(
                f"{path}: reproduced fixed tail differs from recorded result "
                f"by {delta:.6f}s"
            )
        result["recorded_tail_validation"] = validation
        traces.append(result)

    if not traces:
        raise ValueError("no event CSV traces were supplied")

    fixed_total = sum(item["fixed_1x"]["listener_tail_seconds"] for item in traces)
    adaptive_total = sum(item["adaptive"]["listener_tail_seconds"] for item in traces)
    reduction_total = fixed_total - adaptive_total

    analysis = {
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
            "input_end_prefers_explicit_event": True,
            "legacy_missing_end_event_uses_last_chunk_end": True,
            "historical_last_chunk_start_validation_is_annotated_compatibility": True,
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
    if rates:
        analysis["capacity_sweep"] = build_capacity_sweep(
            loaded_traces,
            constant_rates=rates,
            media_duration_scales=scales,
        )
    return analysis


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
                "`Fixed tail` uses the explicit input-ended event when present, "
                "otherwise the exact end of the final source chunk. Historical "
                "summaries that used the final chunk's start are accepted only "
                "as annotated compatibility evidence. This metric is distinct "
                "from whole-file output/input duration drift."
            ),
            "",
        ]
    )
    capacity_sweep = analysis.get("capacity_sweep")
    if capacity_sweep is not None:
        lines.extend(
            [
                "## Offline constant-rate capacity sweep",
                "",
                (
                    "This optional no-drop replay keeps translated-audio "
                    "arrival timestamps and the source input boundary fixed. "
                    "The media-duration scale multiplies every translated "
                    "audio chunk before it is queued. Burst diagnostics use "
                    "the captured, unscaled chunk durations."
                ),
                "",
                (
                    "Burst rates are translated-media seconds per wall-clock "
                    "second. Windows start at whole wall-clock seconds and "
                    "use `[s, s + window)`. Each normal window is fully "
                    "contained before end-of-input; a trace shorter than the "
                    "window uses one start at zero. The denominator is always "
                    "the full 30, 60, or 300 seconds, and post-input arrivals "
                    "are excluded. Reported p95 values use nearest-rank over "
                    "those wall-clock samples."
                ),
                (
                    "Queue percentiles are exact time-weighted values. The "
                    "percentage above 10 seconds uses the wall-clock playback "
                    "window from first translated-media arrival through final "
                    "playback end."
                ),
                "",
                (
                    "| Trace | Chunk p50 | Chunk p95 | Chunk max | "
                    "30s arrival-rate p95 | 60s arrival-rate p95 | "
                    "300s arrival-rate p95 |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for trace in capacity_sweep["traces"]:
            diagnostics = trace["burst_diagnostics"]
            durations = diagnostics[
                "translated_audio_chunk_duration_seconds"
            ]
            rates = diagnostics[
                "rolling_translated_media_arrival_rate_p95_x_realtime"
            ]
            lines.append(
                "| {name} | {p50:.3f}s | {p95:.3f}s | {maximum:.3f}s | "
                "{rate30:.3f}x | {rate60:.3f}x | {rate300:.3f}x |".format(
                    name=_display_name(trace["trace_csv"]),
                    p50=durations["p50"],
                    p95=durations["p95"],
                    maximum=durations["max"],
                    rate30=rates["30_seconds"],
                    rate60=rates["60_seconds"],
                    rate300=rates["300_seconds"],
                )
            )

        lines.extend(
            [
                "",
                (
                    "| Trace | Constant rate | Media scale | No-drop tail | "
                    "Queue p50 | Queue p95 | Peak queue | Time >10s | "
                    "Window >10s | Dropped |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for trace in capacity_sweep["traces"]:
            for scenario in trace["scenarios"]:
                lines.append(
                    "| {name} | {rate:.3f}x | {scale:.3f}x | {tail:.3f}s | "
                    "{p50:.3f}s | {p95:.3f}s | {peak:.3f}s | "
                    "{above:.3f}s | {percent:.1f}% | {dropped:,} |".format(
                        name=_display_name(trace["trace_csv"]),
                        rate=scenario["constant_rate"],
                        scale=scenario["media_duration_scale"],
                        tail=scenario["no_drop_listener_tail_seconds"],
                        p50=scenario["time_weighted_queue_p50_seconds"],
                        p95=scenario["time_weighted_queue_p95_seconds"],
                        peak=scenario["peak_queue_depth_seconds"],
                        above=scenario["seconds_above_10_seconds"],
                        percent=scenario[
                            "percent_playback_window_above_10_seconds"
                        ],
                        dropped=scenario["chunks_dropped"],
                    )
                )
        lines.append("")
    return "\n".join(lines)


def create_argument_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--constant-rate",
        action="append",
        dest="constant_rates",
        type=float,
        help=(
            "constant no-drop playback rate for an optional capacity sweep; "
            "repeat for multiple rates"
        ),
    )
    parser.add_argument(
        "--media-duration-scale",
        action="append",
        dest="media_duration_scales",
        type=float,
        help=(
            "translated-media duration multiplier for the optional capacity "
            "sweep; repeat for multiple scales (default: 1.0)"
        ),
    )
    return parser


def parse_cli_args(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    try:
        rates, scales = normalize_capacity_sweep_options(
            args.constant_rates,
            args.media_duration_scales,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.constant_rates = rates
    args.media_duration_scales = scales
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_cli_args(argv)

    csv_paths = list(args.input_dir.glob("*_results.csv"))
    analysis = build_analysis(
        csv_paths,
        validate_recorded=not args.skip_recorded_validation,
        constant_rates=args.constant_rates,
        media_duration_scales=args.media_duration_scales,
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
