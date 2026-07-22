# Bounded staged NMT and TTS pipeline

## Status

The second staged-pipeline milestone is implemented on
`agent/staged-nmt-tts-pipeline`. It joins direct Nemotron ASR, punctuation
segmentation, direct Riva NMT, and direct Magpie TTS through bounded FIFO
queues. A real-time one-minute English-to-Spanish preflight completed on July
22, 2026.

The browser `/ws/translate` route is deliberately unchanged and still uses
Riva's monolithic S2S operation. The staged path is currently exercised by the
standalone smoke tool and unit-test fakes. This keeps the earlier three-sample
baseline comparable while the new lifecycle and telemetry are validated.

## What is implemented

| Component | Contract |
|---|---|
| `backend/direct_nmt_client.py` | One source segment per RPC, explicit deadline, exact-one/nonempty output, exact target-language validation, no retries |
| `backend/direct_tts_client.py` | Spanish voice lookup, 16 kHz mono Int16 PCM, full-segment atomic buffering, first-audio/completion timing, active-call cancellation, no retries |
| `backend/staged_pipeline.py` | One ASR consumer, one NMT worker, one TTS worker, bounded queues, ordered drain, first-failure ownership, per-stage telemetry |
| `staged_pipeline_smoke.py` | Real-time WAV feed, direct three-service run, raw PCM output, JSON report, and an overall terminal deadline |

NMT and TTS each use one worker. This preserves source order without a reorder
buffer while still overlapping stages: NMT can translate segment `n + 1`
while TTS synthesizes segment `n`.

TTS output is atomic per segment. Although Magpie streams response chunks, the
adapter publishes only after the RPC has completed and every nonempty chunk
has been validated. A failed request therefore cannot leak partial audio and
then duplicate it during a later attempt. The current milestone performs zero
retries.

## Pinned services

| Stage | Image | Host gRPC endpoint |
|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `localhost:50052` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `localhost:50051` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `localhost:50053` |

The tested client is `nvidia-riva-client==2.24.0`. NMT uses
`megatronnmt_any_any_1b`, and Spanish TTS uses
`Magpie-Multilingual.ES-US.Isabela`.

An important live finding is encoded as an adapter guard: the pinned NMT can
return fluent-looking output for an empty or whitespace-only request. Blank
segments are rejected before the RPC, and NMT output must contain exactly one
nonempty `es-US` translation before it can reach TTS.

## Data flow and bounds

```text
English PCM
    -> Nemotron streaming ASR
    -> bounded ASR event bridge (32 events)
    -> punctuation/age/length segmenter
    -> bounded NMT queue (4 segments)
    -> one NMT worker
    -> bounded TTS queue (4 translations)
    -> one TTS worker
    -> bounded output queue (4 atomic audio segments)
    -> smoke consumer now; WebSocket sender in the next milestone
```

All puts await capacity. No text or audio is discarded. The queue bounds are
memory and overload controls, not proof that a live speaker can be paused or
that audience delay will remain below a fixed number of seconds.

The output queue reserves one additional control slot so `COMPLETE` or `ERROR`
cannot deadlock behind its configured audio-segment capacity. Each TTS response
chunk is capped at 256 KiB and one synthesized segment is capped at 60 seconds
of configured PCM by default. The queue still counts atomic segments rather
than playback seconds; browser audio-time bounds belong to the next milestone.

Natural completion is an exact ordered drain:

1. Stop accepting source audio and finish ASR input.
2. Consume remaining ASR finals.
3. Flush the segmenter's residual once.
4. Place a typed internal drain marker after the final NMT segment.
5. NMT processes every earlier item, then forwards the marker to TTS.
6. TTS processes every earlier translation, then publishes one `COMPLETE`.
7. The consumer drains all preceding audio before observing `COMPLETE`.

The first model-stage error owns the session failure, cancels sibling tasks,
and produces one terminal `ERROR` instead of `COMPLETE`. Direct NMT has a real
unary RPC deadline. Riva client 2.24 does not expose a deadline on the public
streaming-TTS helper, so the orchestrator enforces its deadline by cancelling
the retained call and closing the TTS channel.

## Configuration

Defaults are in `.env.example`:

```dotenv
S2S_PIPELINE_MODE=monolithic
STAGED_SEGMENT_MAX_CHARS=240
STAGED_SEGMENT_MAX_AGE_MS=2000
STAGED_ASR_EVENT_QUEUE_MAXSIZE=32
STAGED_NMT_QUEUE_MAXSIZE=4
STAGED_TTS_QUEUE_MAXSIZE=4
STAGED_OUTPUT_QUEUE_MAXSIZE=4
STAGED_NMT_RPC_TIMEOUT_SECONDS=15
STAGED_TTS_RPC_TIMEOUT_SECONDS=60
STAGED_TTS_MAX_SEGMENT_AUDIO_SECONDS=60
STAGED_CLOSE_TIMEOUT_SECONDS=10
```

