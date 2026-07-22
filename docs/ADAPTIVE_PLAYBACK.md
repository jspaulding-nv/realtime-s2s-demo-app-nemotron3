# Adaptive browser playback

## Status and purpose

The browser now has an experimental adaptive playback policy for translated
Spanish audio. Its purpose is to keep listener backlog in a bounded operating
range when synthesized Spanish is longer than the English source or when Riva
delivers audio in bursts.

This is a listener-side mitigation. It does not make ASR, translation, or TTS
finish earlier, and it does not by itself measure the delay between an English
utterance and its corresponding Spanish utterance. See
[Audience latency metrics](AUDIENCE_LATENCY_METRICS.md) for that distinction.

No live sermon run using this adaptive policy had been completed when this
document was written. The saved July 8, 2026 arrival traces were replayed
through a deterministic offline implementation of the same policy. That is
useful predictive evidence, but it is not a new Riva run, Web Audio run, or
listening-quality evaluation.

The replay produced:

| Saved trace | Fixed tail | Simulated adaptive tail | Adaptive queue p95 | Adaptive peak | Playback time over 10 s |
|---|---:|---:|---:|---:|---:|
| Spirit | 172.638 s | 27.051 s | 38.08 s | 46.54 s | 80.0% |
| Blessed | 228.772 s | 40.195 s | 44.71 s | 53.11 s | 77.3% |
| Beholding | 70.394 s | 16.408 s | 19.10 s | 23.81 s | 45.6% |

Aggregate listener tail fell 82.3% in the replay. However, every trace still
missed the 10-second queue SLA by a wide margin and spent 83-93% of translated
media at an accelerated rate. The replay therefore supports testing adaptive
playback, but it does not support calling the queue bounded at 10 seconds. It
also reinforces the need for staged upstream work and a long-form quality
evaluation.

The generated report and machine-readable details are in
[`results/nemotron3/playback_policy_analysis.md`](results/nemotron3/playback_policy_analysis.md)
and `results/nemotron3/playback_policy_analysis.json`.

The regenerated report uses the exact end of the final PCM source chunk when
an older trace lacks an explicit `input_ended` event. The original July 8
compact summaries used the start of that chunk; the analyzer retains those
numbers only as explicitly labeled compatibility evidence.

## Policy

The default policy is defined in
`frontend/src/utils/playbackPolicy.ts`:

| State | Entry condition using projected queue | Playback rate |
|---|---:|---:|
| Normal | Below 5 seconds | 1.00x |
| Catch-up | At least 5 seconds | 1.05x |
| Urgent | At least 8 seconds | 1.10x |
| Over-limit | More than 10 seconds | 1.10x plus an SLA breach |

The 10-second value is a **soft/SLA ceiling**, not a destructive buffer cap.
The implementation preserves every translated speech sample and never drops
audio merely to reduce delay. If translated media arrives faster than it can
be played even at 1.10x, the queue can still exceed 10 seconds and continue to
grow. That condition is measured and shown to the operator.

The controller uses hysteresis so it does not alternate rates at every chunk:

- Catch-up does not return to normal until the projected queue falls below
  4 seconds.
- Urgent does not leave the urgent band until the projected queue falls below
  7 seconds. It then enters catch-up while the queue remains at least 4
  seconds.
- A queue over 10 seconds remains at 1.10x. Crossing into the over-limit state
  increments a breach counter; repeated samples within the same excursion do
  not each count as a new breach.

The rate for a new chunk is chosen from the queue depth projected at normal
speed before that chunk is scheduled. The scheduled duration is then:

```text
scheduled duration = source audio duration / selected playback rate
```

The post-scheduling browser queue is:

```text
queue depth = max(0, scheduled end time - AudioContext.currentTime)
```

This queue value is exact for audio already scheduled in that browser
`AudioContext`. It is not an estimate based on input and output language
durations.

## Runtime behavior

`frontend/src/hooks/useAudioPlayback.ts` owns the gapless Web Audio schedule.
For each Int16 PCM response it:

1. converts the samples to an `AudioBuffer`;
2. calculates the waiting time behind audio already scheduled;
3. chooses the policy state and rate;
4. sets `AudioBufferSourceNode.playbackRate`;
5. schedules the complete buffer without dropping it; and
6. emits a playback scheduling event and updates queue metrics.

