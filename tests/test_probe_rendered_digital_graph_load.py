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
    assert args.trace_rewind is False


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
                "main_context_frame_at_delivery_before": None,
                "main_context_frame_at_delivery_after": None,
                "main_context_state_at_delivery": "unknown",
            },
            {
                "code": "unknown",
                "expected_context_frame": None,
                "observed_context_frame": None,
                "delta_frames": None,
                "elapsed_client_ms": None,
                "main_context_frame_at_delivery_before": None,
                "main_context_frame_at_delivery_after": None,
                "main_context_state_at_delivery": "unknown",
            },
        ],
        "shadow_trace": {
            "status": "unknown",
            "first_anomaly_ordinal": None,
            "first_expected_context_frame": None,
            "first_observed_context_frame": None,
            "trace_entry_count": 0,
            "trace": [],
        },
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
    assert "const sourceFrameCount = 960000;" in expression
    assert "const traceRewind = false;" in expression

    traced = probe._probe_expression(
        publication_frame_ms=500,
        burst_audio_seconds=30,
        burst_at_seconds=2,
        probe_seconds=6,
        trace_rewind=True,
    )
    assert "const traceRewind = true;" in traced
    assert "shadow-worklet.js" in traced
    assert "translated.connect(shadowNode" not in traced
    assert "numberOfInputs: 1" in traced


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


def _shadow_trace(
    *,
    catch_up=True,
    anomaly_source_step=128,
    post_source_steps=None,
):
    entries = []
    decoded = None
    for ordinal in range(6, 10):
        logical = 10_000 + ordinal * 128
        decoded = ordinal * 128
        entries.append({
            "callback_ordinal": ordinal,
            "frame_count": 128,
            "expected_context_frame": logical,
            "observed_context_frame": logical,
            "logical_context_frame": logical,
            "axis_offset_frames": 0,
            "step_frames": 128,
            "worklet_current_time_frame": logical,
            "decoded_source_frame": decoded,
            "source_step_frames": 128,
        })
    anomaly_logical = 10_000 + 10 * 128
    decoded += anomaly_source_step
    entries.append({
        "callback_ordinal": 10,
        "frame_count": 128,
        "expected_context_frame": anomaly_logical,
        "observed_context_frame": anomaly_logical - 128,
        "logical_context_frame": anomaly_logical,
        "axis_offset_frames": -128,
        "step_frames": 0,
        "worklet_current_time_frame": anomaly_logical - 128,
        "decoded_source_frame": decoded,
        "source_step_frames": anomaly_source_step,
    })
    if post_source_steps is None:
        post_source_steps = [128] * probe.SHADOW_TRACE_AFTER
    assert len(post_source_steps) == probe.SHADOW_TRACE_AFTER
    previous_observed = anomaly_logical - 128
    for ordinal, source_step in zip(
        range(11, 11 + probe.SHADOW_TRACE_AFTER),
        post_source_steps,
        strict=True,
    ):
        logical = 10_000 + ordinal * 128
        offset = 0 if catch_up else -128
        observed = logical + offset
        decoded += source_step
        entries.append({
            "callback_ordinal": ordinal,
            "frame_count": 128,
            "expected_context_frame": previous_observed + 128,
            "observed_context_frame": observed,
            "logical_context_frame": logical,
            "axis_offset_frames": offset,
            "step_frames": (
                256 if catch_up and ordinal == 11 else 128
            ),
            "worklet_current_time_frame": logical + offset,
            "decoded_source_frame": decoded,
            "source_step_frames": source_step,
        })
        previous_observed = observed
    return {
        "status": "anomaly",
        "first_anomaly_ordinal": 10,
        "first_expected_context_frame": anomaly_logical,
        "first_observed_context_frame": anomaly_logical - 128,
        "trace_entry_count": len(entries),
        "trace": entries,
    }


@pytest.mark.parametrize(
    (
        "catch_up",
        "anomaly_source_step",
        "clock_behavior",
        "media_behavior",
    ),
    [
        (
            True,
            128,
            "repeated_frame_then_catch_up",
            "samples_contiguous",
        ),
        (
            False,
            128,
            "sustained_one_quantum_offset_within_trace",
            "samples_contiguous",
        ),
        (
            True,
            0,
            "repeated_frame_then_catch_up",
            "samples_duplicated",
        ),
        (
            True,
            256,
            "repeated_frame_then_catch_up",
            "samples_dropped",
        ),
    ],
)
def test_shadow_classifier_distinguishes_clock_and_media_behavior(
    catch_up,
    anomaly_source_step,
    clock_behavior,
    media_behavior,
):
    assert probe.classify_shadow_trace(
        _shadow_trace(
            catch_up=catch_up,
            anomaly_source_step=anomaly_source_step,
        )
    ) == {
        "clock_behavior": clock_behavior,
        "main_clock_relation": "evaluated_at_message_delivery",
        "media_behavior": media_behavior,
    }


@pytest.mark.parametrize(
    ("post_source_step", "media_behavior"),
    [
        (0, "samples_duplicated"),
        (256, "samples_dropped"),
    ],
)
def test_shadow_classifier_checks_post_anomaly_pcm_steps(
    post_source_step,
    media_behavior,
):
    steps = [128] * probe.SHADOW_TRACE_AFTER
    steps[3] = post_source_step
    trace = _shadow_trace(post_source_steps=steps)

    assert probe.classify_shadow_trace(trace)["media_behavior"] == (
        media_behavior
    )


