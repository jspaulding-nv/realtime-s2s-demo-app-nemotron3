# TTS publisher-handoff live result

## Outcome

The default-off publisher-handoff diagnostic passed its one-minute integrity
preflight and its promoted five-minute Sample 02 run on clean commit
`5646096`.

The new evidence does **not** support the TTS worker, event-loop bridge, bounded
output queue, relay, or WebSocket writer as the material source of listener
backlog in either run. The five-minute run nevertheless failed the no-drop
audience queue objective. Translated-media bursts remained the immediate
queue-growth mechanism.

This is a timing and transport result. It does not establish translation
quality, prosody quality, physical audibility, or exact punchline alignment.

## Fixed profile

Both runs used:

- staged Nemotron 3 ASR, Riva NMT, and Magpie multilingual TTS;
- the pinned container releases and independently recorded image digests;
- 800 ms ASR endpointing with punctuation splitting;
- unsplit one-request-per-parent NMT-to-TTS flow;
- schema-3 incremental TTS publication;
- 500 ms publication frames;
- response-chunk and publisher-handoff telemetry;
- protocol-v1 parent/frame metadata;
- real-time source pacing;
- the deterministic no-drop 5/8/10-second listener policy; and
- one clean implementation commit.

Sample 01 supplied the 60-second preflight. Sample 02 supplied the promoted
300-second diagnostic because the retained full-sample baseline had shown its
largest count of rare frame-ready-to-enqueue gaps.

| Service | Release | Captured image digest |
|---|---:|---|
| Nemotron ASR streaming | `1.2.0` | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| Riva Translate | `1.5.2` | `sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb` |
| Magpie multilingual TTS | `1.7.0` | `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d` |

## Integrity

| Gate | 60-second Sample 01 | 300-second Sample 02 |
|---|---:|---:|
| Staged state/outcome | closed / complete | closed / complete |
| Frames produced/sent/reconciled | 116 | 650 |
| Direct incremental frames | 114 | 650 |
| Atomic-fallback frames excluded from direct timing | 2 | 0 |
| NMT blocked admissions | 0 | 0 |
| TTS blocked admissions | 0 | 0 |
| Output blocked admissions | 0 | 0 |
| Dropped/reordered/duplicated frames | 0 / 0 / 0 | 0 / 0 / 0 |
| Cleanup, connection, server, or drain failure | none | none |

Every published frame identity, PCM byte count, retry count, dequeue, WebSocket
send, and client receipt reconciled. The aggregate analyzers reported no raw
text, audio, paths, endpoints, session identifiers, or raw event arrays.

## Publisher handoff

All values below are milliseconds. Direct timing excludes deliberate
atomic-fallback frames.

| Interval | 60 s p50 | 60 s p95 | 60 s max | 300 s p50 | 300 s p95 | 300 s max |
|---|---:|---:|---:|---:|---:|---:|
| Frame ready → publish request, serial total | 0.145 | 2.617 | 3.309 | 0.140 | 2.774 | 3.963 |
| Prior-frame serialization wait | 0.000 | 0.000 | 1.610 | 0.000 | 0.000 | 1.503 |
| Ready/prior commit → publish request | 0.145 | 2.616 | 3.309 | 0.140 | 2.741 | 3.877 |
| Publish request → event-loop callback | 0.628 | 0.843 | 2.130 | 0.604 | 0.816 | 6.577 |
| Callback → output-capacity resumption | 0.210 | 0.424 | 1.242 | 0.217 | 0.348 | 0.811 |
| Capacity → queue commit | 0.247 | 0.357 | 0.871 | 0.243 | 0.336 | 0.580 |
| Frame ready → queue commit | 1.282 | 3.763 | 4.487 | 1.275 | 3.860 | 8.690 |
| Enqueue → dequeue | 6.484 | 10.351 | 10.608 | 5.728 | 10.415 | 11.515 |
| Dequeue → WebSocket send start | 0.694 | 0.834 | 1.230 | 0.679 | 0.872 | 1.385 |
| WebSocket send start → completion | 3.250 | 3.727 | 9.736 | 3.263 | 3.597 | 4.518 |
| Frame ready → send completion | 11.486 | 16.091 | 21.446 | 11.092 | 15.845 | 19.697 |