Adaptive playback is opt-in at the hook boundary and defaults to disabled.
It is enabled for translated output in both of these current UI paths:

- the main live translation panel; and
- the Spanish output side of the file test dashboard.

English input monitoring in the test dashboard remains fixed at 1.00x so it
does not alter the source reference.

The main panel displays current Spanish queue depth and playback rate. It uses
an amber state over 5 seconds and a red state over 10 seconds, with an explicit
message that speech is still being preserved.

The test dashboard samples the live browser queue while input is running and
while translated audio is draining. Natural file completion sends
`end_input`, leaves the WebSocket open, and waits for both:

- at least 5 seconds of network silence; and
- a browser playback queue of at most 0.1 seconds.

The dashboard also enforces a minimum 10-second drain observation and a
300-second maximum drain timeout. This avoids declaring a test complete merely
because the service stopped sending while Spanish audio remained queued for
the listener.

## Telemetry

The playback hook exposes:

- current and peak queue depth;
- current playback rate and policy state;
- total source-media duration received;
- total scheduled wall-clock duration after rate adjustment;
- whether the queue is above 5 or 10 seconds; and
- the number of distinct over-limit excursions.

The dashboard additionally computes p50 and p95 queue depth, time above 5
seconds, and time above 10 seconds from one-second queue samples.

CSV exports include the following optional client-event columns:

```text
media_duration_sec
scheduled_duration_sec
playback_wait_sec
queue_depth_sec
playback_rate
playback_mode
adaptive_playback_enabled
```

Three client stages carry the new data:

- `playback_session_started` records whether the run selected adaptive or
  fixed 1.00x playback.
- `playback_chunk_scheduled` records the decision for an individual received
  audio chunk.
- `playback_queue_sample` records the queue observed by the dashboard.

Do not combine browser `performance.now()` values with backend wall-clock
timestamps as though they shared a clock. Stage-level correlation needs a
session clock or explicit clock synchronization.

## Audio-quality caveat

`AudioBufferSourceNode.playbackRate` changes both tempo and pitch. At 1.05x and
1.10x, voices may sound higher, less natural, or more fatiguing. Independent
audio buffers can also expose discontinuities at chunk boundaries.

This implementation is therefore an experiment, not the final
pitch-preserving solution. Evaluate intelligibility, naturalness, speaker
identity, and listening fatigue with native Spanish listeners. If quality is
not acceptable, prefer one of these follow-ups:

- request faster prosody from TTS if a supported server-side control becomes
  available;
- use a pitch-preserving time-scale algorithm such as WSOLA in an
  `AudioWorklet`; or
- reduce upstream expansion through segmentation and TTS configuration.

Direct Magpie calls in the currently selected Riva client do not expose a
known, validated prosody-rate control for this test, so do not document one as
available until it is confirmed against the pinned client and NIM versions.

## Reproducing the frontend validation

Use Node.js 22 LTS (or a Node version supported by the pinned Vite toolchain),
then run:

```bash
cd frontend
npm ci
npm test
npm run build
npm run lint
```

At the documentation snapshot, 81 frontend tests passed, the production build
passed, and frontend lint passed with Node.js 22.

The focused policy and scheduling tests are in:

```text
frontend/src/__tests__/playbackPolicy.test.ts
frontend/src/__tests__/useAudioPlayback.test.ts
```

## Safe rollback

Adaptive behavior can be disabled per consumer by passing
`adaptivePlayback: false` to `useAudioPlayback`. The hook then schedules every
chunk at 1.00x while retaining the gapless queue. The file test dashboard
exposes this choice as a pre-run checkbox and records it in the exported CSV,
so fixed and adaptive conditions can use the same commit.

## Known limitations

- A 10-second hard cap is mathematically impossible if the sustained media
  production rate exceeds the maximum 1.10x consumption rate and speech may
  not be dropped.
- Playback acceleration affects only audio after Riva has produced and sent
  it. It cannot recover ASR endpointing, NMT, TTS, or network latency already
  incurred.
- The current controller chooses one rate per response buffer rather than
  continuously ramping the rate.
- Queue metrics describe one browser session and one `AudioContext`; they do
  not include device, Bluetooth, or room-acoustic latency.
- A low browser queue can coexist with a long joke delay when upstream stages
  take a long time to emit the corresponding Spanish audio.
