from __future__ import annotations

import hashlib
import json
import os
import wave
from pathlib import Path

import pytest

import analyze_private_stage_quality as analyzer


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(pcm)


def _fixture(tmp_path: Path) -> dict[str, Path]:
    source_pcm = b"\x01\x00" * 48_000
    translated_pcm = b"\x02\x00" * 24_000
    source = tmp_path / "source.wav"
    translated = tmp_path / "translated.pcm"
    review = tmp_path / "review.json"
    stage = tmp_path / "stage.json"
    _write_wav(source, source_pcm)
    translated.write_bytes(translated_pcm)

    review.write_text(
        json.dumps(
            {
                "source_pcm_sha256": hashlib.sha256(source_pcm).hexdigest(),
                "source_pcm_sample_count": 48_000,
                "events": [
                    {
                        "event_id": "event-001",
                        "presented_source_window": {
                            "start_sample": 8_000,
                            "end_sample_exclusive": 40_000,
                        },
                        "meaning_preservation": "no",
                        "spanish_intelligibility": "difficult",
                        "spanish_naturalness": "very_unnatural",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    segments = []
    for parent_id, (start_ms, end_ms, text, translation) in enumerate(
        (
            (0.0, 1_000.0, "First sentence.", "Primera frase."),
            (1_000.0, 2_000.0, "Second sentence.", "Segunda frase."),
            (2_000.0, 3_000.0, "Third sentence.", "Tercera frase."),
        )
    ):
        segments.append(
            {
                "parent_sequence_id": parent_id,
                "audio_bytes": 16_000,
                "audio_duration_ms": 500.0,
                "first_audio_latency_ms": 100.0,
                "processing_duration_ms": 200.0,
                "retry_count": 0,
                "private_stage_text": {
                    "parent_sequence_id": parent_id,
                    "text": translation,
                    "retry_count": 0,
                    "source_override_applied": False,
                    "segment": {
                        "sequence_id": parent_id,
                        "text": text,
                        "reason": "punctuation",
                        "source_start_ms": start_ms,
                        "source_end_ms": end_ms,
                        "contributing_final_ids": [parent_id],
                    },
                },
            }
        )
    stage.write_text(
        json.dumps(
            {
                "summary": {
                    "success": True,
                    "input_completed": True,
                    "pcm_bytes": len(translated_pcm),
                },
                "segments": segments,
                "private_stage_trace": {
                    "enabled": True,
                    "source_pcm_sha256": hashlib.sha256(source_pcm).hexdigest(),
                    "source_pcm_sample_count": 48_000,
                    "translated_pcm_sha256": hashlib.sha256(
                        translated_pcm
                    ).hexdigest(),
                    "translated_pcm_sample_count": 24_000,
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "source": source,
        "translated": translated,
        "review": review,
        "stage": stage,
    }


def test_analyze_builds_private_window_evidence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / "private-output"
    result = analyzer.analyze(
        source_wav=fixture["source"],
        translated_pcm=fixture["translated"],
        stage_report=fixture["stage"],
        review_path=fixture["review"],
        output_dir=output,
    )

    event = result["events"][0]
    assert event["fresh_replay_parent_ids"] == [0, 1, 2]
    assert event["fresh_replay_parents"][1]["asr_source_text"] == (
        "Second sentence."
    )
    assert event["fresh_replay_parents"][1]["nmt_translated_text"] == (
        "Segunda frase."
    )
    assert (output / "event-001-source.wav").is_file()
    assert (output / "event-001-source-fresh-parent-envelope.wav").is_file()
    assert (output / "event-001-translated-fresh-parents.wav").is_file()
    envelope = event["fresh_replay_source_parent_envelope"]
    assert envelope["start_seconds"] == 0.0
    assert envelope["end_seconds"] == 3.0
    assert envelope["extra_prefix_seconds_vs_review_window"] == 0.5
    assert envelope["extra_suffix_seconds_vs_review_window"] == 0.5
    assert (output / "private-stage-quality.json").is_file()
    assert (output / "private-stage-quality.md").is_file()
    assert os.stat(output).st_mode & 0o777 == 0o700
    assert all(
        os.stat(path).st_mode & 0o777 == 0o600
        for path in output.iterdir()
    )


def test_analyze_rejects_wrong_source_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    review = json.loads(fixture["review"].read_text(encoding="utf-8"))
    review["source_pcm_sha256"] = "0" * 64
    fixture["review"].write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(ValueError, match="not bound"):
        analyzer.analyze(
            source_wav=fixture["source"],
            translated_pcm=fixture["translated"],
            stage_report=fixture["stage"],
            review_path=fixture["review"],
            output_dir=tmp_path / "private-output",
        )


def test_analyze_requires_explicit_private_trace(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    stage = json.loads(fixture["stage"].read_text(encoding="utf-8"))
    del stage["private_stage_trace"]
    fixture["stage"].write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match="explicit private stage trace"):
        analyzer.analyze(
            source_wav=fixture["source"],
            translated_pcm=fixture["translated"],
            stage_report=fixture["stage"],
            review_path=fixture["review"],
            output_dir=tmp_path / "private-output",
        )
