# Audience latency metrics

## The audience question

For live English-to-Spanish speech translation, the important experience is
not simply whether the server finishes soon after the microphone stops. It is
whether a Spanish listener hears the corresponding idea, punchline, warning,
or invitation soon enough to participate with the room.

If an English speaker delivers a joke and the rest of the room laughs, a
Spanish listener may still hear the punchline later because delay has accrued
in several places. Browser playback queue depth measures one of those places,
but it is not the whole end-to-end joke delay.

## Delay model

For a corresponding English and Spanish utterance, a useful conceptual model
is:

```text
audience delay
  = source segmentation / EOU delay
  + ASR finalization delay
  + punctuation-segment buffering
  + NMT queue and inference delay
  + TTS queue and time-to-first-audio
  + transport delay
  + browser wait-before-playback
  + output-device delay
```

The current browser implementation measures
`browser wait-before-playback` precisely for scheduled PCM. It does not yet
align source and target utterances, so it cannot independently report the
complete sum above.

For a marked utterance or punchline, define:

```text
utterance onset delay = Spanish audible onset - English utterance end
punchline delay       = Spanish punchline audible time - English punchline time
```

Use the same semantic landmark on both sides. A file-wide duration comparison
cannot substitute for that alignment.

## Metric definitions

### Browser queue depth

```text
Q(t) = max(0, next scheduled end time - AudioContext.currentTime)
```

`Q(t)` is the amount of translated audio already scheduled but not yet heard
in the browser. For a chunk being scheduled, `playback_wait_sec` is how long
that chunk waits behind prior audio. `queue_depth_sec` is the queue after the
new chunk is scheduled at its selected rate.

This is the most direct measure of accumulated listener backlog in the
browser. Report at least:

- p50, p95, and maximum queue depth;
- seconds and percent of observed time over 5 seconds;
- seconds and percent of observed time over 10 seconds;
- distinct entries into the over-limit state; and
- time spent at 1.00x, 1.05x, and 1.10x.

The current summary integrates each sample's state until the next sample. Keep
the sampling interval stable when comparing runs.

### First-audio latency

Time from the beginning of an input stream until the first translated audio
response arrives. This is useful for startup behavior but says nothing about
delay later in a 30- or 40-minute sample.

### Service flush tail

Time from the explicit end-of-input signal until the last translated audio
response arrives. A short flush tail means the service is not still generating
for minutes after input ends. It does **not** mean the listener has heard all
audio; minutes of translated audio may still be scheduled in the browser.

### Listener playback tail

Time from source input completion until the final translated audio would
finish playing through a gapless playback queue. It includes startup latency,
delivery stalls, and media-duration expansion.

Historical `playback_tail_sec` values in `docs/results/nemotron3/` were
reconstructed at fixed 1.00x. They are not results from the new adaptive
browser controller.

### Output/input duration ratio

```text
output-to-input ratio = synthesized PCM duration / source file duration
```

A ratio over 1.0 is queue-pressure evidence: the generated Spanish media is
longer than the complete English file. It is not a speech-only expansion ratio
because source silence remains in the denominator. Use VAD or aligned speech
segments for a speech-only comparison.

### Legacy duration drift

The dashboard historically compared source-file position with cumulative
Spanish media playback position. These positions are not semantically aligned
across languages, and Spanish media can be longer than English media. The
result can become negative even while the listener has a substantial tail.

Keep this value only for continuity with earlier tests. Label it **legacy
duration drift**, not audience delay.

### Stage residence and processing time

The planned staged backend should record, for each sequence ID:

- ASR final timestamp;
- punctuation segment emitted timestamp;
- NMT enqueue, start, and finish timestamps;
- TTS enqueue, first-audio, and finish timestamps;
- WebSocket send timestamp; and
- browser receive, schedule, and audible-start timestamps.

These measurements separate queue residence from inference time and make the
dominant source of delay actionable.

## Current known results

The pinned Nemotron runs from July 8, 2026 produced these fixed-rate listener
playback tails:

| Sample | Output/input duration | Fixed 1.00x tail | Simulated adaptive tail | Simulated queue p95 | Simulated peak | Simulated time over 10 s |
|---|---:|---:|---:|---:|---:|---:|
| Sample 01 | 1.056x | 172.770 s | 27.183 s | 38.31 s | 46.54 s | 80.0% |
| Sample 02 | 1.081x | 228.782 s | 40.206 s | 44.95 s | 53.11 s | 77.3% |
| Sample 03 | 1.017x | 70.598 s | 16.613 s | 19.48 s | 23.81 s | 45.6% |

Their service flush tails were 0.0, 0.4, and 0.8 seconds respectively. This
contrast is important: the service stopped emitting quickly, but the listener
could still have 1-4 minutes of media left to hear at 1.00x.

The adaptive columns are a deterministic replay of the saved client arrival
timestamps and PCM byte counts. They preserve every audio chunk and reproduce
the recorded fixed-rate tails within 0.005 seconds. They are not a new live
Riva run or a browser/audio-quality test.

Across the three replays, tail fell from 472.151 to 84.001 seconds, an 82.2%
reduction. That encouraging tail result must not hide the queue result: p95
remained 19-45 seconds, peaks remained 24-53 seconds, and the queue exceeded
10 seconds for 46-80% of playback time. A 1.10x maximum rate did not meet the
soft queue ceiling on any saved trace. Staged upstream work therefore remains
necessary even if live adaptive playback matches the simulation.

See
[`results/nemotron3/playback_policy_analysis.md`](results/nemotron3/playback_policy_analysis.md)
for the generated replay report.

## Measuring a joke or other marked phrase

The most defensible test uses synchronized source and output recordings:

1. Prepare several English phrases with unambiguous semantic landmarks,
   including a punchline. Record their text and source timestamps.
2. Feed the source through the real-time path at real-time pace. Do not use a
   faster-than-real-time file upload.
3. Record the English program channel and the browser's Spanish output on
   separate channels using the same recorder or clock.
4. Mark the English punchline time and the audible Spanish translation of that
   same punchline. Prefer two independent reviewers for ambiguous wording.
5. Report median, p95, and maximum semantic delay across markers, alongside
   browser queue depth at each marker.
6. Repeat under the fixed 1.00x control and adaptive policy with the same model
   versions, hardware, audio, and network.

If a real audience is present, separately record the room's English-audience
reaction and ask Spanish listeners whether they heard the punchline before,
during, or after that reaction. Human reaction timing is a useful experience
measure, but it should supplement—not replace—the synchronized audio metric.

Use headphones or isolated routing so translated output is not recaptured by
the source microphone.

## Interpreting the 5-10 second objective

The current operating goal is a browser queue near or below 5 seconds, with 10
seconds treated as a soft/SLA ceiling. It is not a promise that every Spanish
utterance will be audible within 10 seconds of English input. Upstream delay
can already exceed that value before a PCM chunk reaches the browser.

A successful experiment should therefore satisfy both classes of criteria:

- **Queue criteria:** p95 queue at or below the agreed threshold, little or no
  time over 10 seconds, and no sustained growth during long-form speech.
- **Semantic criteria:** marked-utterance and punchline delays acceptable to
  the evaluation audience, including transient delays after pauses or delivery
  stalls.
- **Quality criteria:** native listeners consider 1.05x/1.10x speech
  intelligible and natural enough for a sample-length session.

If the browser queue remains low while semantic delay is high, optimize
endpointing, stage queues, NMT, and TTS. If upstream timing is low while the
browser queue grows, reduce output expansion or use pitch-preserving catch-up.
