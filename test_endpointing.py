#!/usr/bin/env python3
"""
Test Riva EndpointingConfig: compare ASR streaming with and without endpointing.

Uses the StreamingTranslateSpeechToText endpoint (since this Riva deployment
only exposes ASR through the NMT S2S/S2T pipelines, not standalone).

Streams 60 seconds of audio from a WAV file and records when final results arrive
under three configurations:
  A) Default (no endpointing config)
  B) Aggressive endpointing
  C) Very aggressive endpointing
"""

import os
import time
import wave
from pathlib import Path
import riva.client
import riva.client.proto.riva_asr_pb2 as riva_asr_pb2
import riva.client.proto.riva_nmt_pb2 as riva_nmt_pb2

# -- Configuration -----------------------------------------------------------
RIVA_URI = os.environ.get("RIVA_URI", "localhost:50051")
WAV_PATH = os.environ.get(
    "S2S_ENDPOINT_AUDIO",
    str(Path(os.environ.get("S2S_TEST_AUDIO_DIR", "test_audio")) / "long-form-03-30min.wav"),
)
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 4800          # 300 ms
CHUNK_BYTES = CHUNK_SAMPLES * 2  # int16 = 2 bytes/sample
SECONDS_TO_READ = 60
TOTAL_BYTES = SECONDS_TO_READ * SAMPLE_RATE * 2  # 1,920,000 bytes
CHUNK_INTERVAL = CHUNK_SAMPLES / SAMPLE_RATE      # 0.3 s

SOURCE_LANGUAGE = "en-US"
TARGET_LANGUAGE = "es-US"
NMT_MODEL = "megatronnmt_any_any_1b"


