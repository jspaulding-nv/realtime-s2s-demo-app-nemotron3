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
RENDER_QUANTUM_FRAMES = 128
SOURCE_ALIGNMENT_FRAMES = math.lcm(
    SOURCE_CHUNK_FRAMES,
    RENDER_QUANTUM_FRAMES,
)
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
SHADOW_TRACE_BEFORE = 4
SHADOW_TRACE_AFTER = 16
SHADOW_TRACE_LIMIT = (
    SHADOW_TRACE_BEFORE + 1 + SHADOW_TRACE_AFTER
)
SHADOW_MARKER_PERIOD = 4_093
SHADOW_MARKER_SCALE = 8_192


SHADOW_WORKLET_SOURCE = rb"""
/* global AudioWorkletProcessor, currentFrame, currentTime, registerProcessor, sampleRate */

const MARKER_PERIOD = 4093;
const MARKER_SCALE = 8192;
const TRACE_BEFORE = 4;
const TRACE_AFTER = 16;

function safeInteger(value, minimum = 0) {
  return Number.isSafeInteger(value) && value >= minimum;
}

function decodeMarker(sample, referenceFrame) {
  if (!Number.isFinite(sample) || !safeInteger(referenceFrame)) return null;
  const code = Math.round(sample * MARKER_SCALE) - 1;
  if (code < 0 || code >= MARKER_PERIOD) return null;
  const cycle = Math.round((referenceFrame - code) / MARKER_PERIOD);
  const decoded = code + cycle * MARKER_PERIOD;
  return safeInteger(decoded) ? decoded : null;
}

class RenderClockShadowProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.armed = false;
    this.sourceStartContextFrame = null;
    this.sourceFrameCount = null;
    this.captureStartContextFrame = null;
    this.callbackOrdinal = 0;
    this.previousObservedContextFrame = null;
    this.previousDecodedSourceFrame = null;
    this.preTrace = [];
    this.trace = [];
    this.firstAnomalyOrdinal = null;
    this.postAnomalyRemaining = TRACE_AFTER;
    this.resultSent = false;
    this.port.onmessage = ({data}) => {
      if (data?.type === 'arm') {
        if (
          this.armed
          || !safeInteger(data.sourceStartContextFrame)
          || !safeInteger(data.sourceFrameCount, 1)
        ) {
          this.sendResult('invalid');
          return;
        }
        this.sourceStartContextFrame = data.sourceStartContextFrame;
        this.sourceFrameCount = data.sourceFrameCount;
        this.armed = true;
        this.captureStartContextFrame = null;
        this.callbackOrdinal = 0;
        this.previousObservedContextFrame = null;
        this.previousDecodedSourceFrame = null;
        this.preTrace = [];
        this.trace = [];
        this.firstAnomalyOrdinal = null;
        this.postAnomalyRemaining = TRACE_AFTER;
        this.port.postMessage({type: 'shadow_armed'});
      } else if (data?.type === 'stop') {
        this.sendResult(
          this.firstAnomalyOrdinal === null ? 'no_anomaly' : 'anomaly',
        );
      }
    };
    this.port.postMessage({type: 'shadow_ready'});
  }

  sendResult(status) {
    if (this.resultSent) return;
    this.resultSent = true;
    const anomaly = (
      this.firstAnomalyOrdinal === null
        ? null
        : this.trace.find(
          entry => entry.callbackOrdinal === this.firstAnomalyOrdinal,
        ) ?? null
    );
    this.port.postMessage({
      type: 'shadow_result',
      status,
      firstAnomalyOrdinal: this.firstAnomalyOrdinal,
      firstExpectedContextFrame: anomaly?.expectedContextFrame ?? null,
      firstObservedContextFrame: anomaly?.observedContextFrame ?? null,
      trace: this.trace,
    });
  }

  process(inputs, outputs) {
    const output = outputs[0]?.[0];
    const frameCount = output?.length ?? 128;
    output?.fill(0);
    if (!this.armed || this.resultSent) return true;

    const observedContextFrame = currentFrame;
    if (this.captureStartContextFrame === null) {
      this.captureStartContextFrame = observedContextFrame;
    }
    const logicalContextFrame = (
      this.captureStartContextFrame + this.callbackOrdinal * frameCount
    );
    const expectedContextFrame = (
      this.previousObservedContextFrame === null
        ? observedContextFrame
        : this.previousObservedContextFrame + frameCount
    );
    const stepFrames = (
      this.previousObservedContextFrame === null
        ? null
        : observedContextFrame - this.previousObservedContextFrame
    );
    const workletCurrentTimeFrame = Math.round(currentTime * sampleRate);

    const logicalSourceFrame = (
      logicalContextFrame - this.sourceStartContextFrame
    );
    const sourceIsActive = (
      logicalSourceFrame >= 0
      && logicalSourceFrame < this.sourceFrameCount
    );
    const sourceSample = inputs[0]?.[0]?.[0];
    const decodedSourceFrame = sourceIsActive
      ? decodeMarker(sourceSample, logicalSourceFrame)
      : null;
    const sourceStepFrames = (
      decodedSourceFrame === null
      || this.previousDecodedSourceFrame === null
        ? null
        : decodedSourceFrame - this.previousDecodedSourceFrame
    );
    if (decodedSourceFrame !== null) {
      this.previousDecodedSourceFrame = decodedSourceFrame;
    }

    const entry = {
      callbackOrdinal: this.callbackOrdinal,
      frameCount,
      expectedContextFrame,
      observedContextFrame,
      logicalContextFrame,
      axisOffsetFrames: observedContextFrame - logicalContextFrame,
      stepFrames,
      workletCurrentTimeFrame,
      decodedSourceFrame,
      sourceStepFrames,
    };
    const anomaly = observedContextFrame !== expectedContextFrame;
    if (this.firstAnomalyOrdinal === null) {
      if (anomaly) {
        this.firstAnomalyOrdinal = this.callbackOrdinal;
        this.trace = [...this.preTrace, entry];
      } else {
        this.preTrace.push(entry);
        if (this.preTrace.length > TRACE_BEFORE) this.preTrace.shift();
      }
    } else if (this.postAnomalyRemaining > 0) {
      this.trace.push(entry);
      this.postAnomalyRemaining -= 1;
      if (this.postAnomalyRemaining === 0) this.sendResult('anomaly');
    }

    this.previousObservedContextFrame = observedContextFrame;
    this.callbackOrdinal += 1;
    return true;
  }
}

registerProcessor('render-clock-shadow', RenderClockShadowProcessor);
"""


