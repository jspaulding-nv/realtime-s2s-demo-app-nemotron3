import wave

from direct_asr_smoke import WaveAudioChunks


def write_pcm_wav(path, frames=1_600):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * frames)


def test_wave_chunks_report_complete_only_after_requested_input_is_consumed(tmp_path):
    path = tmp_path / "sample.wav"
    write_pcm_wav(path)
    chunks = WaveAudioChunks(
        path,
        chunk_frames=400,
        realtime=False,
        duration_seconds=0.05,
    )

    iterator = iter(chunks)
    assert len(next(iterator)) == 800
    assert chunks.input_completed is False
    iterator.close()

    assert chunks.frames_sent == 400
    assert chunks.expected_frames == 800


def test_wave_chunks_report_complete_after_full_requested_prefix(tmp_path):
    path = tmp_path / "sample.wav"
    write_pcm_wav(path)
    chunks = WaveAudioChunks(
        path,
        chunk_frames=400,
        realtime=False,
        duration_seconds=0.05,
    )

    payloads = list(chunks)

    assert len(payloads) == 2
    assert chunks.frames_sent == 800
    assert chunks.input_completed is True
