# Riva ASR Endpointing Research Results

**Date:** 2026-02-12
**Context:** Investigating 15-35 second stalls found in 30-minute latency test

---

## Problem

The S2S (Speech-to-Speech) pipeline experiences 15-35 second stalls where no translated audio is produced. Root cause analysis of the test CSV confirmed with 100% correlation that Riva's ASR accumulates audio for 15-35 seconds before emitting a transcription, causing the entire NMT+TTS pipeline to stall.

## Key Finding: S2S Only Translates on Final ASR Results

The S2S pipeline (`StreamingTranslateSpeechToSpeech`) **only translates on final ASR results** (`is_final=true`), NOT on interim results.

Evidence:
- `StreamingTranslateSpeechToSpeechResponse` contains only `speech` (synthesized audio bytes) and `id` — no `is_final` field, no interim/final semantics
- The companion S2T API DOES have `is_final` flags, confirming this is by design
- Setting `interim_results=True` in ASR config does NOT help S2S latency — it only affects what the S2T API returns

**Implication:** The only way to reduce stalls is to make ASR emit final results more frequently by tuning endpointing sensitivity.

## EndpointingConfig (The Solution)

`RecognitionConfig` has an optional `endpointing_config` field of type `EndpointingConfig` with six fields:

| Field | Type | Description |
|---|---|---|
| `start_history` | int32 (ms) | Window size to detect speech start |
| `start_threshold` | float (0-1) | % of non-blank frames to trigger start |
| `stop_history` | int32 (ms) | Window size to detect speech end — triggers `is_final=true` |
| `stop_threshold` | float (0-1) | % of blank frames to trigger end |
| `stop_history_eou` | int32 (ms) | Shorter window for early end-of-utterance (must be < stop_history) |
| `stop_threshold_eou` | float (0-1) | Threshold for early EOU |

### How Endpointing Works

- The acoustic model produces frame-level outputs: "blank" (silence/non-speech) or non-blank (speech)
- A sliding window of `stop_history` ms is examined
- If the % of blank frames exceeds `stop_threshold`, speech end is detected
- The decoder resets and a final result (`is_final=true`) is emitted
- The EOU fields provide a two-pass system: `stop_history_eou` (shorter window) triggers an early intermediate-final, while `stop_history` triggers the true final

### Tuning Guidance

- **Lower `stop_history`** = faster end-of-utterance detection = shorter segments = lower latency, but may split mid-sentence
- **Higher `stop_threshold`** = requires more silence frames = fewer false endpoints
- **Lower `stop_threshold`** = more sensitive to brief pauses = more frequent splits
- For real-time translation, recommended starting point: `stop_history=400-800` with `stop_threshold=0.5-0.8`

### No Max Utterance Duration

There is **no `max_speech_duration` or `utterance_timeout` field** in the protobuf definitions. The only way to control utterance length is through endpointing sensitivity (silence detection). If the speaker never pauses, the ASR will accumulate indefinitely regardless of endpointing config.

## Full RecognitionConfig Field Inventory

| Field | Type | # | Description |
|---|---|---|---|
| `encoding` | AudioEncoding | 1 | Audio encoding (LINEAR_PCM, etc.) |
| `sample_rate_hertz` | int32 | 2 | Sample rate in Hz |
| `language_code` | string | 3 | BCP-47 language code |
| `max_alternatives` | int32 | 4 | Max recognition hypotheses |
| `profanity_filter` | bool | 5 | Filter profanities |
| `speech_contexts` | repeated SpeechContext | 6 | Word boosting contexts |
| `audio_channel_count` | int32 | 7 | Number of audio channels |
| `enable_word_time_offsets` | bool | 8 | Return word-level timestamps |
| `enable_automatic_punctuation` | bool | 11 | Add punctuation |
| `enable_separate_recognition_per_channel` | bool | 12 | Not yet supported |
| `model` | string | 13 | Model name |
| `verbatim_transcripts` | bool | 14 | Disable inverse text normalization |
| `diarization_config` | SpeakerDiarizationConfig | 19 | Speaker diarization |
| `custom_configuration` | map<string, string> | 24 | Custom key-value pairs for plugins |
| `endpointing_config` | optional EndpointingConfig | 25 | Utterance boundary detection |

## StreamingTranslateSpeechToSpeechConfig Structure

| Field | Type | # | Description |
|---|---|---|---|
| `asr_config` | StreamingRecognitionConfig | 1 | Full ASR config including endpointing |
| `tts_config` | SynthesizeSpeechConfig | 2 | TTS configuration |
| `translation_config` | TranslationConfig | 3 | NMT config |

The ASR endpointing config passes through — setting `endpointing_config` on `asr_config` inside the S2S config controls when the internal ASR produces final results.

## Server-Side vs Client-Side Configuration

- **Server defaults** are set during `riva-build` deployment (e.g., `--endpointing.start_history=200`)
- **Client overrides** via `endpointing_config` in `RecognitionConfig` take precedence per-request
- `custom_configuration` map passes additional key-value pairs to server-side plugins (keys not publicly documented)

## Test 2: Moderate EndpointingConfig — FAILED

Applied to 30-minute test:

```python
endpointing_config = riva_asr_pb2.EndpointingConfig(
    start_history=200,       # 200ms — detect speech start quickly
    start_threshold=0.5,     # 50% non-blank frames to trigger start
    stop_history=600,        # 600ms — detect silence faster than default
    stop_threshold=0.7,      # 70% blank frames to trigger end
    stop_history_eou=400,    # 400ms — early end-of-utterance (< stop_history)
    stop_threshold_eou=0.8,  # 80% blank for early EOU
)
```

