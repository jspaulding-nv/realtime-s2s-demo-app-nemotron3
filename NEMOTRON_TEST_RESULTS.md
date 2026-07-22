# Nemotron S2S long-form test results

**Test date:** 2026-07-08

**Source:** @jgough-essextec's `realtime-s2s-demo-app`

**Pipeline:** Nemotron ASR Streaming 1.2.0 -> Riva Translate 1.6B NMT 1.5.2 -> Magpie multilingual TTS 1.7.0

## Configuration

- English Nemotron streaming profile, batch size 32
- English (`en-US`) to Spanish (`es-US`)
- `Magpie-Multilingual.ES-US.Isabela`
- 16 kHz mono Int16 PCM, streamed in 300 ms chunks at real-time pace
- Automatic punctuation enabled
- Final end-of-utterance window: 800 ms
- One sample streamed at a time

Nemotron is RNNT-based, so the client uses the documented final EOU setting
(`stop_history=800`) and does not send the CTC-only two-pass
`stop_history_eou` fields.

## New run results

| File | Input | First audio | Service flush tail | Output/input | Duration excess | Simulated playback tail | Max positive drift |
|---|---:|---:|---:|---:|---:|---:|---:|
| Sample 01 | 1,908.4s | 16.1s | 0.0s | 1.056x | 107.5s | 172.8s | 65.4s |
| Sample 02 | 2,427.0s | 2.3s | 0.4s | 1.081x | 197.2s | 228.8s | 31.3s |
| Sample 03 | 1,888.1s | 4.7s | 0.8s | 1.017x | 31.4s | 70.6s | 39.0s |

All three streams completed without a WebSocket drop or gRPC failure.

`Service flush tail` measures how long the server continued emitting audio
after end-of-input was explicitly signaled. `Simulated playback tail` replays
the received audio events through the same gapless queue model used by the
browser. It includes initial response latency, delivery stalls, and audio
duration expansion, and is the closest metric to what a listener experiences.

## Comparison with @jgough-essextec's three prior runs

Playback tails were reconstructed before raw captures were removed from public
history, using the same queue simulation as the new run. Only aggregate values
are retained here.

| File | Prior tails | Prior mean | Nemotron run | Change vs. mean |
|---|---|---:|---:|---:|
| Sample 01 | 226.1s, 162.2s, 226.0s | 204.8s | 172.8s | 15.6% reduction |
| Sample 02 | 207.1s, 261.0s, 220.4s | 229.5s | 228.8s | 0.3% reduction |
| Sample 03 | 146.0s, 145.7s, 145.2s | 145.7s | 70.6s | 51.5% reduction |

The duration-excess-only proxy also improved:

| File | Prior mean | Nemotron run | Change |
|---|---:|---:|---:|
| Sample 01 | 170.5s | 107.5s | 36.9% reduction |
| Sample 02 | 214.9s | 197.2s | 8.2% reduction |
| Sample 03 | 115.6s | 31.4s | 72.8% reduction |

## Interpretation

- Sample 03 improved substantially and Sample 01 improved moderately, consistent
  with the direction reported by the Riva team.
- Sample 02 did not show a meaningful listener-tail improvement in this run.
- The server itself finished generating the final audio within 0.8 seconds for
  all files. Most remaining listener tail is therefore queued playback caused
  by longer translated audio and intermittent delivery stalls, not a server
  that remains busy for minutes after input ends.
- Peak positive drift increased versus the prior-run means: Sample 01 33.9s to
  65.4s, Sample 02 14.4s to 31.3s, and Sample 03 29.7s to 39.0s. The 800 ms EOU
  configuration reduced accumulated tail on two files but did not eliminate
  transient stalls.

## Test-harness corrections

The original batch harness waited a fixed 30 seconds before signaling the end
of input, then canceled its receiver immediately after the signal. That could
not capture final S2S responses or measure true generation tail. The updated
harness now:

1. signals `end_input` as soon as source audio is exhausted;
2. keeps the output side open until translated audio becomes idle;
3. records first-audio latency and post-input responses;
4. simulates the browser playback queue from actual arrival timestamps; and
5. writes compact JSON summaries alongside event CSVs and plots.

## Caveats and next tests

- This is one Nemotron run compared with three older runs. Repeat runs are
  needed before treating the percentages as stable.
- Automatic punctuation is enabled. Explicit punctuation-boundary splitting
  and NMT/TTS queue parallelism are internal to the monolithic S2S endpoint;
  the client cannot confirm those stages independently. Testing a client-side
  ASR -> punctuation splitter -> queued NMT/TTS pipeline would isolate them.
- The output/input ratio includes silence in the source file. A speech-only TTS
  expansion ratio requires source VAD or aligned ASR segment durations.
- If Sample 02 remains near a 3–4 minute playback tail, evaluate 1.05x and 1.10x
  TTS playback/prosody speed and compare intelligibility.