def load_audio(path, max_bytes):
    """Read raw PCM bytes from a 16 kHz mono int16 WAV file."""
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1, f"Expected mono, got {wf.getnchannels()} channels"
        assert wf.getsampwidth() == 2, f"Expected 16-bit, got {wf.getsampwidth()*8}-bit"
        assert wf.getframerate() == SAMPLE_RATE, f"Expected {SAMPLE_RATE} Hz, got {wf.getframerate()} Hz"
        frames = wf.readframes(max_bytes // 2)  # readframes takes nframes
    print(f"Loaded {len(frames)} bytes ({len(frames)/2/SAMPLE_RATE:.1f}s) from {path}")
    return frames[:max_bytes]


def audio_chunks(pcm):
    """Yield PCM in CHUNK_BYTES-sized pieces, sleeping to simulate real-time."""
    offset = 0
    while offset < len(pcm):
        end = min(offset + CHUNK_BYTES, len(pcm))
        yield pcm[offset:end]
        offset = end
        time.sleep(CHUNK_INTERVAL)


def run_s2t_test(label, pcm, endpointing_config=None):
    """
    Run a single streaming speech-to-text test via the NMT S2T endpoint
    and return list of (elapsed_s, text) finals.
    """
    print(f"\n{'='*72}")
    print(f"  {label}")
    print(f"{'='*72}")

    auth = riva.client.Auth(uri=RIVA_URI)
    nmt_client = riva.client.NeuralMachineTranslationClient(auth)

    # Build RecognitionConfig
    rec_kwargs = dict(
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hertz=SAMPLE_RATE,
        language_code=SOURCE_LANGUAGE,
        max_alternatives=1,
        enable_automatic_punctuation=True,
        audio_channel_count=1,
    )
    if endpointing_config is not None:
        rec_kwargs["endpointing_config"] = endpointing_config

    asr_config = riva_asr_pb2.StreamingRecognitionConfig(
        config=riva_asr_pb2.RecognitionConfig(**rec_kwargs),
        interim_results=False,  # only finals
    )

    translation_config = riva_nmt_pb2.TranslationConfig(
        source_language_code=SOURCE_LANGUAGE,
        target_language_code=TARGET_LANGUAGE,
        model_name=NMT_MODEL,
    )

    s2t_config = riva.client.StreamingTranslateSpeechToTextConfig(
        asr_config=asr_config,
        translation_config=translation_config,
    )

    finals = []
    all_results = []  # track interims too for debugging
    t_start = time.time()

    print(f"Streaming {len(pcm)} bytes ({len(pcm)/2/SAMPLE_RATE:.1f}s) "
          f"in {CHUNK_BYTES}-byte chunks at {CHUNK_INTERVAL:.2f}s intervals ...")
    print(f"  ASR: {SOURCE_LANGUAGE} -> NMT: {TARGET_LANGUAGE}")
    if endpointing_config:
        print(f"  Endpointing: start_history={endpointing_config.start_history}, "
              f"start_thresh={endpointing_config.start_threshold}, "
              f"stop_history={endpointing_config.stop_history}, "
              f"stop_thresh={endpointing_config.stop_threshold}, "
              f"stop_history_eou={endpointing_config.stop_history_eou}, "
              f"stop_thresh_eou={endpointing_config.stop_threshold_eou}")
    else:
        print(f"  Endpointing: default (none specified)")
    print()

    responses = nmt_client.streaming_s2t_response_generator(
        audio_chunks=audio_chunks(pcm),
        streaming_config=s2t_config,
    )

    for resp in responses:
        for result in resp.results:
            t_now = time.time()
            elapsed = t_now - t_start
            text = result.alternatives[0].transcript.strip() if result.alternatives else ""

            if result.is_final:
                finals.append((elapsed, text))
                print(f"  [{elapsed:6.2f}s] FINAL: {text}")
            else:
                all_results.append((elapsed, False, text))

    t_total = time.time() - t_start
    print(f"\nFinished in {t_total:.1f}s  --  {len(finals)} final result(s)")

    # -- Per-final summary ---------------------------------------------------
    if finals:
        gaps = []
        for i in range(1, len(finals)):
            gap = finals[i][0] - finals[i - 1][0]
            gaps.append(gap)

        print(f"\n  {'#':>3}  {'Time':>7}  {'Gap':>6}  Transcript")
        print(f"  {'---':>3}  {'-------':>7}  {'------':>6}  ----------")
        for i, (ts, txt) in enumerate(finals):
            gap_str = f"{gaps[i-1]:.2f}s" if i > 0 else "   --"
            print(f"  {i+1:3d}  {ts:6.2f}s  {gap_str:>6}  {txt[:80]}")

        if gaps:
            print(f"\n  Gap stats: min={min(gaps):.2f}s  avg={sum(gaps)/len(gaps):.2f}s  max={max(gaps):.2f}s")
        print(f"  First final at: {finals[0][0]:.2f}s")
        print(f"  Last final at:  {finals[-1][0]:.2f}s")

    return finals


def comparison_table(results):
    """Print a side-by-side comparison table."""
    print(f"\n\n{'#'*72}")
    print(f"  COMPARISON TABLE")
    print(f"{'#'*72}\n")

    labels = list(results.keys())
    header = f"  {'Metric':<35}"
    for label in labels:
        header += f"  {label:>14}"
    print(header)
    print(f"  {'-'*35}" + f"  {'-'*14}" * len(labels))

    # Number of finals
    row = f"  {'Finals emitted':<35}"
    for label in labels:
        row += f"  {len(results[label]):>14d}"
    print(row)

    # First final timestamp
    row = f"  {'First final (s)':<35}"
    for label in labels:
        finals = results[label]
        val = f"{finals[0][0]:.2f}" if finals else "N/A"
        row += f"  {val:>14}"
    print(row)

    # Last final timestamp
    row = f"  {'Last final (s)':<35}"
    for label in labels:
        finals = results[label]
        val = f"{finals[-1][0]:.2f}" if finals else "N/A"
        row += f"  {val:>14}"
    print(row)

    # Avg gap
    row = f"  {'Avg gap between finals (s)':<35}"
    for label in labels:
        finals = results[label]
        if len(finals) > 1:
            gaps = [finals[i][0] - finals[i-1][0] for i in range(1, len(finals))]
            val = f"{sum(gaps)/len(gaps):.2f}"
        else:
            val = "N/A"
        row += f"  {val:>14}"
    print(row)

    # Max gap
    row = f"  {'Max gap between finals (s)':<35}"
    for label in labels:
        finals = results[label]
        if len(finals) > 1:
            gaps = [finals[i][0] - finals[i-1][0] for i in range(1, len(finals))]
            val = f"{max(gaps):.2f}"
        else:
            val = "N/A"
        row += f"  {val:>14}"
    print(row)

    # Min gap
    row = f"  {'Min gap between finals (s)':<35}"
    for label in labels:
        finals = results[label]
        if len(finals) > 1:
            gaps = [finals[i][0] - finals[i-1][0] for i in range(1, len(finals))]
            val = f"{min(gaps):.2f}"
        else:
            val = "N/A"
        row += f"  {val:>14}"
    print(row)

    # Total transcript length (chars)
    row = f"  {'Total transcript chars':<35}"
    for label in labels:
        finals = results[label]
        total = sum(len(t) for _, t in finals)
        row += f"  {total:>14d}"
    print(row)

    # Average transcript length per final
    row = f"  {'Avg chars per final':<35}"
    for label in labels:
        finals = results[label]
        if finals:
            avg = sum(len(t) for _, t in finals) / len(finals)
            val = f"{avg:.1f}"
        else:
            val = "N/A"
        row += f"  {val:>14}"
    print(row)

    print()


def main():
    pcm = load_audio(WAV_PATH, TOTAL_BYTES)

    all_results = {}

    # -- Test A: Default (no endpointing) ------------------------------------
    finals_a = run_s2t_test("Test A: Default ASR (no EndpointingConfig)", pcm)
    all_results["A: Default"] = finals_a

    # -- Test B: Aggressive endpointing --------------------------------------
    ep_b = riva_asr_pb2.EndpointingConfig(
        start_history=200,
        start_threshold=0.5,
        stop_history=600,
        stop_threshold=0.7,
        stop_history_eou=400,
        stop_threshold_eou=0.8,
    )
    finals_b = run_s2t_test("Test B: Aggressive endpointing", pcm, endpointing_config=ep_b)
    all_results["B: Aggressive"] = finals_b

    # -- Test C: Very aggressive endpointing ---------------------------------
    ep_c = riva_asr_pb2.EndpointingConfig(
        start_history=100,
        start_threshold=0.3,
        stop_history=300,
        stop_threshold=0.5,
        stop_history_eou=200,
        stop_threshold_eou=0.6,
    )
    finals_c = run_s2t_test("Test C: Very aggressive endpointing", pcm, endpointing_config=ep_c)
    all_results["C: Very Aggr."] = finals_c

    # -- Comparison ----------------------------------------------------------
    comparison_table(all_results)

    # Verdict
    print("VERDICT:")
    for label, finals in all_results.items():
        print(f"  {label}: {len(finals)} finals", end="")
        if len(finals) > 1:
            gaps = [finals[i][0] - finals[i-1][0] for i in range(1, len(finals))]
            print(f"  (avg gap {sum(gaps)/len(gaps):.2f}s, max gap {max(gaps):.2f}s)")
        else:
            print()

    counts = [len(f) for f in all_results.values()]
    if counts[1] > counts[0]:
        print("\n  --> Aggressive endpointing (B) produces MORE frequent finals than default (A).")
    elif counts[1] == counts[0]:
        print("\n  --> Aggressive endpointing (B) produces the SAME number of finals as default (A).")
    else:
        print("\n  --> Aggressive endpointing (B) produces FEWER finals than default (A).")

    if counts[2] > counts[1]:
        print("  --> Very aggressive endpointing (C) produces MORE frequent finals than aggressive (B).")
    elif counts[2] == counts[1]:
        print("  --> Very aggressive endpointing (C) produces the SAME number of finals as aggressive (B).")
    else:
        print("  --> Very aggressive endpointing (C) produces FEWER finals than aggressive (B).")

    print("\nDone.")


if __name__ == "__main__":
    main()
