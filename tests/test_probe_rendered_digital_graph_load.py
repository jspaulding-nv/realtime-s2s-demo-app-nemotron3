import threading

import pytest

import probe_rendered_digital_graph_load as probe


def test_default_arguments_compare_registered_and_prior_frame_sizes():
    args = probe.parse_args([])

    assert args.frame_ms == [100, 500]
    assert args.repeats == 5
    assert args.probe_seconds == 6
    assert args.burst_at_seconds == 2
    assert args.burst_audio_seconds == 30


@pytest.mark.parametrize(
    "arguments",
    [
        ["--frame-ms", "0"],
        ["--frame-ms", "5001"],
        ["--repeats", "0"],
        ["--probe-seconds", "2.5", "--burst-at-seconds", "2"],
        ["--probe-seconds", "76"],
        ["--burst-audio-seconds", "61"],
        ["--burst-audio-seconds", "0.000000000001"],
        ["--repeats", "11"],
        ["--frame-ms", "1"],
        [
            "--frame-ms",
            "100",
            "200",
            "300",
            "400",
            "500",
            "600",
            "700",
            "800",
            "900",
        ],
        [
            "--frame-ms",
            "100",
            "200",
            "300",
            "400",
            "500",
            "600",
            "700",
            "800",
            "--repeats",
            "10",
            "--probe-seconds",
            "75",
            "--burst-at-seconds",
            "2",
        ],
    ],
)
def test_invalid_probe_bounds_fail_during_argument_parsing(arguments):
    with pytest.raises(SystemExit):
        probe.parse_args(arguments)


def test_result_sanitizer_recomputes_gap_and_drops_arbitrary_content():
    result = probe.sanitize_probe_result(
        {
            "audioContextSampleRateHz": 16000,
            "captureStartContextFrame": 384,
            "sourceStartContextFrame": 4480,
            "tickCount": 5,
            "translatedSourcesScheduled": 300,
            "wallElapsedMs": 6000.5,
            "contextElapsedMs": 6016,
            "clockRate": 1.0025,
            "messageCounts": {
                "capture_error": 1,
                "private-message-type": 999,
            },
            "captureErrors": [
                {
                    "code": "noncontiguous_render_quantum",
                    "expectedContextFrame": 32768,
                    "observedContextFrame": 32640,
                    "deltaFrames": 999,
                    "elapsedClientMs": 2021.5,
                    "privateText": "must-not-appear",
                },
                {
                    "code": "arbitrary private code",
                    "expectedContextFrame": "private",
                    "observedContextFrame": True,
                    "elapsedClientMs": float("nan"),
                },
            ],
            "transcript": "must-not-appear",
            "audio": b"must-not-appear",
        }
    )

    assert result == {
        "audio_context_sample_rate_hz": 16000,
        "capture_start_context_frame": 384,
        "source_start_context_frame": 4480,
        "source_tick_count": 5,
        "translated_sources_scheduled": 300,
        "wall_elapsed_ms": 6000.5,
        "context_elapsed_ms": 6016.0,
        "clock_rate": 1.0025,
        "message_counts": {"capture_error": 1},
        "capture_errors": [
            {
                "code": "noncontiguous_render_quantum",
                "expected_context_frame": 32768,
                "observed_context_frame": 32640,
                "delta_frames": -128,
                "elapsed_client_ms": 2021.5,
            },
            {
                "code": "unknown",
                "expected_context_frame": None,
                "observed_context_frame": None,
                "delta_frames": None,
                "elapsed_client_ms": None,
            },
        ],
    }
    assert "must-not-appear" not in repr(result)


def test_probe_expression_uses_only_validated_numeric_substitutions():
    expression = probe._probe_expression(
        publication_frame_ms=500,
        burst_audio_seconds=30,
        burst_at_seconds=2,
        probe_seconds=6,
    )

    assert "__" not in expression
    assert "const publicationFrameMs = 500;" in expression
    assert "const burstAudioSeconds = 30;" in expression
    assert "const burstAtSeconds = 2;" in expression
    assert "const probeSeconds = 6;" in expression


