import asyncio
import wave

import direct_asr_bridge_smoke
from direct_asr_bridge_smoke import (
    build_parser,
    main,
    terminal_deadline_seconds,
)
from staged_models import ASRStreamEvent, ASRStreamEventKind, ASRTranscript


def test_bridge_smoke_defaults_to_bounded_four_event_queue():
    args = build_parser().parse_args([])

    assert args.event_queue_maxsize == 4
    assert args.duration_seconds == 20.0
    assert args.event_timeout_seconds == 30.0
    assert terminal_deadline_seconds(
        args.duration_seconds,
        realtime=True,
        terminal_grace_seconds=args.event_timeout_seconds,
    ) == 50.0
    assert terminal_deadline_seconds(
        args.duration_seconds,
        realtime=False,
        terminal_grace_seconds=args.event_timeout_seconds,
    ) == 30.0


def test_bridge_smoke_rejects_missing_audio_before_connecting(tmp_path):
    missing = tmp_path / "missing.wav"

    assert main(["--file", str(missing)]) == 2


def test_recurring_interims_cannot_extend_overall_terminal_deadline(
    tmp_path,
    monkeypatch,
):
    audio = tmp_path / "short.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 160)

    class EndlessInterimStream:
        def add_chunk(self, chunk):
            del chunk

        def finish_input(self):
            pass

        async def next_event(self):
            await asyncio.sleep(0)
            return ASRStreamEvent(
                kind=ASRStreamEventKind.INTERIM,
                transcript=ASRTranscript(
                    text="still working",
                    is_final=False,
                    received_monotonic_ms=1,
                ),
            )

    class FakeClient:
        def __init__(self, uri):
            del uri

        def connect(self):
            return True

        async def open_stream(self, event_queue_maxsize):
            del event_queue_maxsize
            return EndlessInterimStream()

        async def aclose(self, timeout_s):
            del timeout_s

    monkeypatch.setattr(direct_asr_bridge_smoke, "DirectASRClient", FakeClient)

    assert main(
        [
            "--file",
            str(audio),
            "--fast",
            "--event-timeout-seconds",
            "0.05",
            "--quiet",
        ]
    ) == 1
