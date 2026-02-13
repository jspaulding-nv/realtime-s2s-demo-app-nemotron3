#!/usr/bin/env python3
"""Automated latency/drift test -- replicates what the browser TestDashboard does.

Usage:
    python run_latency_test.py [--duration 60] [--host 127.0.0.1:8000]

Sends audio chunks from a WAV file at real-time speed over WebSocket,
collects translated audio responses, and measures drift.
"""

import argparse
import asyncio
import csv
import json
import struct
import time
import wave
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print("ERROR: websockets package required. Install with: pip install websockets")
    raise SystemExit(1)

try:
    from urllib.request import urlopen, Request
except ImportError:
    pass

SAMPLE_RATE = 16000
CHUNK_SIZE = 4800  # samples
BYTES_PER_SAMPLE = 2  # Int16
CHUNK_BYTES = CHUNK_SIZE * BYTES_PER_SAMPLE  # 9600
CHUNK_DURATION = CHUNK_SIZE / SAMPLE_RATE  # 0.3s

SCRIPT_DIR = Path(__file__).parent


def http_post(url: str) -> dict:
    req = Request(url, method="POST", data=b"")
    resp = urlopen(req)
    return json.loads(resp.read())


def http_get(url: str) -> dict:
    resp = urlopen(url)
    return json.loads(resp.read())


def load_wav(path: Path, max_duration: float = 0) -> bytes:
    """Load a WAV file and return raw PCM bytes. Optionally truncate."""
    with wave.open(str(path), "rb") as wf:
        assert wf.getsampwidth() == 2, f"Expected 16-bit WAV, got {wf.getsampwidth()*8}-bit"
        assert wf.getnchannels() == 1, f"Expected mono, got {wf.getnchannels()} channels"
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        if max_duration > 0:
            n_frames = min(n_frames, int(max_duration * sr))
        pcm = wf.readframes(n_frames)
        if sr != SAMPLE_RATE:
            print(f"WARNING: WAV sample rate is {sr}, expected {SAMPLE_RATE}")
        return pcm


def format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


