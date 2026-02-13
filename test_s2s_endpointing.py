#!/usr/bin/env python3
"""
Test whether the S2S (Speech-to-Speech) API respects EndpointingConfig.

Runs two S2S streaming tests with the first 60 seconds of audio:
  A: S2S with default endpointing (no endpointing_config set)
  B: S2S with aggressive endpointing config

Compares response timing to determine if endpointing config is respected.
"""

import time
import wave
import sys

import riva.client
import riva.client.proto.riva_asr_pb2 as riva_asr_pb2
import riva.client.proto.riva_nmt_pb2 as riva_nmt_pb2

RIVA_URI = "riva-host:50051"
AUDIO_FILE = "test_audio/long-form-03-30min.wav"
CHUNK_SIZE = 9600  # bytes (4800 samples * 2 bytes/sample = 300ms at 16kHz)
CHUNK_INTERVAL = 0.3  # seconds between chunks
AUDIO_DURATION_SEC = 60  # use first 60 seconds
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE  # 32000


def load_audio(filepath, duration_sec):
    """Load first N seconds of audio from WAV file."""
    max_bytes = duration_sec * BYTES_PER_SECOND
    with wave.open(filepath, 'rb') as wf:
        print(f"  WAV info: channels={wf.getnchannels()}, "
              f"sample_width={wf.getsampwidth()}, "
              f"framerate={wf.getframerate()}, "
              f"frames={wf.getnframes()}")
        assert wf.getnchannels() == 1, "Expected mono audio"
        assert wf.getsampwidth() == 2, "Expected 16-bit audio"
        assert wf.getframerate() == 16000, "Expected 16kHz audio"
        n_frames = min(wf.getnframes(), duration_sec * wf.getframerate())
        raw_audio = wf.readframes(n_frames)
    raw_audio = raw_audio[:max_bytes]
    print(f"  Loaded {len(raw_audio)} bytes = {len(raw_audio)/BYTES_PER_SECOND:.1f}s of audio")
    return raw_audio


def make_audio_iterator(raw_audio):
    """Yield audio chunks at ~300ms intervals. Config is handled by the library."""
    offset = 0
    chunk_count = 0
    while offset < len(raw_audio):
        chunk = raw_audio[offset:offset + CHUNK_SIZE]
        offset += CHUNK_SIZE
        chunk_count += 1
        time.sleep(CHUNK_INTERVAL)
        yield chunk

    print(f"  Sent {chunk_count} audio chunks ({offset} bytes)")


def build_s2s_config(endpointing_config=None):
    """Build S2S streaming config, optionally with endpointing."""
    recognition_kwargs = dict(
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hertz=SAMPLE_RATE,
        language_code="en-US",
        max_alternatives=1,
        enable_automatic_punctuation=True,
        audio_channel_count=1,
    )
    if endpointing_config is not None:
        recognition_kwargs["endpointing_config"] = endpointing_config

    asr_config = riva_asr_pb2.StreamingRecognitionConfig(
        config=riva_asr_pb2.RecognitionConfig(**recognition_kwargs),
        interim_results=True,
    )

    translation_config = riva_nmt_pb2.TranslationConfig(
        source_language_code="en-US",
        target_language_code="es-US",
        model_name="megatronnmt_any_any_1b",
    )

    tts_config = riva.client.SynthesizeSpeechConfig(
        language_code="es-US",
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hz=SAMPLE_RATE,
        voice_name="Magpie-Multilingual.ES-US.Isabela",
    )

    s2s_config = riva.client.StreamingTranslateSpeechToSpeechConfig(
        asr_config=asr_config,
        translation_config=translation_config,
        tts_config=tts_config,
    )

    return s2s_config


