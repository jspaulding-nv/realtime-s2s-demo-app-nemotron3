#!/usr/bin/env python3
"""
Batch latency test for real-time audio translation.

Streams audio files through the backend WebSocket, measures translation
drift, and generates per-file latency plots and CSV exports.

Prerequisites:
  1. Riva gRPC services running at the backend's configured RIVA_URI
  2. Backend: cd backend && uvicorn main:app --host 0.0.0.0 --port 8000

Usage:
  python batch_latency_test.py                          # All 3 MP3 files
  python batch_latency_test.py --preflight              # Pre-flight only
  python batch_latency_test.py --file test_audio/X.mp3  # Single file
  python batch_latency_test.py --backend http://host:port
"""

import argparse
import asyncio
import csv
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
import websockets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
CHUNK_SAMPLES = 4800
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE  # 9600
CHUNK_DURATION = CHUNK_SAMPLES / SAMPLE_RATE      # 0.3 s
DRAIN_MAX_SECONDS = 300
TARGET_LANGUAGE = "es-US"

TEST_FILES = [
    "test_audio/200108_SpiritandPresenceofGod.mp3",
    "test_audio/Blessed_Self-Forgetfulness.mp3",
    "test_audio/gospel_in_life_tk_1-john-part-2-mp3_Beholding_the_Love_of_God.mp3",
]
PREFLIGHT_FILE = "test_audio/test-1min.wav"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TimingEvent:
    source: str            # "client" or "backend"
    stage: str
    timestamp_ms: float
    chunk_index: int
    source_position_sec: float
    audio_bytes: int


@dataclass
class DriftSample:
    elapsed_sec: float
    drift_sec: float


@dataclass
class TestResult:
    audio_path: str
    duration_sec: float
    chunks_sent: int = 0
    audio_responses: int = 0
    total_received_bytes: int = 0
    client_events: list = field(default_factory=list)
    backend_events: list = field(default_factory=list)
    drift_samples: list = field(default_factory=list)
    avg_drift: float = 0.0
    max_drift: float = 0.0
    final_drift: float = 0.0
    output_duration_sec: float = 0.0
    tts_expansion_ratio: float = 0.0
    tail_lag_sec: float = 0.0
    first_audio_latency_sec: float = 0.0
    duration_excess_sec: float = 0.0
    playback_tail_sec: float = 0.0
    post_input_responses: int = 0
    input_completed: bool = False
    connection_lost: bool = False
    drain_timed_out: bool = False
    drain_duration_sec: float = 0.0
    translation_completed: bool = False
    server_error: str = ""


