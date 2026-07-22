import json

from batch_latency_test import TestResult as BatchTestResult, generate_summary


def test_generate_summary_records_capture_integrity(tmp_path):
    result = BatchTestResult(
        audio_path="test_audio/example.mp3",
        duration_sec=60.0,
        chunks_sent=200,
        audio_responses=12,
        input_completed=True,
        connection_lost=False,
        drain_timed_out=False,
        drain_duration_sec=13.25,
        translation_completed=True,
        server_error="",
    )
    output = tmp_path / "example_summary.json"

    generate_summary(result, str(output))

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["input_completed"] is True
    assert summary["connection_lost"] is False
    assert summary["drain_timed_out"] is False
    assert summary["drain_duration_sec"] == 13.25
    assert summary["translation_completed"] is True
    assert summary["server_error"] == ""
