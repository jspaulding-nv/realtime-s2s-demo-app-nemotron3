# Magpie headless S2S control — July 27, 2026

## Outcome

**PASS — non-formal 60-second mechanical control.**

The active staged English-to-Spanish path completed the tracked one-minute
fixture through Nemotron 3 ASR, NMT, and Magpie TTS. The deterministic no-drop
listener schedule passed its registered 5/8/10-second policy:

- time-weighted listener-queue p95: 4.181 seconds;
- peak listener queue: 5.460 seconds;
- time above 10 seconds: 0.000 seconds; and
- dropped, reordered, or duplicated translated frames: zero.

This run is non-formal because the worktree also contained uncommitted,
default-off Chatterbox experiment changes. The live pipeline and artifact
validators passed, but the result must not be presented as clean-commit
promotion evidence.

## Runtime

| Component | Selected runtime |
| --- | --- |
| GPU | NVIDIA RTX PRO 6000 Blackwell Server Edition |
| ASR | Nemotron ASR Streaming `1.2.0` |
| ASR profile | English streaming, batch 32 |
| ASR endpointing | 800 ms EOU; word offsets enabled |
| Segmentation | punctuation-aware; 240 characters or 2,000 ms maximum age |
| NMT | Riva Translate 1.6B `1.5.2`, `en-US` to `es-US` |
| TTS | Magpie multilingual `1.7.0`, Spanish voice |
| TTS publication | schema-3 incremental, 500 ms frames |
| Stage queues | ASR events 32; NMT 4; TTS 4; output 4 |
| Listener policy | no drop; 5-second target, 8-second urgent, 10-second limit |
| Playback rates | 1.00x / 1.05x / 1.10x |
| Input pacing | 300 ms PCM chunks at real-time source-end boundaries |

The three pinned NIM containers were healthy before and after the run, with
zero restarts and no OOM termination. Only the script-owned FastAPI process was
started and then stopped gracefully. Post-run GPU use was 32,300 MiB, leaving
64,950 MiB free.

## Fresh control result

| Metric | July 27 control |
| --- | ---: |
| Source duration / chunks | 60.000 s / 200 |
| Complete translated parents | 23 |
| Translated PCM frames | 112 |
| Translated PCM duration | 50.760 s |
| Output/source duration ratio | 0.846x |
| First translated audio receipt | 5.204 s |
| Service tail after input | 1.296 s |
| Terminal arrival after input | 1.404 s |
| Adaptive listener tail | 6.756 s |
| Time-weighted queue p95 | 4.181 s |
| Peak queue | 5.460 s |
| Time above 10 seconds | 0.000 s |
| Accelerated source audio | 2.248% |
| Queue gate | PASS |

The pipeline reconciled every parent, frame, byte count, and terminal event.
There were no staged integrity errors and no NMT or TTS retries. TTS
incremental publication moved the first frame ahead of full request completion
by a median 0.219 seconds and p95 0.485 seconds.

## Source-freshness envelope

Protocol-v1 source attribution permits a conservative parent-envelope
projection:

| Source boundary to scheduled translated audio | p50 | p95 | maximum |
| --- | ---: | ---: | ---: |
| First frame of parent | 3.897 s | 6.043 s | 6.043 s |
| Final frame of parent | 6.704 s | 8.171 s | 8.308 s |

The median first-frame frontier grew by 0.581 seconds between the first and
last source quartiles. This is modest positive accumulation over one minute,
not an unbounded-growth finding. A long-form trace is still required to
determine whether the queue remains bounded over a complete talk.

The stage-level p95 values show where the fresh delay arose:

| Interval | p95 |
| --- | ---: |
| Source end to latest contributing ASR final | 1.481 s |
| ASR final to punctuation-segment emission | 2.061 s |
| NMT queue residence | 0.136 s |
| NMT processing | 0.557 s |
| TTS queue residence | 0.000 s |
| TTS start to first audio | 0.150 s |
| Output queue residence | 0.011 s |

This supports the current direction: Nemotron 3, 800 ms EOU, punctuation
splitting, and overlapping bounded NMT/TTS workers keep service-stage waiting
small enough for the one-minute control. Listener scheduling, phrase grouping,
and source finalization remain part of the end-to-end delay.

## Comparison with the retained control

The prior clean-commit preflight and this fresh run used the same tracked
fixture and model family:

| Metric | Prior control | July 27 control | Difference |
| --- | ---: | ---: | ---: |
| Queue p95 | 4.072 s | 4.181 s | +0.108 s |
| Peak queue | 5.211 s | 5.460 s | +0.249 s |
| Adaptive listener tail | 6.658 s | 6.756 s | +0.099 s |
| First-parent-frame p95 | 5.898 s | 6.043 s | +0.145 s |
| Final-parent-frame p95 | 7.663 s | 8.171 s | +0.508 s |
| Frontier growth | 1.074 s | 0.581 s | -0.493 s |

Both runs passed the queue gate, spent zero time over 10 seconds, and
preserved every translated frame. The differences are consistent with modest
run-to-run timing and synthesis variation; this single probe does not establish
a regression.

## Audience-experience claim boundary

This result does **not** prove that a translated punchline is audible within
6.043 or 8.171 seconds. Those values bound the translated audio for an
ASR-attributed parent, not the exact English semantic instant or corresponding
Spanish word. The headless harness intentionally retains no PCM and proves no
DAC, acoustic, or audience-reaction timing.

For a live audience, the practical objective remains a bounded listener queue
near 5 seconds with 10 seconds as a soft ceiling. That queue objective is
necessary but not sufficient: ASR finalization, punctuation grouping, NMT,
TTS startup, and the position of the translated punchline within its parent
also contribute before the listener hears it.

## Recommended next gate

Add a default-off, browser-independent private semantic capture:

1. Bind the exact sent source PCM by hash and sample count.
2. Retain translated PCM in validated protocol-frame order.
3. Retain a hash-bound per-frame ledger with parent/frame IDs, translated
   sample offsets, source range, arrival time, and deterministic scheduled
   start/end/rate.
4. Have at least two bilingual reviewers independently mark anonymous source
   and corresponding Spanish target sample indices.
5. Compute reviewed source-to-scheduled-target delay across short reactions
   and punchline-like landmarks near the beginning, middle, and end.

That method removes Chrome as a dependency and directly measures scheduled
digital semantic delay. It still must not be called physical audibility.
Literal audience-reaction synchronization requires a same-clock external
capture of the English reference, physical Spanish output, and optionally the
room reaction.

Only after that semantic gate should 1.05x/1.10x playback be promoted based on
timing plus native-listener intelligibility and naturalness review.

## Private ignored evidence

The raw artifacts remain mode `0600` under:

```text
experiment_results/magpie-headless-control-20260727/
```

Key bindings:

| Artifact | SHA-256 |
| --- | --- |
| Summary JSON | `51ed5275b227d9ecf93f629df6492575f2bf108ddb78d4525eb624d49ad559b3` |
| Timing CSV | `6a799197c4ccfec0e1dd2f9dffdff378f3da8dc22a1163176e3c74f5d8bdf860` |
| Playback analysis | `f09c5a8419e596ff275a6ca0122bf7b5bb8af457ffbadaccb45cf753043d7e25` |
| Streaming analysis | `fe9f3c5a1f5dce9c61de094fc075f0dfc99cea2372d9f2abd178bf9ee252f943` |
| Stage attribution | `3e5a7c3870b8c7b43422122f8aace122e517521c7e83e60591efd7029dc3aaef` |