def run_s2s_test(label, raw_audio, s2s_config):
    """Run a single S2S streaming test and collect timing data."""
    print(f"\n{'='*70}")
    print(f"  TEST {label}")
    print(f"{'='*70}")

    auth = riva.client.Auth(uri=RIVA_URI)
    nmt_client = riva.client.NeuralMachineTranslationClient(auth)

    responses_data = []  # list of (timestamp_relative, audio_byte_length)
    start_time = time.time()

    audio_iter = make_audio_iterator(raw_audio)

    print(f"  Streaming audio to S2S API...")

    try:
        response_gen = nmt_client.streaming_s2s_response_generator(
            audio_chunks=audio_iter,
            streaming_config=s2s_config,
        )

        for response in response_gen:
            now = time.time() - start_time
            if hasattr(response, 'speech') and response.speech and hasattr(response.speech, 'audio') and response.speech.audio:
                audio_len = len(response.speech.audio)
                responses_data.append((now, audio_len))
                audio_dur = audio_len / BYTES_PER_SECOND
                print(f"    [{now:7.2f}s] Response #{len(responses_data):3d}: "
                      f"{audio_len:6d} bytes ({audio_dur:.2f}s audio)")

    except Exception as e:
        elapsed = time.time() - start_time
        print(f"  ERROR at {elapsed:.2f}s: {type(e).__name__}: {e}")

    total_time = time.time() - start_time
    print(f"  Completed in {total_time:.2f}s, got {len(responses_data)} responses with audio")

    return responses_data


def analyze_results(label, responses_data):
    """Analyze timing of responses."""
    result = {}
    result["label"] = label
    result["response_count"] = len(responses_data)

    if len(responses_data) == 0:
        result["total_audio_bytes"] = 0
        result["total_output_duration"] = 0.0
        result["timestamps"] = []
        result["gaps"] = []
        result["max_gap"] = 0.0
        result["avg_gap"] = 0.0
        result["gap_distribution"] = {k: 0 for k in ["<1s", "1-3s", "3-5s", "5-10s", "10-20s", "20+s"]}
        return result

    timestamps = [r[0] for r in responses_data]
    audio_bytes = [r[1] for r in responses_data]

    total_bytes = sum(audio_bytes)
    total_duration = total_bytes / BYTES_PER_SECOND

    result["total_audio_bytes"] = total_bytes
    result["total_output_duration"] = total_duration
    result["timestamps"] = timestamps

    # Compute gaps between consecutive responses
    gaps = []
    for i in range(1, len(timestamps)):
        gaps.append(timestamps[i] - timestamps[i - 1])

    result["gaps"] = gaps
    result["max_gap"] = max(gaps) if gaps else 0.0
    result["avg_gap"] = sum(gaps) / len(gaps) if gaps else 0.0

    # Gap distribution
    dist = {"<1s": 0, "1-3s": 0, "3-5s": 0, "5-10s": 0, "10-20s": 0, "20+s": 0}
    for g in gaps:
        if g < 1:
            dist["<1s"] += 1
        elif g < 3:
            dist["1-3s"] += 1
        elif g < 5:
            dist["3-5s"] += 1
        elif g < 10:
            dist["5-10s"] += 1
        elif g < 20:
            dist["10-20s"] += 1
        else:
            dist["20+s"] += 1
    result["gap_distribution"] = dist

    return result


def print_analysis(result):
    """Print detailed analysis for one test."""
    label = result["label"]
    print(f"\n--- Analysis: {label} ---")
    print(f"  Response count:       {result['response_count']}")
    print(f"  Total audio bytes:    {result['total_audio_bytes']:,}")
    print(f"  Total output dur:     {result['total_output_duration']:.2f}s")
    print(f"  Max gap:              {result['max_gap']:.2f}s")
    print(f"  Avg gap:              {result['avg_gap']:.2f}s")
    print(f"  Gap distribution:")
    for bucket, count in result["gap_distribution"].items():
        print(f"    {bucket:>6s}: {count}")

    if result["gaps"]:
        print(f"  First 10 gaps: {[f'{g:.2f}' for g in result['gaps'][:10]]}")
        if len(result["gaps"]) > 10:
            print(f"  Last 5 gaps:   {[f'{g:.2f}' for g in result['gaps'][-5:]]}")