PROBE_EXPRESSION = r"""
(async () => {
  const sampleRateHz = 16000;
  const publicationFrameMs = __PUBLICATION_FRAME_MS__;
  const burstAudioSeconds = __BURST_AUDIO_SECONDS__;
  const burstAtSeconds = __BURST_AT_SECONDS__;
  const probeSeconds = __PROBE_SECONDS__;
  const sourceFrameCount = __SOURCE_FRAME_COUNT__;
  const maximumFrames = __MAXIMUM_CAPTURE_FRAMES__;
  const traceRewind = __TRACE_REWIND__;
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
  let shadowNode = null;
  let shadowReadyResolve;
  let shadowArmedResolve;
  let shadowResultResolve;
  const shadowReady = new Promise(resolve => {
    shadowReadyResolve = resolve;
  });
  const shadowArmed = new Promise(resolve => {
    shadowArmedResolve = resolve;
  });
  const shadowResult = new Promise(resolve => {
    shadowResultResolve = resolve;
  });
  if (traceRewind) {
    await context.audioWorklet.addModule('/shadow-worklet.js');
    shadowNode = new AudioWorkletNode(
      context,
      'render-clock-shadow',
      {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: 'explicit',
        channelInterpretation: 'discrete',
      },
    );
    shadowNode.port.onmessage = ({data}) => {
      if (data?.type === 'shadow_ready') {
        shadowReadyResolve();
      } else if (data?.type === 'shadow_armed') {
        shadowArmedResolve();
      } else if (data?.type === 'shadow_result') {
        shadowResultResolve(data);
      }
    };
    shadowNode.connect(sink);
    await shadowReady;
  }

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
    shadowTrace: traceRewind ? null : {status: 'not_requested'},
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
      const mainContextFrameAtDeliveryBefore = Math.round(
        context.currentTime * sampleRateHz
      );
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
        mainContextFrameAtDeliveryBefore,
        mainContextFrameAtDeliveryAfter: Math.round(
          context.currentTime * sampleRateHz
        ),
        mainContextStateAtDelivery: context.state,
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
    sourceFrameCount,
    sampleRateHz,
  );
  if (traceRewind) {
    const marker = sourceBuffer.getChannelData(0);
    for (let index = 0; index < marker.length; index += 1) {
      marker[index] = ((index % 4093) + 1) / 8192;
    }
  }
  const source = context.createBufferSource();
  source.buffer = sourceBuffer;
  source.connect(monitor);
  source.connect(node, 0, 0);
  if (shadowNode !== null) source.connect(shadowNode, 0, 0);
  source.start(sourceStartContextFrame / sampleRateHz);
  node.port.postMessage({
    type: 'arm_source_clock',
    sourceStartContextFrame,
    sourceFrameCount,
    sourceChunkFrames: 4800,
  });
  if (shadowNode !== null) {
    shadowNode.port.postMessage({
      type: 'arm',
      sourceStartContextFrame,
      sourceFrameCount,
    });
  }
  await Promise.all([
    started,
    ...(shadowNode === null ? [] : [shadowArmed]),
  ]);

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
  if (shadowNode !== null) shadowNode.port.postMessage({type: 'stop'});
  await Promise.race([
    stopped,
    new Promise(resolve => setTimeout(resolve, 1000)),
  ]);
  if (shadowNode !== null) {
    result.shadowTrace = await Promise.race([
      shadowResult,
      new Promise(resolve => {
        setTimeout(() => resolve({status: 'incomplete'}), 1000);
      }),
    ]);
  }
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
    shadow_worklet_bytes = b""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/worklet.js":
            body = self.worklet_bytes
            content_type = "text/javascript"
        elif self.path == "/shadow-worklet.js":
            body = self.shadow_worklet_bytes
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


def _safe_integer(value: Any) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -SAFE_INTEGER_MAX <= value <= SAFE_INTEGER_MAX
    ):
        return value
    return None


def _safe_nonnegative_integer(value: Any) -> int | None:
    parsed = _safe_integer(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _safe_finite_number(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return float(value)
    return None


def _sanitize_shadow_trace(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, Mapping) else {}
    raw_status = payload.get("status")
    status = (
        raw_status
        if raw_status in {
            "anomaly",
            "incomplete",
            "invalid",
            "no_anomaly",
            "not_requested",
        }
        else "unknown"
    )
    result: dict[str, Any] = {
        "status": status,
        "first_anomaly_ordinal": _safe_nonnegative_integer(
            payload.get("firstAnomalyOrdinal")
        ),
        "first_expected_context_frame": _safe_nonnegative_integer(
            payload.get("firstExpectedContextFrame")
        ),
        "first_observed_context_frame": _safe_nonnegative_integer(
            payload.get("firstObservedContextFrame")
        ),
    }
    safe_entries: list[dict[str, Any]] = []
    raw_entries = payload.get("trace")
    result["trace_entry_count"] = (
        min(len(raw_entries), SHADOW_TRACE_LIMIT + 1)
        if isinstance(raw_entries, list)
        else 0
    )
    if isinstance(raw_entries, list):
        for raw_entry in raw_entries[:SHADOW_TRACE_LIMIT]:
            entry = raw_entry if isinstance(raw_entry, Mapping) else {}
            safe_entries.append(
                {
                    "callback_ordinal": _safe_nonnegative_integer(
                        entry.get("callbackOrdinal")
                    ),
                    "frame_count": _safe_nonnegative_integer(
                        entry.get("frameCount")
                    ),
                    "expected_context_frame": _safe_nonnegative_integer(
                        entry.get("expectedContextFrame")
                    ),
                    "observed_context_frame": _safe_nonnegative_integer(
                        entry.get("observedContextFrame")
                    ),
                    "logical_context_frame": _safe_nonnegative_integer(
                        entry.get("logicalContextFrame")
                    ),
                    "axis_offset_frames": _safe_integer(
                        entry.get("axisOffsetFrames")
                    ),
                    "step_frames": _safe_integer(
                        entry.get("stepFrames")
                    ),
                    "worklet_current_time_frame": (
                        _safe_nonnegative_integer(
                            entry.get("workletCurrentTimeFrame")
                        )
                    ),
                    "decoded_source_frame": _safe_nonnegative_integer(
                        entry.get("decodedSourceFrame")
                    ),
                    "source_step_frames": _safe_integer(
                        entry.get("sourceStepFrames")
                    ),
                }
            )
    result["trace"] = safe_entries
    return result


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
                "main_context_frame_at_delivery_before": (
                    _safe_nonnegative_integer(
                        error.get("mainContextFrameAtDeliveryBefore")
                    )
                ),
                "main_context_frame_at_delivery_after": (
                    _safe_nonnegative_integer(
                        error.get("mainContextFrameAtDeliveryAfter")
                    )
                ),
                "main_context_state_at_delivery": (
                    error.get("mainContextStateAtDelivery")
                    if error.get("mainContextStateAtDelivery") in {
                        "closed",
                        "interrupted",
                        "running",
                        "suspended",
                    }
                    else "unknown"
                ),
            }
            capture_errors.append(safe_error)
    result["capture_errors"] = capture_errors
    result["shadow_trace"] = _sanitize_shadow_trace(
        payload.get("shadowTrace")
    )
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


def _source_frame_count(
    probe_seconds: float,
    *,
    trace_rewind: bool,
) -> int:
    if not trace_rewind:
        return SOURCE_FRAME_COUNT
    required_frames = max(
        SOURCE_FRAME_COUNT,
        math.ceil((probe_seconds + 1) * SAMPLE_RATE_HZ),
    )
    return (
        math.ceil(required_frames / SOURCE_ALIGNMENT_FRAMES)
        * SOURCE_ALIGNMENT_FRAMES
    )


def _minimum_source_tick_count(
    probe_seconds: float,
    *,
    source_frame_count: int = SOURCE_FRAME_COUNT,
) -> int:
    source_seconds = source_frame_count / SAMPLE_RATE_HZ
    chunk_seconds = SOURCE_CHUNK_FRAMES / SAMPLE_RATE_HZ
    if probe_seconds * MIN_CLOCK_RATE >= source_seconds + chunk_seconds:
        return source_frame_count // SOURCE_CHUNK_FRAMES
    observable_seconds = min(
        probe_seconds * MIN_CLOCK_RATE,
        source_seconds,
    )
    return min(
        source_frame_count // SOURCE_CHUNK_FRAMES,
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
    maximum_source_ticks: int | None = None,
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
    if maximum_source_ticks is None:
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


def validate_shadow_trace(
    shadow_trace: Mapping[str, Any],
) -> list[str]:
    """Validate the complete bounded trace before interpreting it."""

    failures: list[str] = []
    raw_entries = shadow_trace.get("trace")
    entries = raw_entries if isinstance(raw_entries, list) else []
    if (
        shadow_trace.get("trace_entry_count") != SHADOW_TRACE_LIMIT
        or len(entries) != SHADOW_TRACE_LIMIT
    ):
        return ["shadow_trace_incomplete"]
    if not all(isinstance(entry, Mapping) for entry in entries):
        return ["shadow_trace_structure"]

    ordinal = shadow_trace.get("first_anomaly_ordinal")
    anomaly_index = SHADOW_TRACE_BEFORE
    anomaly = entries[anomaly_index]
    if (
        not isinstance(ordinal, int)
        or anomaly.get("callback_ordinal") != ordinal
        or shadow_trace.get("first_expected_context_frame")
        != anomaly.get("expected_context_frame")
        or shadow_trace.get("first_observed_context_frame")
        != anomaly.get("observed_context_frame")
    ):
        failures.append("shadow_anomaly_identity")

    quantum = anomaly.get("frame_count")
    if not isinstance(quantum, int) or quantum <= 0:
        return failures + ["shadow_frame_count"]

    first_ordinal = entries[0].get("callback_ordinal")
    first_logical = entries[0].get("logical_context_frame")
    if not isinstance(first_ordinal, int) or not isinstance(
        first_logical, int
    ):
        return failures + ["shadow_trace_structure"]

    previous: Mapping[str, Any] | None = None
    for index, entry in enumerate(entries):
        observed = entry.get("observed_context_frame")
        logical = entry.get("logical_context_frame")
        decoded = entry.get("decoded_source_frame")
        if (
            entry.get("callback_ordinal") != first_ordinal + index
            or entry.get("frame_count") != quantum
            or logical != first_logical + index * quantum
            or not isinstance(observed, int)
            or not isinstance(logical, int)
            or entry.get("axis_offset_frames") != observed - logical
            or entry.get("worklet_current_time_frame") != observed
            or not isinstance(decoded, int)
            or not isinstance(entry.get("source_step_frames"), int)
        ):
            failures.append("shadow_trace_structure")
            break
        if previous is not None:
            previous_observed = previous.get("observed_context_frame")
            previous_decoded = previous.get("decoded_source_frame")
            if (
                not isinstance(previous_observed, int)
                or entry.get("expected_context_frame")
                != previous_observed + quantum
                or entry.get("step_frames")
                != observed - previous_observed
                or not isinstance(previous_decoded, int)
                or entry.get("source_step_frames")
                != decoded - previous_decoded
            ):
                failures.append("shadow_trace_arithmetic")
                break
        previous = entry

    if any(
        entry.get("axis_offset_frames") != 0
        or entry.get("step_frames") != quantum
        for entry in entries[:anomaly_index]
    ):
        failures.append("shadow_pre_anomaly_state")
    if (
        anomaly.get("axis_offset_frames") != -quantum
        or anomaly.get("step_frames") != 0
    ):
        failures.append("shadow_first_anomaly_shape")
    return list(dict.fromkeys(failures))


def classify_shadow_trace(
    shadow_trace: Mapping[str, Any],
) -> dict[str, str]:
    """Classify a complete bounded, generated-PCM render-clock trace."""

    if shadow_trace.get("status") == "no_anomaly":
        return {
            "clock_behavior": "not_reproduced",
            "main_clock_relation": "not_observed",
            "media_behavior": "not_observed",
        }
    if (
        shadow_trace.get("status") != "anomaly"
        or validate_shadow_trace(shadow_trace)
    ):
        return {
            "clock_behavior": "unresolved",
            "main_clock_relation": "not_observed",
            "media_behavior": "unresolved",
        }

    entries = shadow_trace["trace"]
    anomaly_index = SHADOW_TRACE_BEFORE
    anomaly = entries[anomaly_index]
    quantum = anomaly["frame_count"]
    later = entries[anomaly_index + 1 :]

    catch_up_index = next(
        (
            index
            for index, entry in enumerate(later)
            if entry.get("axis_offset_frames") == 0
            and entry.get("step_frames") == 2 * quantum
        ),
        None,
    )
    if catch_up_index is not None:
        before_catch_up = later[:catch_up_index]
        after_catch_up = later[catch_up_index + 1 :]
        if all(
            entry.get("axis_offset_frames") == -quantum
            and entry.get("step_frames") == quantum
            for entry in before_catch_up
        ) and all(
            entry.get("axis_offset_frames") == 0
            and entry.get("step_frames") == quantum
            for entry in after_catch_up
        ):
            clock_behavior = "repeated_frame_then_catch_up"
        else:
            clock_behavior = "one_quantum_discontinuity_other"
    elif all(
        entry.get("axis_offset_frames") == -quantum
        and entry.get("step_frames") == quantum
        for entry in later
    ):
        clock_behavior = "sustained_one_quantum_offset_within_trace"
    else:
        clock_behavior = "one_quantum_discontinuity_other"

    source_steps = [
        entry.get("source_step_frames")
        for entry in entries[anomaly_index:]
    ]
    if all(step == quantum for step in source_steps):
        media_behavior = "samples_contiguous"
    elif (
        all(step in {0, quantum, 2 * quantum} for step in source_steps)
        and 0 in source_steps
        and 2 * quantum in source_steps
    ):
        media_behavior = "samples_duplicated_and_dropped"
    elif (
        all(step in {0, quantum} for step in source_steps)
        and 0 in source_steps
    ):
        media_behavior = "samples_duplicated"
    elif (
        all(step in {quantum, 2 * quantum} for step in source_steps)
        and 2 * quantum in source_steps
    ):
        media_behavior = "samples_dropped"
    else:
        media_behavior = "other"

    return {
        "clock_behavior": clock_behavior,
        "main_clock_relation": "evaluated_at_message_delivery",
        "media_behavior": media_behavior,
    }


def evaluate_rewind_diagnostic(
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconcile the exact fail-closed recorder with the shadow trace."""

    raw_errors = result.get("capture_errors")
    errors = raw_errors if isinstance(raw_errors, list) else []
    raw_shadow = result.get("shadow_trace")
    shadow = raw_shadow if isinstance(raw_shadow, Mapping) else {}
    classification = classify_shadow_trace(shadow)
    failure_codes: list[str] = []

    if not errors:
        no_anomaly_shape = (
            shadow.get("status") == "no_anomaly"
            and shadow.get("first_anomaly_ordinal") is None
            and shadow.get("first_expected_context_frame") is None
            and shadow.get("first_observed_context_frame") is None
            and shadow.get("trace_entry_count") == 0
            and shadow.get("trace") == []
        )
        if no_anomaly_shape:
            status = "not_reproduced"
        else:
            status = "invalid"
            failure_codes.append("exact_shadow_disagreement")
        return {
            "classification": classification,
            "failure_codes": failure_codes,
            "status": status,
        }

    if len(errors) != 1:
        failure_codes.append("exact_error_count")
    exact = errors[0] if isinstance(errors[0], Mapping) else {}
    if exact.get("code") != "noncontiguous_render_quantum":
        failure_codes.append("unexpected_exact_error")
    if shadow.get("status") != "anomaly":
        failure_codes.append("shadow_trace_missing")
    if (
        exact.get("expected_context_frame")
        != shadow.get("first_expected_context_frame")
        or exact.get("observed_context_frame")
        != shadow.get("first_observed_context_frame")
    ):
        failure_codes.append("exact_shadow_disagreement")

    failure_codes.extend(validate_shadow_trace(shadow))
    if classification["clock_behavior"] == "unresolved":
        failure_codes.append("clock_classification_unresolved")
    if classification["media_behavior"] in {"not_observed", "unresolved"}:
        failure_codes.append("media_classification_unresolved")

    main_before = exact.get("main_context_frame_at_delivery_before")
    main_after = exact.get("main_context_frame_at_delivery_after")
    expected = exact.get("expected_context_frame")
    if (
        isinstance(main_before, int)
        and isinstance(main_after, int)
        and isinstance(expected, int)
        and main_before >= expected
        and main_after >= main_before
        and exact.get("main_context_state_at_delivery") == "running"
    ):
        classification["main_clock_relation"] = (
            "running_at_or_past_expected_when_error_delivered"
        )
    else:
        classification["main_clock_relation"] = "unresolved"
        failure_codes.append("main_clock_delivery_observation_unresolved")

    return {
        "classification": classification,
        "failure_codes": failure_codes,
        "status": "invalid" if failure_codes else "traced",
    }


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
    parser.add_argument(
        "--trace-rewind",
        action="store_true",
        help=(
            "attach a generated-PCM shadow worklet that traces a recorder "
            "clock discontinuity without changing the strict recorder"
        ),
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
    trace_rewind: bool = False,
) -> str:
    maximum_capture_frames = math.ceil(
        (probe_seconds + 5) * SAMPLE_RATE_HZ
    )
    source_frame_count = _source_frame_count(
        probe_seconds,
        trace_rewind=trace_rewind,
    )
    return (
        PROBE_EXPRESSION
        .replace("__PUBLICATION_FRAME_MS__", str(publication_frame_ms))
        .replace("__BURST_AUDIO_SECONDS__", repr(burst_audio_seconds))
        .replace("__BURST_AT_SECONDS__", repr(burst_at_seconds))
        .replace("__PROBE_SECONDS__", repr(probe_seconds))
        .replace("__SOURCE_FRAME_COUNT__", str(source_frame_count))
        .replace("__TRACE_REWIND__", "true" if trace_rewind else "false")
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


def _record_failed(
    record: Mapping[str, Any],
    *,
    trace_rewind: bool,
) -> bool:
    validation = record.get("validation")
    if (
        not isinstance(validation, Mapping)
        or validation.get("status") != "pass"
    ):
        return True
    if not trace_rewind:
        return False
    diagnostic = record.get("rewind_diagnostic")
    return (
        not isinstance(diagnostic, Mapping)
        or diagnostic.get("status") not in {"not_reproduced", "traced"}
    )


def run(args: argparse.Namespace) -> int:
    worklet_bytes, worklet_sha256 = _registered_worklet_bytes(WORKLET_PATH)
    shadow_worklet_sha256 = hashlib.sha256(
        SHADOW_WORKLET_SOURCE
    ).hexdigest()
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
        ProbeHandler.shadow_worklet_bytes = SHADOW_WORKLET_SOURCE
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
            **(
                {"shadow_worklet_sha256": shadow_worklet_sha256}
                if args.trace_rewind
                else {}
            ),
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
                        trace_rewind=args.trace_rewind,
                    ),
                    timeout_seconds=args.probe_seconds + 15,
                )
                expected_translated_sources = _translated_source_count(
                    publication_frame_ms=frame_ms,
                    burst_audio_seconds=args.burst_audio_seconds,
                )
                source_frame_count = _source_frame_count(
                    args.probe_seconds,
                    trace_rewind=args.trace_rewind,
                )
                minimum_source_ticks = _minimum_source_tick_count(
                    args.probe_seconds,
                    source_frame_count=source_frame_count,
                )
                minimum_pcm_blocks = _minimum_pcm_block_count(
                    args.probe_seconds
                )
                failure_codes = validate_probe_result(
                    result,
                    expected_translated_sources=(
                        expected_translated_sources
                    ),
                    maximum_source_ticks=(
                        source_frame_count // SOURCE_CHUNK_FRAMES
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
                        "source_frame_count": source_frame_count,
                        "status": "fail" if failure_codes else "pass",
                    },
                    **(
                        {
                            "rewind_diagnostic": (
                                evaluate_rewind_diagnostic(result)
                            )
                        }
                        if args.trace_rewind
                        else {}
                    ),
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
                    _record_failed(
                        record,
                        trace_rewind=args.trace_rewind,
                    )
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
        rewind_summary = (
            {
                status: sum(
                    record.get("rewind_diagnostic", {}).get("status")
                    == status
                    for record in results
                )
                for status in ("invalid", "not_reproduced", "traced")
            }
            if args.trace_rewind
            else None
        )
        print(
            json.dumps(
                {
                    "overall_status": (
                        "fail"
                        if any(
                            _record_failed(
                                record,
                                trace_rewind=args.trace_rewind,
                            )
                            for record in results
                        )
                        else "pass"
                    ),
                    "provenance": provenance,
                    **(
                        {"rewind_diagnostic_summary": rewind_summary}
                        if rewind_summary is not None
                        else {}
                    ),
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
                _record_failed(
                    record,
                    trace_rewind=args.trace_rewind,
                )
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