def test_registered_worklet_bytes_match_approved_digest(tmp_path):
    worklet_bytes, digest = probe._registered_worklet_bytes(
        probe.WORKLET_PATH
    )

    assert worklet_bytes == probe.WORKLET_PATH.read_bytes()
    assert digest == probe.APPROVED_WORKLET_MODULE_SHA256

    changed = tmp_path / "changed.worklet.js"
    changed.write_bytes(worklet_bytes + b"\n")
    with pytest.raises(RuntimeError, match="registered digest"):
        probe._registered_worklet_bytes(changed)


def test_probe_validation_requires_complete_lifecycle_and_workload():
    result = probe.sanitize_probe_result(
        {
            "audioContextSampleRateHz": 16000,
            "captureStartContextFrame": 384,
            "sourceStartContextFrame": 4480,
            "tickCount": 20,
            "translatedSourcesScheduled": 60,
            "wallElapsedMs": 6000,
            "contextElapsedMs": 6003,
            "clockRate": 1.0005,
            "messageCounts": {
                "ready": 1,
                "source_clock_armed": 1,
                "started": 1,
                "stopped": 1,
                "pcm_block": 13,
            },
            "captureErrors": [],
        }
    )

    assert probe.validate_probe_result(
        result,
        expected_translated_sources=60,
        minimum_pcm_blocks=10,
        minimum_source_ticks=18,
    ) == []

    result["translated_sources_scheduled"] = 0
    result["source_tick_count"] = 0
    result["clock_rate"] = None
    result["message_counts"] = {"ready": 1}
    assert probe.validate_probe_result(
        result,
        expected_translated_sources=60,
        minimum_pcm_blocks=10,
        minimum_source_ticks=18,
    ) == [
        "translated_source_count",
        "source_tick_count",
        "clock_rate",
        "message_lifecycle",
        "pcm_block_count",
    ]


def test_full_source_probe_requires_every_tick_and_registered_sample_rate():
    assert probe._minimum_source_tick_count(65) == 200

    result = probe.sanitize_probe_result(
        {
            "audioContextSampleRateHz": 48000,
            "captureStartContextFrame": 384,
            "sourceStartContextFrame": 4480,
            "tickCount": 200,
            "translatedSourcesScheduled": 60,
            "wallElapsedMs": 65000,
            "contextElapsedMs": 65000,
            "clockRate": 1,
            "messageCounts": {
                "ready": 1,
                "source_clock_armed": 1,
                "started": 1,
                "stopped": 1,
                "pcm_block": 130,
            },
            "captureErrors": [],
        }
    )
    assert probe.validate_probe_result(
        result,
        expected_translated_sources=60,
        minimum_pcm_blocks=127,
        minimum_source_ticks=200,
    ) == ["audio_context_sample_rate"]


def test_browser_product_is_projected_onto_fixed_schema():
    assert probe._sanitize_browser_product(
        "HeadlessChrome/138.0.7204.49"
    ) == {
        "kind": "headless_chrome",
        "version": "138.0.7204.49",
    }
    assert probe._sanitize_browser_product(
        "private browser build /home/person"
    ) == {"kind": "unknown", "version": None}


def test_popen_failure_cleans_server_and_temporary_directories(
    monkeypatch,
    tmp_path,
):
    stopped = threading.Event()

    class FakeServer:
        server_port = 1
        shutdown_called = False
        close_called = False

        def serve_forever(self):
            stopped.wait(timeout=5)

        def shutdown(self):
            self.shutdown_called = True
            stopped.set()

        def server_close(self):
            self.close_called = True

    server = FakeServer()
    created = []

    def fake_mkdtemp(*, prefix):
        path = tmp_path / f"{prefix}{len(created)}"
        path.mkdir()
        created.append(path)
        return str(path)

    monkeypatch.setattr(
        probe,
        "ThreadingHTTPServer",
        lambda *_args, **_kwargs: server,
    )
    monkeypatch.setattr(probe.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(
        probe.preflight,
        "find_chrome",
        lambda _path: tmp_path / "chrome",
    )
    monkeypatch.setattr(
        probe.preflight,
        "sha256_file",
        lambda _path: "a" * 64,
    )
    monkeypatch.setattr(
        probe.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("synthetic launch failure")
        ),
    )

    with pytest.raises(OSError, match="synthetic launch failure"):
        probe.run(probe.parse_args(["--frame-ms", "500", "--repeats", "1"]))

    assert server.shutdown_called is True
    assert server.close_called is True
    assert all(not path.exists() for path in created)
