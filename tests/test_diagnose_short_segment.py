import threading

import pytest

from staged_models import EmissionReason, TextSegment

from diagnose_short_segment import (
    DiagnosticTTSTimeout,
    merge_with_next,
    riva_config,
    safe_audio_metadata,
    safe_service_metadata,
    segment_metadata,
    synthesize_with_deadline,
    text_shape,
)


def segment(sequence_id, text, final_id):
    return TextSegment(
        sequence_id=sequence_id,
        text=text,
        reason=EmissionReason.PUNCTUATION,
        emitted_monotonic_ms=20 + sequence_id,
        buffered_since_monotonic_ms=10 + sequence_id,
        source_start_ms=sequence_id * 100,
        source_end_ms=(sequence_id + 1) * 100,
        contributing_final_ids=(final_id,),
    )


def test_text_shape_is_repeatable_within_key_and_never_contains_plaintext():
    key = b"k" * 32

    first = text_shape("Sí.", correlation_key=key)
    second = text_shape("Sí.", correlation_key=key)
    different = text_shape("No.", correlation_key=key)

    assert first == second
    assert first["correlation_hmac_sha256"] != different[
        "correlation_hmac_sha256"
    ]
    assert first["characters"] == 3
    assert first["letters_or_digits"] == 2
    assert first["script_class_counts"] == {
        "Latin": 2,
        "Punctuation": 1,
    }
    assert "Sí" not in repr(first)


def test_segment_metadata_omits_source_text():
    source = segment(7, "Private source.", 4)

    metadata = segment_metadata(source)

    assert metadata["sequence_id"] == 7
    assert metadata["source_characters"] == len(source.text)
    assert "text" not in metadata
    assert source.text not in repr(metadata)


def test_diagnostic_context_merge_preserves_ordered_provenance():
    current = segment(7, "Short.", 4)
    following = segment(8, "Context.", 5)

    merged = merge_with_next(current, following)

    assert merged.sequence_id == 7
    assert merged.text == "Short. Context."
    assert merged.contributing_final_ids == (4, 5)
    assert merged.source_start_ms == 700
    assert merged.source_end_ms == 900


def test_safe_report_metadata_omits_paths_filenames_and_endpoint_hosts(
    tmp_path,
    monkeypatch,
):
    private = tmp_path / "customer-person-name.wav"
    private.write_bytes(b"audio")
    monkeypatch.setattr(riva_config, "asr_uri", "private-asr.internal:50052")
    monkeypatch.setattr(riva_config, "uri", "private-nmt.internal:50051")
    monkeypatch.setattr(riva_config, "tts_uri", "private-tts.internal:50053")

    audio = safe_audio_metadata(
        private,
        prefix_seconds=1.0,
        pcm_bytes=32_000,
        realtime=True,
    )
    services = safe_service_metadata(
        client_max_retries=0,
        tts_timeout_seconds=60,
    )
    serialized = repr({"audio": audio, "services": services})

    assert str(private) not in serialized
    assert private.name not in serialized
    assert "private-" not in serialized
    assert audio["sha256"]


class BlockingProbeTTS:
    active_retry_count = 1

    def __init__(self):
        self.cancelled = threading.Event()
        self.disconnect_count = 0
        self.connect_count = 0

    def synthesize_segment(self, translation):
        del translation
        self.cancelled.wait(timeout=1)
        raise RuntimeError("cancelled")

    def disconnect(self):
        self.disconnect_count += 1
        self.cancelled.set()

    def connect(self):
        self.connect_count += 1
        return True


def test_tts_probe_timeout_aborts_and_reconnects_before_returning():
    tts = BlockingProbeTTS()

    with pytest.raises(DiagnosticTTSTimeout) as failure:
        synthesize_with_deadline(tts, object(), timeout_s=0.01)

    assert failure.value.retry_count == 1
    assert tts.disconnect_count == 1
    assert tts.connect_count == 1