async def run_test(host: str, wav_path: Path, max_duration: float):
    base = f"http://{host}"
    ws_url = f"ws://{host}/ws/translate"

    # Load audio
    print(f"Loading {wav_path.name}...")
    pcm_data = load_wav(wav_path, max_duration)
    total_samples = len(pcm_data) // BYTES_PER_SAMPLE
    total_chunks = total_samples // CHUNK_SIZE
    file_duration = total_samples / SAMPLE_RATE
    print(f"  Duration: {format_time(file_duration)} ({file_duration:.1f}s)")
    print(f"  Chunks: {total_chunks}")
    print()

    # Start backend test mode
    print("Starting test session...")
    http_post(f"{base}/api/test/start")

    # Connect WebSocket
    print(f"Connecting to {ws_url}...")
    ws = await websockets.connect(ws_url)

    # Wait for connected status
    msg = await ws.recv()
    data = json.loads(msg)
    print(f"  Status: {data.get('status', 'unknown')}")

    # Start translation stream
    await ws.send(json.dumps({"type": "start_stream", "targetLanguage": "es-US"}))
    msg = await ws.recv()
    data = json.loads(msg)
    print(f"  Status: {data.get('status', 'unknown')} - {data.get('message', '')}")
    print()

    # Tracking state
    chunks_sent = 0
    responses_received = 0
    total_output_bytes = 0
    test_start = time.monotonic()
    initial_drift = None
    max_drift = 0.0
    drift_history = []
    last_print = 0.0

    # Task to receive audio responses
    receive_done = asyncio.Event()
    receive_stopped = False

    async def receive_loop():
        nonlocal responses_received, total_output_bytes, initial_drift, max_drift
        try:
            while not receive_stopped:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    responses_received += 1
                    total_output_bytes += len(msg)
                elif isinstance(msg, str):
                    # JSON control message (status, level, etc.)
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            receive_done.set()

    receiver = asyncio.create_task(receive_loop())

    # Send chunks at real-time speed
    print("Streaming audio...")
    print("-" * 70)
    header = f"{'Elapsed':>8}  {'Sent':>6}  {'Recv':>6}  {'Output(s)':>9}  {'Raw Drift':>10}  {'Acc Drift':>10}"
    print(header)
    print("-" * 70)

    expected_time = time.monotonic() + CHUNK_DURATION

    for i in range(total_chunks):
        offset = i * CHUNK_BYTES
        chunk = pcm_data[offset:offset + CHUNK_BYTES]

        try:
            await ws.send(chunk)
        except websockets.exceptions.ConnectionClosed:
            print("\nWebSocket closed unexpectedly!")
            break

        chunks_sent += 1
        elapsed = time.monotonic() - test_start

        # Compute drift
        output_duration = total_output_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        raw_drift = elapsed - output_duration

        if initial_drift is None and output_duration > 0:
            initial_drift = raw_drift

        acc_drift = raw_drift - initial_drift if initial_drift is not None else 0.0
        max_drift = max(max_drift, abs(acc_drift))

        # Record drift point
        drift_history.append({
            "elapsed_sec": elapsed,
            "raw_drift_sec": raw_drift,
            "acc_drift_sec": acc_drift,
            "chunks_sent": chunks_sent,
            "responses_received": responses_received,
            "output_duration_sec": output_duration,
        })

        # Print status every ~2 seconds
        if elapsed - last_print >= 2.0 or i == total_chunks - 1:
            last_print = elapsed
            print(
                f"{format_time(elapsed):>8}  "
                f"{chunks_sent:>6}  "
                f"{responses_received:>6}  "
                f"{output_duration:>9.2f}  "
                f"{raw_drift:>9.2f}s  "
                f"{acc_drift:>9.2f}s"
            )

        # Self-correcting timer
        drift = time.monotonic() - expected_time
        expected_time += CHUNK_DURATION
        sleep_time = max(0, CHUNK_DURATION - drift)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

    print("-" * 70)
    print(f"Finished sending {chunks_sent} chunks.")
    print()

    # Tell Riva we're done sending so it flushes remaining audio
    print("Signaling end of input (stop_stream)...")
    try:
        await ws.send(json.dumps({"type": "stop_stream"}))
    except Exception:
        pass

    # Wait for Riva to flush all remaining responses.
    # Use a 10-second quiet period since Riva sends in bursts with long gaps.
    QUIET_TIMEOUT = 10
    MAX_WAIT = 120  # absolute max wait
    print(f"Waiting for remaining responses (quiet={QUIET_TIMEOUT}s, max={MAX_WAIT}s)...")
    wait_start = time.monotonic()
    last_received_time = time.monotonic()

    while True:
        await asyncio.sleep(2)
        elapsed = time.monotonic() - test_start
        output_duration = total_output_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        current_recv = responses_received

        print(
            f"  ... recv={current_recv}, output={output_duration:.2f}s, "
            f"elapsed={format_time(elapsed)}"
        )

        if current_recv != getattr(run_test, '_prev_recv', -1):
            last_received_time = time.monotonic()
        run_test._prev_recv = current_recv

        # Stop if no new responses for QUIET_TIMEOUT seconds
        if current_recv > 0 and (time.monotonic() - last_received_time) >= QUIET_TIMEOUT:
            print(f"  No new responses for {QUIET_TIMEOUT}s -- Riva flush complete.")
            break

        # Absolute safety limit
        if time.monotonic() - wait_start >= MAX_WAIT:
            print(f"  Max wait reached ({MAX_WAIT}s).")
            break

    # Signal receiver to stop
    receive_stopped = True
    receiver.cancel()
    try:
        await receiver
    except asyncio.CancelledError:
        pass

    # Close WebSocket
    try:
        await ws.close()
    except Exception:
        pass

    http_post(f"{base}/api/test/stop")

    # Final stats
    elapsed = time.monotonic() - test_start
    output_duration = total_output_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)
    raw_drift = elapsed - output_duration
    acc_drift = raw_drift - initial_drift if initial_drift is not None else 0.0

    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"  File duration:         {format_time(file_duration)} ({file_duration:.1f}s)")
    print(f"  Test elapsed:          {format_time(elapsed)} ({elapsed:.1f}s)")
    print(f"  Chunks sent:           {chunks_sent}")
    print(f"  Responses received:    {responses_received}")
    print(f"  Output audio duration: {output_duration:.2f}s")
    print(f"  Final raw drift:       {raw_drift:.2f}s")
    print(f"  Final acc. drift:      {acc_drift:.2f}s")
    print(f"  Max acc. drift:        {max_drift:.2f}s")
    print()

    if max_drift > 30:
        print("  *** DANGER: Drift exceeds 30s threshold ***")
    elif max_drift > 20:
        print("  ** WARNING: Drift exceeds 20s threshold **")
    elif responses_received == 0:
        print("  * NOTE: No responses received -- Riva may not be processing audio *")
    else:
        print("  Drift within acceptable range.")

    # Export backend events
    print()
    print("Exporting data...")
    export = http_get(f"{base}/api/test/export")
    backend_events = export.get("events", [])

    # Write CSV
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = SCRIPT_DIR / f"latency-test-{timestamp}.csv"

    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "source", "stage", "elapsed_sec", "chunk_index",
            "source_position_sec", "audio_bytes", "raw_drift_sec", "acc_drift_sec",
        ])

        # Client-side drift data
        for d in drift_history:
            writer.writerow([
                "client", "drift_sample", f"{d['elapsed_sec']:.3f}", d["chunks_sent"],
                f"{d['chunks_sent'] * CHUNK_DURATION:.3f}", "",
                f"{d['raw_drift_sec']:.3f}", f"{d['acc_drift_sec']:.3f}",
            ])

        # Backend events
        for e in backend_events:
            writer.writerow([
                "backend", e["stage"], f"{e['wall_clock']:.3f}", e["chunk_index"],
                f"{e['source_position_sec']:.3f}", e["audio_bytes_len"], "", "",
            ])

    print(f"  Saved: {csv_path}")
    print(f"  Client data points: {len(drift_history)}")
    print(f"  Backend events: {len(backend_events)}")
    print()
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Automated latency/drift test")
    parser.add_argument(
        "--duration", type=float, default=0,
        help="Max duration in seconds (0 = full file)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1:8000",
        help="Backend host:port (default: 127.0.0.1:8000)",
    )
    parser.add_argument(
        "--file", type=str, default=None,
        help="Path to WAV file (default: test_audio/test-1min.wav)",
    )
    args = parser.parse_args()

    if args.file:
        wav_path = Path(args.file)
    else:
        wav_path = SCRIPT_DIR / "test_audio" / "test-1min.wav"

    if not wav_path.exists():
        print(f"ERROR: WAV file not found: {wav_path}")
        raise SystemExit(1)

    asyncio.run(run_test(args.host, wav_path, args.duration))


if __name__ == "__main__":
    main()