**Result: No improvement.** Max stall increased from 35.5s to 37.2s. Stalls > 15s increased from 15 to 22. These values were too conservative — `stop_history=600` and `stop_threshold=0.7` actually made the endpointer *less* sensitive than the server default.

See `30-min-test-2.MD` for full results.

---

## Verification: Does S2S Respect EndpointingConfig?

Ran controlled 60-second tests comparing ASR-only and S2S APIs with different endpointing configs.

### ASR-Only Test (60 seconds of audio)

| Metric | A: Default | B: Moderate (Test 2 values) | C: Aggressive |
|---|---|---|---|
| Finals emitted | 11 | 10 | **18** |
| Avg gap between finals | 5.71s | 6.37s | **3.34s** |
| Max gap between finals | 16.48s | 21.13s | **7.13s** |
| Avg chars per final | 58.4 | 68.1 | **37.3** |

**Finding:** Moderate values (Test B) were worse than default — confirming why Test 2 failed. Aggressive values (Test C) nearly doubled the finals count and halved the max gap.

### S2S Test (60 seconds of audio)

| Metric | A: S2S Default | B: S2S Aggressive |
|---|---|---|
| Response count | 371 | **388** |
| Total output duration | 47.42s | 48.48s |
| Max gap | **16.29s** | **7.04s** |
| Avg gap | 0.16s | 0.15s |
| Gaps > 5s | 4 | **1** |
| Gaps > 10s | 1 | **0** |
| Gaps 1-3s | 5 | 8 |
| Gaps 3-5s | 1 | 8 |

**Conclusion: S2S DOES respect client-side EndpointingConfig.** The aggressive config:
- Cut max gap from 16.3s to 7.0s (57% reduction)
- Eliminated all gaps > 10s
- Reduced gaps > 5s by 75% (4 → 1)
- Trade-off: more medium gaps (1-5s) as long utterances are split into shorter segments

### Why Test 2 Failed

The moderate values (`stop_history=600, stop_threshold=0.7`) were **more conservative than the server default**. A higher `stop_threshold` (0.7) means "require 70% blank frames" — this is harder to trigger than whatever the default is (likely 0.5 or lower). Combined with `stop_history=600` (a relatively long window), the endpointer needed *more* silence evidence than the default, not less.

---

## Applied Configuration (Test 3)

Using the aggressive values proven to work in both ASR-only and S2S controlled tests:

```python
endpointing_config = riva_asr_pb2.EndpointingConfig(
    start_history=100,       # 100ms — very fast speech start detection
    start_threshold=0.3,     # 30% non-blank frames triggers start
    stop_history=300,        # 300ms silence triggers final result
    stop_threshold=0.5,      # 50% blank frames triggers end
    stop_history_eou=200,    # 200ms early end-of-utterance
    stop_threshold_eou=0.6,  # 60% blank for early EOU
)
```

**Expected effect based on 60s S2S test:** Max gap should drop from ~35s to ~7s. Gaps > 10s should be eliminated. More frequent 1-5s gaps expected as trade-off (shorter utterance segments).

---

## Test 3 Results: Aggressive Endpointing — Partial Success

### Three-Way Comparison

| Metric | Test 1 (default) | Test 2 (moderate) | Test 3 (aggressive) | T1→T3 |
|---|---|---|---|---|
| Stalls > 3s | 184 | 178 | 203 | +10% (worse) |
| Stalls > 5s | ~100 | — | 122 | +22% (worse) |
| Stalls > 10s | ~50 | — | 34 | **-32% (better)** |
| Stalls > 15s | 15 | 22 | 6 | **-60% (better)** |
| Stalls > 20s | ~6 | — | 2 | **-67% (better)** |
| Max stall | 35.5s | 37.2s | 31.0s | **-4.5s (better)** |
| Top 5 stalls | 35.5, 31.3, 28.4, 23.3, 21.6 | 37.2, 34.2, 31.2, 27.1, 26.8 | 31.0, 23.3, 17.8, 16.5, 16.0 | Tail compressed |
| Drift slope | -4.63 s/min | -4.66 s/min | -4.75 s/min | Unchanged |

### Analysis

**Severe stalls significantly reduced.** The tail of the distribution compressed dramatically — stalls >15s dropped 60%, stalls >20s dropped 67%. The top-5 worst stalls improved from [35.5, 31.3, 28.4, 23.3, 21.6] to [31.0, 23.3, 17.8, 16.5, 16.0].

**Moderate stalls increased.** Stalls in the 3-10s range grew because shorter utterance segments mean more NMT+TTS round-trips, each with inherent processing latency. This is the expected trade-off.

**Drift unchanged.** Endpointing controls *when* Riva processes, not *how fast*. The NMT+TTS pipeline overhead per utterance is fixed.

### 60-Second Test vs 30-Minute Test Discrepancy

The 60-second controlled S2S test predicted max gap of ~7s, but the 30-minute test still showed a 31s max gap. Possible explanations:
1. Longer sessions accumulate more audio in Riva's internal buffers
2. Certain speech patterns (long continuous passages with no micro-pauses) defeat even aggressive endpointing
3. The 60-second test may have been too short to encounter the worst-case speech segments

### Conclusion

EndpointingConfig is a valid lever but insufficient alone. The aggressive config is the best of the three tested — it should be kept. Further improvement requires either:
1. Even more aggressive values (diminishing returns, risk of translation quality degradation)
2. Decomposed pipeline (separate ASR → NMT → TTS calls for per-stage control)
3. Server-side Riva tuning
4. Client-side buffering to smooth the stall-burst pattern
