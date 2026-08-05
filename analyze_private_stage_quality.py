#!/usr/bin/env python3
"""Join a private staged replay to source-window bilingual review results.

The report intentionally retains English ASR text and Spanish NMT text. It is
therefore default-private evidence and must never be committed or published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import wave
from pathlib import Path
from typing import Any, Optional, Sequence


SAMPLE_RATE_HZ = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _read_pcm16_wav(path: Path) -> bytes:
    try:
        with wave.open(str(path), "rb") as handle:
            actual = (
                handle.getframerate(),
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.getcomptype(),
            )
            expected = (SAMPLE_RATE_HZ, CHANNELS, SAMPLE_WIDTH_BYTES, "NONE")
            if actual != expected:
                raise ValueError(
                    "source WAV must be 16 kHz mono PCM16; "
                    f"observed {actual!r}"
                )
            return handle.readframes(handle.getnframes())
    except wave.Error as exc:
        raise ValueError(f"invalid source WAV: {exc}") from exc


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(pcm)
    os.chmod(path, 0o600)


def _write_private(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _int(value: Any, field: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} must be an integer at least {minimum}")
    return value


def _number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    return float(value)


def _normalized_segments(report: dict[str, Any], pcm_bytes: int) -> list[dict[str, Any]]:
    summary = report.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("stage report summary is missing")
    if summary.get("success") is not True or summary.get("input_completed") is not True:
        raise ValueError("stage replay did not complete successfully")
    if _int(summary.get("pcm_bytes"), "summary.pcm_bytes") != pcm_bytes:
        raise ValueError("translated PCM size does not match the stage report")

    records = report.get("segments")
    if not isinstance(records, list) or not records:
        raise ValueError("stage report has no synthesized segments")
    normalized: list[dict[str, Any]] = []
    byte_cursor = 0
    for expected_id, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError("stage segment must be an object")
        private = record.get("private_stage_text")
        if private is None:
            # Schema-2 atomic records already embed their translation.
            private = record.get("translation")
        if not isinstance(private, dict):
            raise ValueError(
                "stage report lacks private text; rerun staged_pipeline_smoke.py "
                "with --private-stage-trace"
            )
        source = private.get("segment")
        if not isinstance(source, dict):
            raise ValueError("private stage translation lacks its source segment")
        parent_id = _int(
            record.get("parent_sequence_id", private.get("parent_sequence_id")),
            "parent_sequence_id",
        )
        if parent_id != expected_id:
            raise ValueError("this diagnostic requires contiguous parent order")
        if _int(source.get("sequence_id"), "source.sequence_id") != parent_id:
            raise ValueError("source and TTS parent identities do not match")
        audio_bytes = _int(
            record.get("audio_bytes"), f"segment[{parent_id}].audio_bytes", 1
        )
        if audio_bytes % SAMPLE_WIDTH_BYTES:
            raise ValueError("TTS audio is not PCM16 sample aligned")
        source_start = _number(source.get("source_start_ms"), "source_start_ms")
        source_end = _number(source.get("source_end_ms"), "source_end_ms")
        if source_end < source_start:
            raise ValueError("source segment range is reversed")
        normalized.append(
            {
                "parent_sequence_id": parent_id,
                "source_start_ms": source_start,
                "source_end_ms": source_end,
                "emission_reason": source.get("reason"),
                "contributing_final_ids": source.get("contributing_final_ids"),
                "asr_source_text": source.get("text"),
                "nmt_translated_text": private.get("text"),
                "nmt_retry_count": private.get("retry_count"),
                "nmt_source_override_applied": private.get(
                    "source_override_applied"
                ),
                "tts_audio_duration_ms": record.get("audio_duration_ms"),
                "tts_first_audio_latency_ms": record.get(
                    "first_audio_latency_ms"
                ),
                "tts_processing_duration_ms": record.get(
                    "processing_duration_ms"
                ),
                "tts_retry_count": record.get("retry_count"),
                "translated_sample_start": byte_cursor // SAMPLE_WIDTH_BYTES,
                "translated_sample_end_exclusive": (
                    byte_cursor + audio_bytes
                ) // SAMPLE_WIDTH_BYTES,
            }
        )
        byte_cursor += audio_bytes
    if byte_cursor != pcm_bytes:
        raise ValueError("per-parent TTS byte totals do not reconcile with PCM")
    return normalized


def analyze(
    *,
    source_wav: Path,
    translated_pcm: Path,
    stage_report: Path,
    review_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError("output directory must not already exist")
    source_pcm = _read_pcm16_wav(source_wav)
    translated = translated_pcm.read_bytes()
    if not translated or len(translated) % SAMPLE_WIDTH_BYTES:
        raise ValueError("translated PCM must be non-empty PCM16")
    review = _load_object(review_path, "review")
    report = _load_object(stage_report, "stage report")

    source_hash = hashlib.sha256(source_pcm).hexdigest()
    translated_hash = hashlib.sha256(translated).hexdigest()
    source_samples = len(source_pcm) // SAMPLE_WIDTH_BYTES
    translated_samples = len(translated) // SAMPLE_WIDTH_BYTES
    if review.get("source_pcm_sha256") != source_hash:
        raise ValueError("review is not bound to this source PCM")
    if review.get("source_pcm_sample_count") != source_samples:
        raise ValueError("review source sample count does not match")
    private_binding = report.get("private_stage_trace")
    if not isinstance(private_binding, dict) or private_binding.get("enabled") is not True:
        raise ValueError("stage report is not an explicit private stage trace")
    expected_bindings = {
        "source_pcm_sha256": source_hash,
        "source_pcm_sample_count": source_samples,
        "translated_pcm_sha256": translated_hash,
        "translated_pcm_sample_count": translated_samples,
    }
    for field, expected in expected_bindings.items():
        if private_binding.get(field) != expected:
            raise ValueError(f"private stage trace binding mismatch: {field}")

    parents = _normalized_segments(report, len(translated))
    events = review.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("review has no events")

    output_dir.mkdir(mode=0o700, parents=False)
    os.chmod(output_dir, 0o700)
    event_reports = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("review event must be an object")
        window = event.get("presented_source_window")
        if not isinstance(window, dict):
            raise ValueError("review event lacks a source window")
        start = _int(window.get("start_sample"), "start_sample")
        end = _int(window.get("end_sample_exclusive"), "end_sample_exclusive", 1)
        if start >= end or end > source_samples:
            raise ValueError("review source window is invalid")
        start_ms = start / SAMPLE_RATE_HZ * 1_000
        end_ms = end / SAMPLE_RATE_HZ * 1_000
        selected = [
            parent
            for parent in parents
            if parent["source_end_ms"] > start_ms
            and parent["source_start_ms"] < end_ms
        ]
        if not selected:
            raise ValueError("no fresh replay parent overlaps a review source window")
        envelope_start_ms = min(parent["source_start_ms"] for parent in selected)
        envelope_end_ms = max(parent["source_end_ms"] for parent in selected)
        envelope_start = max(
            0, int(envelope_start_ms * SAMPLE_RATE_HZ // 1_000)
        )
        envelope_end = min(
            source_samples,
            int(envelope_end_ms * SAMPLE_RATE_HZ / 1_000 + 0.999999),
        )
        translated_excerpt = b"".join(
            translated[
                parent["translated_sample_start"] * SAMPLE_WIDTH_BYTES :
                parent["translated_sample_end_exclusive"] * SAMPLE_WIDTH_BYTES
            ]
            for parent in selected
        )
        event_id = str(event.get("event_id", "")).strip()
        if not event_id or not event_id.replace("-", "").isalnum():
            raise ValueError("review event_id is unsafe for an artifact name")
        _write_wav(
            output_dir / f"{event_id}-source.wav",
            source_pcm[start * SAMPLE_WIDTH_BYTES : end * SAMPLE_WIDTH_BYTES],
        )
        _write_wav(
            output_dir / f"{event_id}-source-fresh-parent-envelope.wav",
            source_pcm[
                envelope_start * SAMPLE_WIDTH_BYTES :
                envelope_end * SAMPLE_WIDTH_BYTES
            ],
        )
        _write_wav(
            output_dir / f"{event_id}-translated-fresh-parents.wav",
            translated_excerpt,
        )
        event_reports.append(
            {
                "event_id": event_id,
                "original_review_ratings": {
                    "meaning_preservation": event.get("meaning_preservation"),
                    "spanish_intelligibility": event.get("spanish_intelligibility"),
                    "spanish_naturalness": event.get("spanish_naturalness"),
                },
                "source_window": {
                    "start_sample": start,
                    "end_sample_exclusive": end,
                    "start_seconds": start / SAMPLE_RATE_HZ,
                    "end_seconds": end / SAMPLE_RATE_HZ,
                },
                "fresh_replay_parent_ids": [
                    parent["parent_sequence_id"] for parent in selected
                ],
                "fresh_replay_source_parent_envelope": {
                    "start_sample": envelope_start,
                    "end_sample_exclusive": envelope_end,
                    "start_seconds": envelope_start / SAMPLE_RATE_HZ,
                    "end_seconds": envelope_end / SAMPLE_RATE_HZ,
                    "extra_prefix_seconds_vs_review_window": max(
                        0.0, start_ms - envelope_start_ms
                    ) / 1_000,
                    "extra_suffix_seconds_vs_review_window": max(
                        0.0, envelope_end_ms - end_ms
                    ) / 1_000,
                    "review_prefix_without_parent_seconds": max(
                        0.0, envelope_start_ms - start_ms
                    ) / 1_000,
                    "review_suffix_without_parent_seconds": max(
                        0.0, end_ms - envelope_end_ms
                    ) / 1_000,
                },
                "fresh_replay_parents": selected,
                "fresh_translated_excerpt_sample_count": (
                    len(translated_excerpt) // SAMPLE_WIDTH_BYTES
                ),
            }
        )

    result = {
        "schema_version": 1,
        "report_type": "private_stage_quality_isolation",
        "interpretation_boundary": {
            "review_ratings_describe_original_frozen_translation": True,
            "stage_text_and_excerpt_describe_fresh_replay": True,
            "fresh_replay_is_post_hoc_proof_of_original_failure": False,
            "manual_stage_comparison_required": True,
        },
        "bindings": {
            "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            "stage_report_sha256": hashlib.sha256(stage_report.read_bytes()).hexdigest(),
            "source_pcm_sha256": source_hash,
            "translated_fresh_pcm_sha256": translated_hash,
        },
        "events": event_reports,
        "triage_instructions": [
            "Compare each source WAV with asr_source_text; a material mismatch implicates ASR or segmentation.",
            "If English is faithful, compare it with nmt_translated_text; a meaning error implicates NMT or segment context.",
            "If both texts are faithful, compare Spanish text with translated audio; poor intelligibility implicates TTS.",
        ],
        "privacy": {
            "contains_transcript_or_translation_text": True,
            "contains_audio": False,
            "contains_audio_hashes": True,
            "contains_reviewer_identity": False,
            "private_diagnostic_artifact": True,
        },
    }
    _write_private(
        output_dir / "private-stage-quality.json",
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
    )
    lines = [
        "# Private stage-quality isolation",
        "",
        "> Private: contains English ASR and Spanish NMT text. Do not commit or share externally.",
        "",
        "The ratings came from the frozen reviewed output. The stage records and translated excerpts came from a fresh replay of the identical source PCM; they diagnose reproducibility but are not proof of the exact original failure.",
        "",
    ]
    for event in event_reports:
        ratings = event["original_review_ratings"]
        lines.extend(
            [
                f"## {event['event_id']}",
                "",
                f"Original ratings: meaning `{ratings['meaning_preservation']}`, intelligibility `{ratings['spanish_intelligibility']}`, naturalness `{ratings['spanish_naturalness']}`.",
                "",
                (
                    "For an aligned fresh comparison, use "
                    f"`{event['event_id']}-source-fresh-parent-envelope.wav` "
                    "with the translated fresh-parent WAV. The shorter "
                    "`source.wav` reproduces the original fixed review window "
                    "and may not contain the same boundary context."
                ),
                "",
            ]
        )
        for parent in event["fresh_replay_parents"]:
            lines.extend(
                [
                    f"Parent {parent['parent_sequence_id']} ({parent['source_start_ms'] / 1000:.2f}-{parent['source_end_ms'] / 1000:.2f}s):",
                    "",
                    f"- ASR: {parent['asr_source_text']}",
                    f"- NMT: {parent['nmt_translated_text']}",
                    f"- TTS: {parent['tts_audio_duration_ms'] / 1000:.3f}s audio; first audio {parent['tts_first_audio_latency_ms']:.1f}ms",
                    "",
                ]
            )
    lines.extend(
        [
            "## Manual decision",
            "",
            "1. Source audio vs ASR text: material mismatch => ASR/segmentation.",
            "2. Faithful English vs Spanish text: meaning mismatch => NMT/context.",
            "3. Faithful Spanish text vs Spanish audio: intelligibility mismatch => TTS.",
            "",
        ]
    )
    _write_private(output_dir / "private-stage-quality.md", "\n".join(lines))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build private source-window ASR/NMT/TTS isolation evidence"
    )
    parser.add_argument("--source-wav", type=Path, required=True)
    parser.add_argument("--translated-pcm", type=Path, required=True)
    parser.add_argument("--stage-report", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = analyze(
            source_wav=args.source_wav,
            translated_pcm=args.translated_pcm,
            stage_report=args.stage_report,
            review_path=args.review,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(f"Private stage-quality analysis failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"Private stage-quality evidence created for {len(result['events'])} windows."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
