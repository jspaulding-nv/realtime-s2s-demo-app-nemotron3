#!/usr/bin/env python3
"""Probe Chrome AudioWorklet continuity under synthetic playback graph load.

This diagnostic serves the repository's exact recorder worklet to localhost,
creates no Riva connections, and uses only generated PCM. Its stdout schema is
limited to frame counters, clock rates, message counts, and allowlisted
recorder codes.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.request

from analyze_rendered_digital_preflight import (
    APPROVED_WORKLET_MODULE_SHA256,
)
import run_rendered_digital_preflight as preflight


REPOSITORY_ROOT = Path(__file__).resolve().parent
WORKLET_PATH = (
    REPOSITORY_ROOT
    / "frontend"
    / "public"
    / "rendered-digital-recorder.worklet.js"
)
SAMPLE_RATE_HZ = 16_000
SOURCE_FRAME_COUNT = 960_000
SOURCE_CHUNK_FRAMES = 4_800
CAPTURE_BLOCK_FRAMES = 8_000
SAFE_INTEGER_MAX = (1 << 53) - 1
MIN_CLOCK_RATE = 0.99
MAX_CLOCK_RATE = 1.01
MAX_FRAME_CANDIDATES = 8
MAX_REPEATS = 10
MAX_TOTAL_PROBE_SECONDS = 900
MIN_TRANSLATED_SOURCES_PER_REPEAT = 10
MAX_TRANSLATED_SOURCES_PER_REPEAT = 2_000
MAX_TOTAL_TRANSLATED_SOURCES = 10_000
BROWSER_PRODUCT_PATTERN = re.compile(
    r"(HeadlessChrome|Chrome|Chromium)/"
    r"([0-9]+(?:\.[0-9]+){0,3})\Z"
)
SAFE_CAPTURE_CODES = frozenset(
    {
        "capture_frame_limit_exceeded",
        "capture_started_after_source",
        "invalid_source_clock",
        "noncontiguous_render_quantum",
        "unknown",
    }
)
SAFE_MESSAGE_TYPES = frozenset(
    {
        "capture_error",
        "pcm_block",
        "ready",
        "source_chunk_due",
        "source_clock_armed",
        "started",
        "stopped",
    }
)


PROBE_EXPRESSION = r"""
(async () => {
  const sampleRateHz = 16000;
  const publicationFrameMs = __PUBLICATION_FRAME_MS__;
  const burstAudioSeconds = __BURST_AUDIO_SECONDS__;
  const burstAtSeconds = __BURST_AT_SECONDS__;
  const probeSeconds = __PROBE_SECONDS__;
  const maximumFrames = __MAXIMUM_CAPTURE_FRAMES__;
  const context = new AudioContext({
    sampleRate: sampleRateHz,
    latencyHint: 'interactive',
  });
  if (context.state === 'suspended') await context.resume();
  await context.audioWorklet.addModule('/worklet.js');
  const node = new AudioWorkletNode(
    context,
    'rendered-digital-recorder',
    {
      numberOfInputs: 2,
      numberOfOutputs: 1,
      outputChannelCount: [2],
      channelCount: 1,
      channelCountMode: 'explicit',
      channelInterpretation: 'discrete',
      processorOptions: {
        chunkFrames: 8000,
        maximumFrames,
      },
    },
  );
  const sink = context.createGain();
  sink.gain.value = 1;
  node.connect(sink);
  sink.connect(context.destination);

  const result = {
    audioContextSampleRateHz: context.sampleRate,
    captureErrors: [],
    captureStartContextFrame: null,
    clockRate: null,
    contextElapsedMs: null,
    firstTickClientMs: null,
    lastTickClientMs: null,
    messageCounts: {},
    sourceStartContextFrame: null,
    tickCount: 0,
    translatedSourcesScheduled: 0,
    wallElapsedMs: null,
  };
  let readyResolve;
  let startedResolve;
  let stoppedResolve;
  let wallStart = performance.now();
  const ready = new Promise(resolve => { readyResolve = resolve; });
  const started = new Promise(resolve => { startedResolve = resolve; });
  const stopped = new Promise(resolve => { stoppedResolve = resolve; });
  node.port.onmessage = ({data}) => {
    const type = typeof data?.type === 'string' ? data.type : 'unknown';
    result.messageCounts[type] = (result.messageCounts[type] || 0) + 1;
    if (type === 'ready') {
      readyResolve();
    } else if (type === 'started') {
      result.captureStartContextFrame = data.captureStartContextFrame;
      startedResolve();
    } else if (type === 'source_chunk_due') {
      const now = performance.now();
      if (result.firstTickClientMs === null) {
        result.firstTickClientMs = now;
      }
      result.lastTickClientMs = now;
      result.tickCount += 1;
    } else if (type === 'capture_error') {
      const rawExpected = (
        data.code === 'capture_started_after_source'
          ? data.sourceStartContextFrame
          : data.expectedContextFrame
      );
      const expected = Number.isSafeInteger(rawExpected)
        ? rawExpected
        : null;
      const observed = Number.isSafeInteger(data.observedContextFrame)
        ? data.observedContextFrame
        : null;
      result.captureErrors.push({
        code: typeof data.code === 'string' ? data.code : 'unknown',
        expectedContextFrame: expected,
        observedContextFrame: observed,
        deltaFrames: (
          expected !== null && observed !== null
            ? observed - expected
            : null
        ),
        elapsedClientMs: performance.now() - wallStart,
      });
      startedResolve();
      stoppedResolve();
    } else if (type === 'stopped') {
      stoppedResolve();
    }
  };

  await ready;
  const sourceStartContextFrame = Math.ceil(
    (context.currentTime + 0.25) * sampleRateHz / 128
  ) * 128;
  result.sourceStartContextFrame = sourceStartContextFrame;
  const monitor = context.createGain();
  monitor.gain.value = 0;
  monitor.connect(context.destination);
  const sourceBuffer = context.createBuffer(
    1,
    960000,
    sampleRateHz,
  );
  const source = context.createBufferSource();
  source.buffer = sourceBuffer;
  source.connect(monitor);
  source.connect(node, 0, 0);
  source.start(sourceStartContextFrame / sampleRateHz);
  node.port.postMessage({
    type: 'arm_source_clock',
    sourceStartContextFrame,
    sourceFrameCount: 960000,
    sourceChunkFrames: 4800,
  });
  await started;

  wallStart = performance.now();
  const contextStart = context.currentTime;
  await new Promise(resolve => {
    setTimeout(resolve, burstAtSeconds * 1000);
  });
  const publicationFrameCount = Math.round(
    publicationFrameMs * sampleRateHz / 1000
  );
  const totalTranslatedFrames = Math.round(
    burstAudioSeconds * sampleRateHz
  );
  let translatedStart = Math.max(
    context.currentTime + 0.25,
    sourceStartContextFrame / sampleRateHz + burstAtSeconds,
  );
  for (
    let offset = 0;
    offset < totalTranslatedFrames;
    offset += publicationFrameCount
  ) {
    const frameCount = Math.min(
      publicationFrameCount,
      totalTranslatedFrames - offset,
    );
    const buffer = context.createBuffer(
      1,
      frameCount,
      sampleRateHz,
    );
    buffer.getChannelData(0).fill(0.1);
    const translated = context.createBufferSource();
    translated.buffer = buffer;
    translated.playbackRate.value = 1.1;
    translated.connect(monitor);
    translated.connect(node, 0, 1);
    translated.start(translatedStart);
    translatedStart += (frameCount / sampleRateHz) / 1.1;
    result.translatedSourcesScheduled += 1;
  }
  await new Promise(resolve => {
    setTimeout(
      resolve,
      (probeSeconds - burstAtSeconds) * 1000,
    );
  });
  const wallEnd = performance.now();
  const contextEnd = context.currentTime;
  node.port.postMessage({type: 'stop'});
  await Promise.race([
    stopped,
    new Promise(resolve => setTimeout(resolve, 1000)),
  ]);
  result.wallElapsedMs = wallEnd - wallStart;
  result.contextElapsedMs = (contextEnd - contextStart) * 1000;
  result.clockRate = result.contextElapsedMs / result.wallElapsedMs;
  await context.close();
  return result;
})()
"""


class ProbeHandler(BaseHTTPRequestHandler):
    """Serve only the diagnostic page and the exact tracked worklet."""

    worklet_bytes = b""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/worklet.js":
            body = self.worklet_bytes
            content_type = "text/javascript"
        else:
            body = b"<!doctype html><title>Recorder graph probe</title>"
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _safe_nonnegative_integer(value: Any) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= SAFE_INTEGER_MAX
    ):
        return value
    return None


def _safe_finite_number(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return float(value)
    return None


def sanitize_probe_result(value: Any) -> dict[str, Any]:
    """Project browser output onto the fixed transcript-free result schema."""

    payload = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    for source_key, output_key in (
        ("audioContextSampleRateHz", "audio_context_sample_rate_hz"),
        ("captureStartContextFrame", "capture_start_context_frame"),
        ("sourceStartContextFrame", "source_start_context_frame"),
        ("tickCount", "source_tick_count"),
        ("translatedSourcesScheduled", "translated_sources_scheduled"),
    ):
        result[output_key] = _safe_nonnegative_integer(
            payload.get(source_key)
        )
    for source_key, output_key in (
        ("wallElapsedMs", "wall_elapsed_ms"),
        ("contextElapsedMs", "context_elapsed_ms"),
        ("clockRate", "clock_rate"),
    ):
        result[output_key] = _safe_finite_number(payload.get(source_key))

    raw_counts = payload.get("messageCounts")
    counts: dict[str, int] = {}
    if isinstance(raw_counts, Mapping):
        for message_type in sorted(SAFE_MESSAGE_TYPES):
            count = _safe_nonnegative_integer(raw_counts.get(message_type))
            if count is not None:
                counts[message_type] = count
    result["message_counts"] = counts

    capture_errors = []
    raw_errors = payload.get("captureErrors")
    if isinstance(raw_errors, list):
        for raw_error in raw_errors[:8]:
            error = raw_error if isinstance(raw_error, Mapping) else {}
            raw_code = error.get("code")
            code = (
                raw_code
                if isinstance(raw_code, str)
                and raw_code in SAFE_CAPTURE_CODES
                else "unknown"
            )
            expected = _safe_nonnegative_integer(
                error.get("expectedContextFrame")
            )
            observed = _safe_nonnegative_integer(
                error.get("observedContextFrame")
            )
            safe_error: dict[str, Any] = {
                "code": code,
                "expected_context_frame": expected,
                "observed_context_frame": observed,
                "delta_frames": (
                    observed - expected
                    if expected is not None and observed is not None
                    else None
                ),
                "elapsed_client_ms": _safe_finite_number(
                    error.get("elapsedClientMs")
                ),
            }
            capture_errors.append(safe_error)
    result["capture_errors"] = capture_errors
    return result


def _translated_source_count(
    *,
    publication_frame_ms: int,
    burst_audio_seconds: float,
) -> int:
    publication_frames = round(
        publication_frame_ms * SAMPLE_RATE_HZ / 1_000
    )
    total_frames = round(burst_audio_seconds * SAMPLE_RATE_HZ)
    return math.ceil(total_frames / publication_frames)


def _minimum_source_tick_count(probe_seconds: float) -> int:
    source_seconds = SOURCE_FRAME_COUNT / SAMPLE_RATE_HZ
    chunk_seconds = SOURCE_CHUNK_FRAMES / SAMPLE_RATE_HZ
    if probe_seconds * MIN_CLOCK_RATE >= source_seconds + chunk_seconds:
        return SOURCE_FRAME_COUNT // SOURCE_CHUNK_FRAMES
    observable_seconds = min(
        probe_seconds * MIN_CLOCK_RATE,
        source_seconds,
    )
    return min(
        SOURCE_FRAME_COUNT // SOURCE_CHUNK_FRAMES,
        max(
            1,
            math.floor(
                observable_seconds
                / chunk_seconds
            )
            - 1,
        ),
    )


def _minimum_pcm_block_count(probe_seconds: float) -> int:
    block_seconds = CAPTURE_BLOCK_FRAMES / SAMPLE_RATE_HZ
    return max(
        1,
        math.floor(probe_seconds * MIN_CLOCK_RATE / block_seconds) - 1,
    )


def validate_probe_result(
    result: Mapping[str, Any],
    *,
    expected_translated_sources: int,
    minimum_pcm_blocks: int,
    minimum_source_ticks: int,
) -> list[str]:
    """Return fixed diagnostic codes for incomplete or invalid repeats."""

    failures: list[str] = []
    capture_errors = result.get("capture_errors")
    if not isinstance(capture_errors, list) or capture_errors:
        failures.append("capture_error")

    if result.get("audio_context_sample_rate_hz") != SAMPLE_RATE_HZ:
        failures.append("audio_context_sample_rate")

    capture_start = result.get("capture_start_context_frame")
    source_start = result.get("source_start_context_frame")
    if not isinstance(capture_start, int):
        failures.append("capture_start")
    if not isinstance(source_start, int):
        failures.append("source_start")
    if (
        isinstance(capture_start, int)
        and isinstance(source_start, int)
        and capture_start > source_start
    ):
        failures.append("capture_started_after_source")

    translated_sources = result.get("translated_sources_scheduled")
    if translated_sources != expected_translated_sources:
        failures.append("translated_source_count")

    source_ticks = result.get("source_tick_count")
    maximum_source_ticks = SOURCE_FRAME_COUNT // SOURCE_CHUNK_FRAMES
    if (
        not isinstance(source_ticks, int)
        or not minimum_source_ticks <= source_ticks <= maximum_source_ticks
    ):
        failures.append("source_tick_count")

    wall_elapsed_ms = result.get("wall_elapsed_ms")
    context_elapsed_ms = result.get("context_elapsed_ms")
    if (
        not isinstance(wall_elapsed_ms, float)
        or wall_elapsed_ms <= 0
        or not isinstance(context_elapsed_ms, float)
        or context_elapsed_ms <= 0
    ):
        failures.append("elapsed_clock")

    clock_rate = result.get("clock_rate")
    if (
        not isinstance(clock_rate, float)
        or not MIN_CLOCK_RATE <= clock_rate <= MAX_CLOCK_RATE
    ):
        failures.append("clock_rate")

    message_counts = result.get("message_counts")
    if not isinstance(message_counts, Mapping):
        failures.append("message_lifecycle")
    else:
        if any(
            message_counts.get(message_type) != 1
            for message_type in (
                "ready",
                "source_clock_armed",
                "started",
                "stopped",
            )
        ):
            failures.append("message_lifecycle")
        pcm_blocks = message_counts.get("pcm_block")
        if (
            not isinstance(pcm_blocks, int)
            or pcm_blocks < minimum_pcm_blocks
        ):
            failures.append("pcm_block_count")

    return failures


def _sanitize_browser_product(value: Any) -> dict[str, str | None]:
    product = value if isinstance(value, str) else ""
    match = BROWSER_PRODUCT_PATTERN.fullmatch(product)
    if match is None:
        return {"kind": "unknown", "version": None}
    kinds = {
        "Chrome": "chrome",
        "Chromium": "chromium",
        "HeadlessChrome": "headless_chrome",
    }
    return {
        "kind": kinds[match.group(1)],
        "version": match.group(2),
    }


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare exact recorder continuity under synthetic translated "
            "AudioBufferSourceNode bursts; does not connect to Riva"
        )
    )
    parser.add_argument(
        "--frame-ms",
        type=_positive_integer,
        nargs="+",
        default=[100, 500],
        help="publication frame durations to compare (default: 100 500)",
    )
    parser.add_argument(
        "--repeats",
        type=_positive_integer,
        default=5,
        help="repeats per frame duration (default: 5)",
    )
    parser.add_argument(
        "--probe-seconds",
        type=_positive_float,
        default=6,
        help="wall-clock duration per repeat (default: 6)",
    )
    parser.add_argument(
        "--burst-at-seconds",
        type=_positive_float,
        default=2,
        help="schedule the translated burst after this delay (default: 2)",
    )
    parser.add_argument(
        "--burst-audio-seconds",
        type=_positive_float,
        default=30,
        help="synthetic translated audio duration per burst (default: 30)",
    )
    parser.add_argument(
        "--chrome",
        type=Path,
        help="explicit Chrome/Chromium executable",
    )
    args = parser.parse_args(argv)
    if args.probe_seconds <= args.burst_at_seconds + 0.5:
        parser.error(
            "--probe-seconds must exceed --burst-at-seconds by more than 0.5"
        )
    if args.probe_seconds > 75:
        parser.error("--probe-seconds must not exceed 75")
    if args.burst_audio_seconds > 60:
        parser.error("--burst-audio-seconds must not exceed 60")
    if any(value > 5_000 for value in args.frame_ms):
        parser.error("--frame-ms values must not exceed 5000")
    if len(args.frame_ms) > MAX_FRAME_CANDIDATES:
        parser.error(
            f"no more than {MAX_FRAME_CANDIDATES} --frame-ms values are allowed"
        )
    if len(set(args.frame_ms)) != len(args.frame_ms):
        parser.error("--frame-ms values must be unique")
    if args.repeats > MAX_REPEATS:
        parser.error(f"--repeats must not exceed {MAX_REPEATS}")
    total_probe_seconds = (
        len(args.frame_ms) * args.repeats * args.probe_seconds
    )
    if total_probe_seconds > MAX_TOTAL_PROBE_SECONDS:
        parser.error(
            "the requested repeats exceed the total probe-time limit"
        )
    sources_per_repeat = [
        _translated_source_count(
            publication_frame_ms=frame_ms,
            burst_audio_seconds=args.burst_audio_seconds,
        )
        for frame_ms in args.frame_ms
    ]
    if any(
        not MIN_TRANSLATED_SOURCES_PER_REPEAT
        <= count
        <= MAX_TRANSLATED_SOURCES_PER_REPEAT
        for count in sources_per_repeat
    ):
        parser.error(
            "a requested frame duration falls outside the per-repeat "
            "source-node limits"
        )
    if (
        sum(sources_per_repeat) * args.repeats
        > MAX_TOTAL_TRANSLATED_SOURCES
    ):
        parser.error(
            "the requested candidates exceed the total source-node limit"
        )
    return args


def _wait_for_page(
    cdp: preflight.CDP,
    *,
    timeout_seconds: float = 10,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if cdp.evaluate("document.readyState === 'complete'") is True:
            return
        time.sleep(0.1)
    raise RuntimeError("probe page load timed out")


def _probe_expression(
    *,
    publication_frame_ms: int,
    burst_audio_seconds: float,
    burst_at_seconds: float,
    probe_seconds: float,
) -> str:
    maximum_capture_frames = math.ceil(
        (probe_seconds + 5) * SAMPLE_RATE_HZ
    )
    return (
        PROBE_EXPRESSION
        .replace("__PUBLICATION_FRAME_MS__", str(publication_frame_ms))
        .replace("__BURST_AUDIO_SECONDS__", repr(burst_audio_seconds))
        .replace("__BURST_AT_SECONDS__", repr(burst_at_seconds))
        .replace("__PROBE_SECONDS__", repr(probe_seconds))
        .replace(
            "__MAXIMUM_CAPTURE_FRAMES__",
            str(maximum_capture_frames),
        )
    )


def _launch_and_wait(
    cdp: preflight.CDP,
    expression: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    launched = cdp.evaluate(
        "window.__graphProbeResult=null;"
        "window.__graphProbeError=null;"
        "void ("
        + expression
        + ").then("
        "value=>{window.__graphProbeResult=value;},"
        "error=>{window.__graphProbeError=String(error?.name||'Error');}"
        ");true",
        user_gesture=True,
    )
    if launched is not True:
        raise RuntimeError("browser probe did not launch")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        error_name = cdp.evaluate("window.__graphProbeError")
        if error_name is not None:
            raise RuntimeError("browser probe failed")
        if cdp.evaluate("window.__graphProbeResult!==null") is True:
            return sanitize_probe_result(
                cdp.evaluate("window.__graphProbeResult")
            )
        time.sleep(0.5)
    raise RuntimeError("browser probe timed out")


def _registered_worklet_bytes(path: Path) -> tuple[bytes, str]:
    worklet_bytes = path.read_bytes()
    digest = hashlib.sha256(worklet_bytes).hexdigest()
    if digest != APPROVED_WORKLET_MODULE_SHA256:
        raise RuntimeError("recorder worklet does not match registered digest")
    return worklet_bytes, digest


def _terminate_chrome(chrome: subprocess.Popen[bytes] | None) -> None:
    if chrome is None:
        return
    if chrome.poll() is None:
        try:
            os.killpg(chrome.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        chrome.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    if chrome.poll() is None:
        try:
            os.killpg(chrome.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        chrome.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run(args: argparse.Namespace) -> int:
    worklet_bytes, worklet_sha256 = _registered_worklet_bytes(WORKLET_PATH)
    chrome_path = preflight.find_chrome(args.chrome)
    browser_binary_sha256 = preflight.sha256_file(chrome_path)

    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    server_started = False
    profile: Path | None = None
    log_directory: Path | None = None
    chrome_log: Any = None
    chrome: subprocess.Popen[bytes] | None = None
    cdp: preflight.CDP | None = None
    results: list[dict[str, Any]] = []
    try:
        ProbeHandler.worklet_bytes = worklet_bytes
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        server_thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
        )
        server_thread.start()
        server_started = True
        profile = Path(
            tempfile.mkdtemp(prefix="s2s-graph-probe-profile-")
        )
        log_directory = Path(
            tempfile.mkdtemp(prefix="s2s-graph-probe-log-")
        )
        chrome_log = (log_directory / "chrome.log").open("wb")
        chrome = subprocess.Popen(
            [
                str(chrome_path),
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--autoplay-policy=no-user-gesture-required",
                "--remote-allow-origins=*",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=chrome_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        debugging_port = preflight._chrome_debugging_port(
            chrome,
            profile,
        )
        with urllib.request.urlopen(
            f"http://127.0.0.1:{debugging_port}/json/list",
            timeout=5,
        ) as response:
            targets = json.load(response)
        target = next(
            item for item in targets if item.get("type") == "page"
        )
        cdp = preflight.CDP(str(target["webSocketDebuggerUrl"]))
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        browser_product = _sanitize_browser_product(
            cdp.call("Browser.getVersion").get("product")
        )
        if browser_product["kind"] == "unknown":
            raise RuntimeError("browser product is not recognized")
        provenance = {
            "browser_binary_sha256": browser_binary_sha256,
            "browser_kind": browser_product["kind"],
            "browser_version": browser_product["version"],
            "worklet_sha256": worklet_sha256,
        }
        cdp.call(
            "Page.navigate",
            {"url": f"http://127.0.0.1:{server.server_port}/"},
        )
        _wait_for_page(cdp)

        for frame_ms in args.frame_ms:
            for repetition in range(1, args.repeats + 1):
                result = _launch_and_wait(
                    cdp,
                    _probe_expression(
                        publication_frame_ms=frame_ms,
                        burst_audio_seconds=args.burst_audio_seconds,
                        burst_at_seconds=args.burst_at_seconds,
                        probe_seconds=args.probe_seconds,
                    ),
                    timeout_seconds=args.probe_seconds + 15,
                )
                expected_translated_sources = _translated_source_count(
                    publication_frame_ms=frame_ms,
                    burst_audio_seconds=args.burst_audio_seconds,
                )
                minimum_source_ticks = _minimum_source_tick_count(
                    args.probe_seconds
                )
                minimum_pcm_blocks = _minimum_pcm_block_count(
                    args.probe_seconds
                )
                failure_codes = validate_probe_result(
                    result,
                    expected_translated_sources=(
                        expected_translated_sources
                    ),
                    minimum_pcm_blocks=minimum_pcm_blocks,
                    minimum_source_ticks=minimum_source_ticks,
                )
                record = {
                    "frame_ms": frame_ms,
                    "provenance": provenance,
                    "repetition": repetition,
                    "result": result,
                    "validation": {
                        "expected_translated_sources": (
                            expected_translated_sources
                        ),
                        "failure_codes": failure_codes,
                        "minimum_pcm_blocks": minimum_pcm_blocks,
                        "minimum_source_ticks": minimum_source_ticks,
                        "status": "fail" if failure_codes else "pass",
                    },
                }
                results.append(record)
                print(
                    json.dumps(
                        record,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )

        summary = {
            str(frame_ms): {
                "failures": sum(
                    record["validation"]["status"] == "fail"
                    for record in results
                    if record["frame_ms"] == frame_ms
                ),
                "repeats": sum(
                    record["frame_ms"] == frame_ms
                    for record in results
                ),
            }
            for frame_ms in args.frame_ms
        }
        print(
            json.dumps(
                {
                    "overall_status": (
                        "fail"
                        if any(
                            record["validation"]["status"] == "fail"
                            for record in results
                        )
                        else "pass"
                    ),
                    "provenance": provenance,
                    "summary": summary,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return (
            1
            if any(
                record["validation"]["status"] == "fail"
                for record in results
            )
            else 0
        )
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except Exception:
                pass
        try:
            _terminate_chrome(chrome)
        except Exception:
            pass
        if chrome_log is not None:
            try:
                chrome_log.close()
            except Exception:
                pass
        if server is not None and server_started:
            try:
                server.shutdown()
            except Exception:
                pass
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        if server_thread is not None:
            try:
                server_thread.join(timeout=5)
            except Exception:
                pass
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)
        if log_directory is not None:
            shutil.rmtree(log_directory, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        print("ERROR: graph-load probe interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"ERROR: graph-load probe failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