# ---------------------------------------------------------------------------
# Audio decoding
# ---------------------------------------------------------------------------
def decode_audio(path: str) -> np.ndarray:
    """Decode any audio file to 16 kHz mono Int16 PCM via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise RuntimeError(
                "ffmpeg is not installed; install imageio-ffmpeg in the venv"
            ) from exc

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {path}: {result.stderr.decode().strip()}"
        )
    return np.frombuffer(result.stdout, dtype=np.int16)


# ---------------------------------------------------------------------------
# Progress bar helper
# ---------------------------------------------------------------------------
def progress_bar(current: float, total: float, width: int = 20) -> str:
    frac = min(current / total, 1.0) if total > 0 else 0
    filled = int(frac * width)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return bar


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------
async def run_test(audio_path: str, backend_url: str) -> TestResult:
    """Run a single latency test against one audio file."""

    ws_url = backend_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/translate"

    # -- Decode audio -------------------------------------------------------
    print(f"Decoding {audio_path}...", end=" ", flush=True)
    pcm = decode_audio(audio_path)
    duration_sec = len(pcm) / SAMPLE_RATE
    print(f"{len(pcm)} samples ({duration_sec:.1f}s)")

    result = TestResult(audio_path=audio_path, duration_sec=duration_sec)
    pcm_bytes = pcm.tobytes()
    total_chunks = (len(pcm_bytes) + CHUNK_BYTES - 1) // CHUNK_BYTES

    # -- Start backend timing session ---------------------------------------
    print("Starting test session...", flush=True)
    resp = requests.post(f"{backend_url}/api/test/start", timeout=10)
    resp.raise_for_status()

    # -- Connect WebSocket --------------------------------------------------
    print(f"Connecting to {ws_url}...", flush=True)
    test_start_time = time.monotonic()
    client_start_epoch = time.time()

    # Disable websockets library auto-ping (uvicorn/starlette doesn't
    # respond to protocol-level pings). We send app-level pings instead.
    async with websockets.connect(
        ws_url,
        max_size=2**22,
        ping_interval=None,
        ping_timeout=None,
        close_timeout=10,
    ) as ws:

        # Wait for "connected" status
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        if msg.get("status") != "connected":
            raise RuntimeError(f"Unexpected initial message: {msg}")

        # Send start_stream
        await ws.send(json.dumps({
            "type": "start_stream",
            "targetLanguage": TARGET_LANGUAGE,
        }))

        # Wait for "listening"
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        if msg.get("status") != "listening":
            raise RuntimeError(f"Expected 'listening', got: {msg}")

        # -- Shared state for concurrent tasks ------------------------------
        chunks_sent = 0
        audio_responses = 0
        total_recv_bytes = 0
        last_audio_time = time.monotonic()
        send_done = asyncio.Event()
        translation_complete = asyncio.Event()
        connection_lost = False
        server_error = ""
        last_print_time = 0.0

        def current_drift() -> float:
            input_pos = chunks_sent * CHUNK_DURATION
            output_dur = total_recv_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            return input_pos - output_dur

        # -- App-level keepalive ping task ----------------------------------
        async def keepalive():
            """Send app-level ping every 10s to keep connection alive."""
            try:
                while True:
                    await asyncio.sleep(10)
                    await ws.send(json.dumps({"type": "ping"}))
            except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                pass

        # -- Send task ------------------------------------------------------
        async def send_audio():
            nonlocal chunks_sent, last_print_time, connection_lost
            offset = 0
            idx = 0
            loop_start = time.monotonic()

            while offset < len(pcm_bytes):
                chunk = pcm_bytes[offset : offset + CHUNK_BYTES]
                send_ts = time.time()

                try:
                    await ws.send(chunk)
                except websockets.exceptions.ConnectionClosed:
                    connection_lost = True
                    print(f"\nConnection lost at chunk {idx} "
                          f"({idx * CHUNK_DURATION:.1f}s)")
                    break

                result.client_events.append(TimingEvent(
                    source="client",
                    stage="chunk_sent",
                    timestamp_ms=(send_ts - client_start_epoch) * 1000,
                    chunk_index=idx,
                    source_position_sec=idx * CHUNK_DURATION,
                    audio_bytes=len(chunk),
                ))

                chunks_sent = idx + 1
                offset += CHUNK_BYTES
                idx += 1

                # Progress reporting (every 1s)
                now = time.monotonic()
                if now - last_print_time >= 1.0:
                    elapsed = now - test_start_time
                    pos = chunks_sent * CHUNK_DURATION
                    bar = progress_bar(pos, duration_sec)
                    drift = current_drift()

                    # Record drift sample
                    result.drift_samples.append(DriftSample(
                        elapsed_sec=elapsed, drift_sec=drift,
                    ))

                    print(
                        f"\rStreaming: [{bar}] {pos:.1f}/{duration_sec:.1f}s"
                        f" | Sent: {chunks_sent} | Recv: {audio_responses}"
                        f" | Drift: {drift:.1f}s   ",
                        end="", flush=True,
                    )
                    last_print_time = now

                # Self-correcting timer: sleep until next chunk boundary
                expected = loop_start + idx * CHUNK_DURATION
                sleep_for = expected - time.monotonic()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

            send_done.set()

        # -- Receive task ---------------------------------------------------
        async def receive_audio():
            nonlocal audio_responses, total_recv_bytes, connection_lost, last_audio_time, server_error
            recv_idx = 0
            try:
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        recv_ts = time.time()
                        audio_responses += 1
                        total_recv_bytes += len(raw)
                        last_audio_time = time.monotonic()
                        result.client_events.append(TimingEvent(
                            source="client",
                            stage="audio_received",
                            timestamp_ms=(recv_ts - client_start_epoch) * 1000,
                            chunk_index=recv_idx,
                            source_position_sec=total_recv_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE),
                            audio_bytes=len(raw),
                        ))
                        recv_idx += 1
                    elif isinstance(raw, str):
                        try:
                            control = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if (
                            control.get("type") == "status"
                            and control.get("status") == "completed"
                        ):
                            translation_complete.set()
                        elif control.get("type") == "error":
                            server_error = control.get("message", "backend error")
                            translation_complete.set()
            except websockets.exceptions.ConnectionClosed:
                connection_lost = True
            except asyncio.CancelledError:
                pass

        # -- Run send + receive + keepalive concurrently --------------------
        ping_task = asyncio.create_task(keepalive())
        recv_task = asyncio.create_task(receive_audio())
        await send_audio()

        # Final progress line
        pos = chunks_sent * CHUNK_DURATION
        print(
            f"\rStreaming: [{progress_bar(pos, duration_sec)}] "
            f"{pos:.1f}/{duration_sec:.1f}s"
            f" | Sent: {chunks_sent} | Recv: {audio_responses}"
            f" | Drift: {current_drift():.1f}s   ",
        )

        if connection_lost:
            print("(connection lost — saving partial results)")
        else:
            # Close only the input side so Riva can flush final ASR/NMT/TTS
            # responses while this client keeps receiving translated audio.
            responses_at_input_end = audio_responses
            input_end_time = time.monotonic()
            await ws.send(json.dumps({"type": "end_input"}))

            print(
                "Draining translated tail until Riva confirms completion "
                f"(max {DRAIN_MAX_SECONDS}s)...",
                flush=True,
            )
            drain_start = time.monotonic()
            while time.monotonic() - drain_start < DRAIN_MAX_SECONDS:
                if connection_lost:
                    print("\n(connection lost during drain)")
                    break
                elapsed = time.monotonic() - drain_start
                idle = time.monotonic() - last_audio_time
                drift = current_drift()
                print(
                    f"\rDraining: {elapsed:.0f}s | idle: {idle:.0f}s"
                    f" | Recv: {audio_responses} | Drift: {drift:.1f}s"
                    f" | complete: {translation_complete.is_set()}   ",
                    end="", flush=True,
                )
                result.drift_samples.append(DriftSample(
                    elapsed_sec=time.monotonic() - test_start_time,
                    drift_sec=drift,
                ))
                if translation_complete.is_set():
                    break
                await asyncio.sleep(1.0)
            print()

            result.drain_duration_sec = time.monotonic() - drain_start
            result.translation_completed = (
                translation_complete.is_set() and not server_error
            )
            result.server_error = server_error
            result.drain_timed_out = not translation_complete.is_set()
            result.tail_lag_sec = max(0.0, last_audio_time - input_end_time)
            result.post_input_responses = audio_responses - responses_at_input_end

            # Stop stream
            try:
                await ws.send(json.dumps({"type": "stop_stream"}))
            except websockets.exceptions.ConnectionClosed:
                pass

        ping_task.cancel()
        recv_task.cancel()
        try:
            await ping_task
        except asyncio.CancelledError:
            pass
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    # -- Stop backend timing & export ---------------------------------------
    requests.post(f"{backend_url}/api/test/stop", timeout=10)

    try:
        export_resp = requests.get(f"{backend_url}/api/test/export", timeout=30)
        export_resp.raise_for_status()
        export_data = export_resp.json()
        for ev in export_data.get("events", []):
            result.backend_events.append(TimingEvent(
                source="backend",
                stage=ev["stage"],
                timestamp_ms=ev.get("wall_clock", 0) * 1000,
                chunk_index=ev.get("chunk_index", -1),
                source_position_sec=ev.get("source_position_sec", 0),
                audio_bytes=ev.get("audio_bytes_len", 0),
            ))
    except Exception as e:
        print(f"Warning: could not export backend timing: {e}")

    # -- Compute summary stats ----------------------------------------------
    result.chunks_sent = chunks_sent
    result.audio_responses = audio_responses
    result.total_received_bytes = total_recv_bytes
    result.input_completed = chunks_sent == total_chunks
    result.connection_lost = connection_lost
    result.output_duration_sec = total_recv_bytes / (
        SAMPLE_RATE * BYTES_PER_SAMPLE
    )
    result.duration_excess_sec = max(
        0.0, result.output_duration_sec - duration_sec
    )
    if duration_sec:
        result.tts_expansion_ratio = result.output_duration_sec / duration_sec

    if result.drift_samples:
        drifts = [s.drift_sec for s in result.drift_samples]
        result.avg_drift = sum(drifts) / len(drifts)
        result.max_drift = max(drifts)
        result.final_drift = drifts[-1]

    # Simulate the browser's gapless playback queue using actual arrival
    # timestamps. This captures initial latency and delivery gaps as well as
    # output-duration expansion, yielding the listener-visible tail.
    playback_end_sec = 0.0
    input_end_sec = 0.0
    for event in sorted(result.client_events, key=lambda e: e.timestamp_ms):
        event_time_sec = event.timestamp_ms / 1000
        if event.stage == "chunk_sent":
            input_end_sec = max(input_end_sec, event_time_sec)
        elif event.stage == "audio_received":
            if result.first_audio_latency_sec == 0.0:
                result.first_audio_latency_sec = event_time_sec
            playback_end_sec = max(playback_end_sec, event_time_sec)
            playback_end_sec += event.audio_bytes / (
                SAMPLE_RATE * BYTES_PER_SAMPLE
            )
    result.playback_tail_sec = max(0.0, playback_end_sec - input_end_sec)

    return result


# ---------------------------------------------------------------------------
# Output generation
# ---------------------------------------------------------------------------
def generate_plot(result: TestResult, output_path: str):
    """Create a matplotlib drift-over-time plot."""
    if not result.drift_samples:
        print(f"  No drift data to plot for {result.audio_path}")
        return

    elapsed_min = [s.elapsed_sec / 60 for s in result.drift_samples]
    drift_sec = [s.drift_sec for s in result.drift_samples]

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(elapsed_min, drift_sec, color="#3b82f6", linewidth=1.5, label="Drift")
    ax.axhline(y=20, color="orange", linestyle="--", linewidth=1, label="Warning (20s)")
    ax.axhline(y=30, color="red", linestyle="--", linewidth=1, label="Danger (30s)")
    ax.set_xlabel("Elapsed Time (minutes)")
    ax.set_ylabel("Translation Delay (seconds)")
    ax.set_title(Path(result.audio_path).stem)
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {output_path}")


def generate_csv(result: TestResult, output_path: str):
    """Write combined client + backend timing events to CSV."""
    all_events = result.client_events + result.backend_events
    all_events.sort(key=lambda e: e.timestamp_ms)

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "source", "stage", "timestamp_ms",
            "chunk_index", "source_position_sec", "audio_bytes",
        ])
        for ev in all_events:
            writer.writerow([
                ev.source, ev.stage, f"{ev.timestamp_ms:.2f}",
                ev.chunk_index, f"{ev.source_position_sec:.3f}",
                ev.audio_bytes,
            ])
    print(f"Saved: {output_path}")


def generate_summary(result: TestResult, output_path: str):
    """Write compact metrics separately from the event-level CSV."""
    summary = {
        "audio_path": result.audio_path,
        "input_duration_sec": result.duration_sec,
        "chunks_sent": result.chunks_sent,
        "audio_responses": result.audio_responses,
        "total_received_bytes": result.total_received_bytes,
        "output_duration_sec": result.output_duration_sec,
        # This is a whole-file output/input proxy. Source silence is included
        # in the denominator, so it is not a speech-only prosody measurement.
        "output_to_input_duration_ratio": result.tts_expansion_ratio,
        "average_drift_sec": result.avg_drift,
        "max_drift_sec": result.max_drift,
        "final_drift_sec": result.final_drift,
        "tail_lag_sec": result.tail_lag_sec,
        "first_audio_latency_sec": result.first_audio_latency_sec,
        "duration_excess_sec": result.duration_excess_sec,
        "playback_tail_sec": result.playback_tail_sec,
        "post_input_responses": result.post_input_responses,
        "input_completed": result.input_completed,
        "connection_lost": result.connection_lost,
        "drain_timed_out": result.drain_timed_out,
        "drain_duration_sec": result.drain_duration_sec,
        "translation_completed": result.translation_completed,
        "server_error": result.server_error,
    }
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    print(f"Saved: {output_path}")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def check_backend(backend_url: str):
    """Verify backend is reachable."""
    try:
        resp = requests.get(f"{backend_url}/api/config", timeout=5)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"ERROR: Cannot reach backend at {backend_url}: {e}")
        print("Make sure the backend is running:")
        print("  cd backend && uvicorn main:app --host 0.0.0.0 --port 8000")
        return False


async def run_preflight(backend_url: str) -> bool:
    """Run pre-flight validation with test-1min.wav."""
    print("\n=== Pre-flight Validation ===")
    if not Path(PREFLIGHT_FILE).exists():
        print(f"ERROR: Pre-flight file not found: {PREFLIGHT_FILE}")
        return False

    try:
        result = await run_test(PREFLIGHT_FILE, backend_url)
    except Exception as e:
        print(f"\nPre-flight FAILED: {e}")
        return False

    if result.audio_responses == 0:
        print(
            f"\nPre-flight FAILED: No audio responses received."
            f" Sent {result.chunks_sent} chunks but got 0 translated audio back."
            f"\nCheck that Riva services are running at the configured RIVA_URI."
        )
        return False

    if (
        not result.input_completed
        or result.connection_lost
        or result.drain_timed_out
        or not result.translation_completed
        or result.server_error
    ):
        print(
            "\nPre-flight FAILED: capture did not complete cleanly: "
            f"input_completed={result.input_completed}, "
            f"connection_lost={result.connection_lost}, "
            f"drain_timed_out={result.drain_timed_out}, "
            f"translation_completed={result.translation_completed}, "
            f"server_error={result.server_error or 'none'}"
        )
        return False

    print(
        f"Pre-flight PASSED: Received {result.audio_responses} audio responses, "
        f"avg drift {result.avg_drift:.1f}s, "
        f"output {result.output_duration_sec:.1f}s "
        f"({result.tts_expansion_ratio:.3f}x), "
        f"tail lag {result.tail_lag_sec:.1f}s"
    )
    return True


async def run_batch(files: list[str], backend_url: str, output_dir: str):
    """Run tests on a list of audio files sequentially."""
    total = len(files)
    for i, fpath in enumerate(files, 1):
        print(f"\n=== Test {i}/{total}: {Path(fpath).name} ===")

        if not Path(fpath).exists():
            print(f"SKIPPED: File not found: {fpath}")
            continue

        try:
            result = await run_test(fpath, backend_url)
        except Exception as e:
            print(f"\nERROR: {e}")
            continue

        # Generate outputs
        stem = Path(fpath).stem
        parent = Path(output_dir)
        parent.mkdir(parents=True, exist_ok=True)
        plot_path = str(parent / f"{stem}_latency.png")
        csv_path = str(parent / f"{stem}_results.csv")
        summary_path = str(parent / f"{stem}_summary.json")

        generate_plot(result, plot_path)
        generate_csv(result, csv_path)
        generate_summary(result, summary_path)

        print(
            f"Summary: avg_drift={result.avg_drift:.1f}s, "
            f"max_drift={result.max_drift:.1f}s, "
            f"final_drift={result.final_drift:.1f}s, "
            f"output={result.output_duration_sec:.1f}s, "
            f"expansion={result.tts_expansion_ratio:.3f}x, "
            f"tail_lag={result.tail_lag_sec:.1f}s, "
            f"first_audio={result.first_audio_latency_sec:.1f}s, "
            f"duration_excess={result.duration_excess_sec:.1f}s, "
            f"playback_tail={result.playback_tail_sec:.1f}s, "
            f"post_input_responses={result.post_input_responses}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Batch latency test for real-time audio translation"
    )
    parser.add_argument(
        "--preflight", action="store_true",
        help="Run pre-flight validation only (test-1min.wav)",
    )
    parser.add_argument(
        "--file", type=str,
        help="Test a single audio file instead of the full batch",
    )
    parser.add_argument(
        "--backend", type=str, default="http://localhost:8000",
        help="Backend URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--output-dir", type=str, default="test_results_nemotron",
        help="Directory for new CSV and plot outputs",
    )
    args = parser.parse_args()

    if not check_backend(args.backend):
        sys.exit(1)

    if args.preflight:
        ok = asyncio.run(run_preflight(args.backend))
        sys.exit(0 if ok else 1)

    if args.file:
        asyncio.run(run_batch([args.file], args.backend, args.output_dir))
    else:
        asyncio.run(run_batch(TEST_FILES, args.backend, args.output_dir))


if __name__ == "__main__":
    main()