Neither run had a frame-ready-to-enqueue interval over 100 ms or one second.
The retained full-sample baseline had observed 2 / 9 / 2 frames over 100 ms
and 1 / 7 / 0 over one second across Samples 01 / 02 / 03, with maxima of
5.648 / 7.054 / 0.930 seconds. The shorter live result therefore shows that
the historical anomaly was absent in these windows; it does not prove that the
anomaly cannot recur later in a complete sermon.

## Service and media timing

| Metric | 60-second Sample 01 | 300-second Sample 02 |
|---|---:|---:|
| First translated audio | 15.520 s | 4.676 s |
| Service tail after source end | 1.318 s | 3.569 s |
| Translated PCM | 53.128 s | 303.348 s |
| Translated/source duration ratio | 0.885x | 1.011x |
| Source boundary → first client frame p95 | 3.915 s | 4.400 s |
| Source boundary → scheduled start p95 | 11.817 s | 13.324 s |
| Source boundary → scheduled start maximum | 11.817 s | 24.994 s |

The Sample 02 TTS response stream was active rather than withheld: every one
of 84 requests returned multiple responses, with a 20 ms median inter-response
arrival, 142 ms median TTS-start-to-first-audio interval, and 1.254-second p95
from first response to RPC completion. Incremental publication placed the
first frame on the WebSocket a median 377 ms before full TTS completion.

## Audience queue result

| No-drop scheduled-listener metric | 60-second Sample 01 | 300-second Sample 02 |
|---|---:|---:|
| Time-weighted queue p95 | 11.152 s | 12.104 s |
| Peak queue | 13.307 s | 21.991 s |
| Time above 10 seconds | 6.453 s | 46.361 s |
| Modeled listener tail | 5.700 s | 25.561 s |
| 5-second-p95 / 10-second-peak gate | fail / fail | fail / fail |

The five-minute run preserved every frame while failing the bounded-audience
objective. Its strongest aligned 30-second window delivered 41.657 seconds of
translated media and 39.149 seconds of scheduled workload. That window grew
the queue by 10.092 seconds and reached a 13.814-second peak even though no
stage reported blocked admission.

Across the 84 completed parents:

- parent audio duration versus immediate queue change had a descriptive
  Spearman coefficient of 0.995;
- source-boundary-to-first-frame timing versus immediate queue change was
  0.399.

Across the 11 aligned 30-second windows, scheduled workload versus queue change
had a descriptive Spearman coefficient of 0.764.

These are descriptive associations, not causal estimates, but they agree with
the earlier full-sample attribution: generated-media volume arriving in bursts
tracks immediate queue growth much more closely than publisher-handoff delay.

## Decision

Do not optimize the publisher handoff on the basis of these runs. Its complete
frame-ready-to-WebSocket path remained in the low tens of milliseconds and
showed no long tail. Pacing the same PCM later would only move waiting upstream
and would not reduce the listener's semantic delay.

The next candidate must reduce or absorb translated workload while preserving
content:

1. retain a visible 5–10 second audience queue objective and the no-drop
   integrity gate;
2. test supported TTS speaking-rate or pitch-preserving 1.05x / 1.10x
   time-scale controls with native-language quality review;
3. measure leading/trailing synthesized silence before altering text
   segmentation;
4. compare generated duration and 30-second workload bursts, not only whole-run
   duration ratio; and
5. add consent-cleared source/target semantic landmarks before making a
   punchline-delay claim.

A hard 10-second cap is not achievable without dropping content, accelerating
faster than incoming workload, or applying backpressure to the live speaker
when a 30-second interval creates more than 39 seconds of scheduled work.

## Claim boundary

It is reasonable to claim that the pinned five-minute staged path completed,
preserved every observed PCM frame, and did not reproduce the historical
publisher-handoff anomaly. It is also reasonable to claim that its modeled
no-drop listener queue exceeded the 5/10-second objective.

It is not reasonable to claim that the complete long-form anomaly is fixed,
that a listener heard audio at the scheduled software deadline, or that an
English and translated-language punchline were semantically synchronized.