def print_comparison_table(results):
    """Print side-by-side comparison table."""
    a, b = results[0], results[1]

    print(f"\n{'='*70}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*70}")

    gaps_gt5_a = sum(1 for g in a["gaps"] if g > 5)
    gaps_gt5_b = sum(1 for g in b["gaps"] if g > 5)
    gaps_gt10_a = sum(1 for g in a["gaps"] if g > 10)
    gaps_gt10_b = sum(1 for g in b["gaps"] if g > 10)

    header = f"| {'Metric':<30s} | {'A: S2S Default':>20s} | {'B: S2S Aggressive EP':>22s} |"
    separator = f"|{'-'*32}|{'-'*22}|{'-'*24}|"

    rows = [
        ("Response count", f"{a['response_count']}", f"{b['response_count']}"),
        ("Total output duration", f"{a['total_output_duration']:.2f}s", f"{b['total_output_duration']:.2f}s"),
        ("Total audio bytes", f"{a['total_audio_bytes']:,}", f"{b['total_audio_bytes']:,}"),
        ("Max gap", f"{a['max_gap']:.2f}s", f"{b['max_gap']:.2f}s"),
        ("Avg gap", f"{a['avg_gap']:.2f}s", f"{b['avg_gap']:.2f}s"),
        ("Gaps > 5s", f"{gaps_gt5_a}", f"{gaps_gt5_b}"),
        ("Gaps > 10s", f"{gaps_gt10_a}", f"{gaps_gt10_b}"),
        ("Gaps < 1s", f"{a['gap_distribution']['<1s']}", f"{b['gap_distribution']['<1s']}"),
        ("Gaps 1-3s", f"{a['gap_distribution']['1-3s']}", f"{b['gap_distribution']['1-3s']}"),
        ("Gaps 3-5s", f"{a['gap_distribution']['3-5s']}", f"{b['gap_distribution']['3-5s']}"),
        ("Gaps 5-10s", f"{a['gap_distribution']['5-10s']}", f"{b['gap_distribution']['5-10s']}"),
        ("Gaps 10-20s", f"{a['gap_distribution']['10-20s']}", f"{b['gap_distribution']['10-20s']}"),
        ("Gaps 20+s", f"{a['gap_distribution']['20+s']}", f"{b['gap_distribution']['20+s']}"),
    ]

    print(header)
    print(separator)
    for metric, val_a, val_b in rows:
        print(f"| {metric:<30s} | {val_a:>20s} | {val_b:>22s} |")
    print(separator)

    # Verdict
    print(f"\n{'='*70}")
    print(f"  VERDICT")
    print(f"{'='*70}")
    if a["response_count"] == 0 and b["response_count"] == 0:
        print("  Both tests returned zero responses. Cannot determine endpointing effect.")
    elif abs(a["response_count"] - b["response_count"]) <= 1 and abs(a["max_gap"] - b["max_gap"]) < 1.0:
        print("  Response counts and gaps are SIMILAR.")
        print("  --> EndpointingConfig likely has NO EFFECT on S2S API.")
    else:
        diff_count = b["response_count"] - a["response_count"]
        diff_gap = a["max_gap"] - b["max_gap"]
        print(f"  Response count difference: {diff_count:+d} (B vs A)")
        print(f"  Max gap difference:        {diff_gap:+.2f}s (A minus B)")
        if b["response_count"] > a["response_count"] * 1.3 or diff_gap > 2.0:
            print("  --> EndpointingConfig DOES appear to affect S2S API behavior.")
        else:
            print("  --> Results are somewhat different but not conclusive.")
            print("     Endpointing may have a minor effect on S2S API.")


def main():
    print("S2S EndpointingConfig Test")
    print("=" * 70)

    # Load audio
    print("\nLoading audio...")
    raw_audio = load_audio(AUDIO_FILE, AUDIO_DURATION_SEC)

    # ---------------------------------------------------------------
    # TEST A: Default endpointing
    # ---------------------------------------------------------------
    s2s_config_a = build_s2s_config(endpointing_config=None)
    responses_a = run_s2s_test("A: S2S Default Endpointing", raw_audio, s2s_config_a)

    print("\n  Pausing 5 seconds before next test...")
    time.sleep(5)

    # ---------------------------------------------------------------
    # TEST B: Aggressive endpointing
    # ---------------------------------------------------------------
    endpointing = riva_asr_pb2.EndpointingConfig(
        start_history=100,
        start_threshold=0.3,
        stop_history=300,
        stop_threshold=0.5,
        stop_history_eou=200,
        stop_threshold_eou=0.6,
    )
    s2s_config_b = build_s2s_config(endpointing_config=endpointing)
    responses_b = run_s2s_test("B: S2S Aggressive Endpointing", raw_audio, s2s_config_b)

    # ---------------------------------------------------------------
    # Analyze and compare
    # ---------------------------------------------------------------
    result_a = analyze_results("A: S2S Default", responses_a)
    result_b = analyze_results("B: S2S Aggressive EP", responses_b)

    print_analysis(result_a)
    print_analysis(result_b)
    print_comparison_table([result_a, result_b])

    print("\nDone.")


if __name__ == "__main__":
    main()