`S2S_PIPELINE_MODE` is configuration groundwork only in this milestone. The
browser route does not consume it yet, and remains monolithic regardless of
that value. `staged_pipeline_smoke.py` explicitly constructs the staged path.

## Reproduce the live preflight

Docker Compose reads `.env` itself, but shell commands such as `docker login`
need the variables exported into the shell:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin

docker compose up -d
docker compose ps
```

Once the three containers are healthy, the smoke itself does not need the NGC
key; it talks to the local gRPC ports:

```bash
PYTHONPATH=.python-packages python3 staged_pipeline_smoke.py \
  --file test_audio/test-1min.wav \
  --duration-seconds 60 \
  --pcm-output test_results_staged/staged-smoke-es-US.pcm \
  --json-output test_results_staged/staged-smoke-report.json
```

Generated PCM/JSON artifacts under `test_results_staged/` are ignored. Listen
to the raw output without adding a WAV header:

```bash
ffplay -f s16le -ar 16000 -ac 1 \
  test_results_staged/staged-smoke-es-US.pcm
```

Use `--fast` only for throughput testing. Audience-latency observations need
real-time source pacing.

## July 22, 2026 live result

The pinned containers were healthy on the 96 GB RTX PRO 6000 Blackwell Server
Edition. The preflight used 60 seconds from `test_audio/test-1min.wav`, real-
time pacing, English input, Spanish output, automatic punctuation, and 800 ms
EOU.

| Metric | Observed |
|---|---:|
| Input sent | 60.000 s |
| Wall time through terminal drain | 61.024 s |
| First translated audio available | 5.109 s after run start |
| Tail drain after source input ended | 1.307 s |
| Atomic audio segments | 23 |
| Segment reasons | 13 punctuation, 10 age |
| Output PCM | 1,612,414 bytes / 50.388 s |
| Output/input whole-prefix duration ratio | 0.840 |
| Maximum NMT / TTS / output queue depth | 2 / 2 / 2 |
| Blocked queue puts | 0 |
| NMT processing | 126.99–596.25 ms; 319.84 ms average |
| TTS first response audio | 120.85–170.82 ms; 145.98 ms average |
| TTS full-segment completion | 247.08–1,092.73 ms; 493.69 ms average |
| NMT queue residence | 13.17 ms average; 169.26 ms maximum |
| TTS queue residence | 5.22 ms average; 116.26 ms maximum |

This sample stayed caught up: it finished only 1.307 seconds after the input
window, no bounded queue saturated, and NMT/TTS work overlapped without
reordering. It establishes that the direct APIs and drain protocol work on the
deployed profiles. It does not yet establish sample-length stability.

The 0.840 duration ratio includes silence in the whole source WAV prefix. It is
not an aligned utterance-only TTS expansion measurement and should not be
compared directly with the Riva team's 6–10% Spanish expansion finding.

## Audience-delay interpretation

First output in this sample arrived 5.109 seconds after the file began, but
that is not the same as punchline delay. A listener hears a particular joke
only after:

```text
source phrase timing
+ ASR finalization / segmentation
+ NMT and TTS processing
+ already queued Spanish playback
```

The staged report measures the server-side components and atomic-audio queue.
It does not yet measure browser playback backlog or align an English punchline
with its Spanish rendition. The audience objective therefore remains a
bounded playback queue of roughly 5–10 seconds, with 10 seconds treated as a
soft ceiling rather than a guaranteed cap. A growing Spanish playback queue
could still make laughter feel late as a sample continues even when the
server's end-of-input tail is small.

## Remaining work

1. Add a staged WebSocket session and one ordered sender behind an explicit,
   default-off feature flag.
2. Preserve the existing `start_stream`, `end_input`, `stop_stream`, binary
   PCM, status, and terminal semantics.
3. Feed staged PCM into the browser's existing 1.00x/1.05x/1.10x controller
   and record actual Web Audio queue seconds.
4. Extend the batch runner to select the staged route and rerun Sample 01,
   Sample 02, and Sample 03 end to end.
5. Add synchronized marked-phrase/joke measurements; service tail alone does
   not answer the audience-experience question.
6. If the browser queue still grows, evaluate 1.05–1.10x playback/prosody with
   native Spanish listeners and document quality tradeoffs.

The direct ASR input iterator itself is still unbounded. Bounds begin at the
ASR event bridge. For live microphone use, sustained upstream overload needs
an explicit degraded-mode policy rather than pretending application
backpressure can pause a speaker.

Direct adapters supplied to a session are exclusive for that session while it
is active. `owns_clients=False` suppresses automatic connect and normal idle
disconnect, but an RPC timeout or unfinished-session cancellation must still
abort the model call/channel; reconnect those supplied clients before reuse.