def test_shadow_sanitizer_bounds_trace_and_drops_arbitrary_content():
    raw_trace = []
    for ordinal in range(probe.SHADOW_TRACE_LIMIT + 20):
        raw_trace.append({
            "callbackOrdinal": ordinal,
            "frameCount": 128,
            "expectedContextFrame": 1000 + ordinal * 128,
            "observedContextFrame": 1000 + ordinal * 128,
            "logicalContextFrame": 1000 + ordinal * 128,
            "axisOffsetFrames": 0,
            "stepFrames": 128,
            "workletCurrentTimeFrame": 1000 + ordinal * 128,
            "decodedSourceFrame": ordinal * 128,
            "sourceStepFrames": 128,
            "privateText": "must-not-appear",
        })
    sanitized = probe.sanitize_probe_result({
        "shadowTrace": {
            "status": "anomaly",
            "firstAnomalyOrdinal": 4,
            "firstExpectedContextFrame": 1512,
            "firstObservedContextFrame": 1384,
            "trace": raw_trace,
            "transcript": "must-not-appear",
        },
    })["shadow_trace"]

    assert sanitized["status"] == "anomaly"
    assert sanitized["trace_entry_count"] == (
        probe.SHADOW_TRACE_LIMIT + 1
    )
    assert len(sanitized["trace"]) == probe.SHADOW_TRACE_LIMIT
    assert probe.validate_shadow_trace(sanitized) == [
        "shadow_trace_incomplete"
    ]
    assert "must-not-appear" not in repr(sanitized)


def test_shadow_trace_requires_complete_contiguous_arithmetic():
    complete = _shadow_trace()
    assert probe.validate_shadow_trace(complete) == []

    incomplete = {**complete, "trace": complete["trace"][:-1]}
    assert probe.validate_shadow_trace(incomplete) == [
        "shadow_trace_incomplete"
    ]

    malformed = _shadow_trace()
    malformed["trace"][8]["source_step_frames"] = 0
    assert "shadow_trace_arithmetic" in probe.validate_shadow_trace(
        malformed
    )
    assert probe.classify_shadow_trace(malformed)["clock_behavior"] == (
        "unresolved"
    )


def test_rewind_diagnostic_reconciles_exact_and_shadow_clocks():
    shadow = _shadow_trace()
    result = {
        "capture_errors": [{
            "code": "noncontiguous_render_quantum",
            "expected_context_frame": shadow[
                "first_expected_context_frame"
            ],
            "observed_context_frame": shadow[
                "first_observed_context_frame"
            ],
            "main_context_frame_at_delivery_before": shadow[
                "first_expected_context_frame"
            ],
            "main_context_frame_at_delivery_after": shadow[
                "first_expected_context_frame"
            ] + 128,
            "main_context_state_at_delivery": "running",
        }],
        "shadow_trace": shadow,
    }

    diagnostic = probe.evaluate_rewind_diagnostic(result)

    assert diagnostic["status"] == "traced"
    assert diagnostic["failure_codes"] == []
    assert diagnostic["classification"] == {
        "clock_behavior": "repeated_frame_then_catch_up",
        "main_clock_relation": (
            "running_at_or_past_expected_when_error_delivered"
        ),
        "media_behavior": "samples_contiguous",
    }

    result["capture_errors"][0]["observed_context_frame"] += 128
    invalid = probe.evaluate_rewind_diagnostic(result)
    assert invalid["status"] == "invalid"
    assert "exact_shadow_disagreement" in invalid["failure_codes"]


def test_trace_mode_fails_closed_on_invalid_diagnostic():
    passing_validation = {"validation": {"status": "pass"}}

    assert probe._record_failed(
        passing_validation,
        trace_rewind=False,
    ) is False
    assert probe._record_failed(
        {
            **passing_validation,
            "rewind_diagnostic": {"status": "not_reproduced"},
        },
        trace_rewind=True,
    ) is False
    assert probe._record_failed(
        {
            **passing_validation,
            "rewind_diagnostic": {"status": "invalid"},
        },
        trace_rewind=True,
    ) is True


def test_shadow_worklet_contains_only_generated_marker_diagnostics():
    source = probe.SHADOW_WORKLET_SOURCE.decode("ascii")

    assert "render-clock-shadow" in source
    assert "TRACE_AFTER = 16" in source
    assert "interleavedPcm16" not in source
    assert "transcript" not in source.lower()


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


def test_trace_source_marker_covers_the_full_probe_window():
    for probe_seconds in (60.1, 65, 65.1, 75):
        source_frames = probe._source_frame_count(
            probe_seconds,
            trace_rewind=True,
        )

        assert source_frames >= (probe_seconds + 1) * (
            probe.SAMPLE_RATE_HZ
        )
        assert source_frames % probe.RENDER_QUANTUM_FRAMES == 0
        assert source_frames % probe.SOURCE_CHUNK_FRAMES == 0
    assert probe._minimum_source_tick_count(
        65,
        source_frame_count=probe._source_frame_count(
            65,
            trace_rewind=True,
        ),
    ) > probe.SOURCE_FRAME_COUNT // probe.SOURCE_CHUNK_FRAMES


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
