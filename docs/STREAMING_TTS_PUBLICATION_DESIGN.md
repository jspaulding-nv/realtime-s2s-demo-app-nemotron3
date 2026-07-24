# Default-off incremental TTS publication

## Objective

Forward Magpie PCM while one TTS RPC is still running instead of joining the
entire response stream into one WebSocket message. This is a burst-smoothing
experiment for the staged ASR -> NMT -> TTS path. It does not replace the
separate requirement to keep the listener playback queue bounded.

The experiment is disabled by default:

```dotenv
STAGED_TTS_INCREMENTAL_PUBLISH=0
STAGED_TTS_INCREMENTAL_FRAME_MS=100
STAGED_TTS_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS=4
```

When disabled, telemetry schemas 1 and 2, atomic TTS retry behavior, and the
existing WebSocket binary framing remain unchanged.

## Schema 3 identity and lifecycle

The first experiment keeps post-NMT request splitting disabled. Incremental
publication and `STAGED_TTS_SUBSEGMENT_MAX_CHARS>0` are deliberately mutually
exclusive, so every TTS request has one unambiguous parent identity.

Each published PCM frame has:

```text
(parent_sequence_id, audio_frame_id)
```

`audio_frame_id` starts at zero for each parent and is contiguous. Variable
Riva responses are reframed into 100 ms blocks aligned to mono Int16 sample
boundaries. The final frame may be shorter. Concatenating the published frames
must reproduce the exact successful RPC PCM byte stream.

The output queue carries three nonterminal event types:

1. legacy `AUDIO`, containing one complete atomic `SynthesizedSegment`;
2. schema-3 `AUDIO_FRAME`, containing one incremental PCM frame; and
3. schema-3 `PARENT_COMPLETE`, containing authoritative frame and byte totals.

`PARENT_COMPLETE` is internal and is never sent over the WebSocket. It is
enqueued only after clean TTS iterator exhaustion and after every preceding
frame has been accepted by the bounded output queue. It consumes an ordinary
output-queue slot. Only the final pipeline `COMPLETE` or `ERROR` event may use
the queue's reserved terminal slot.

The browser protocol remains ordered binary PCM followed by one JSON terminal.
Schema 3 changes binary message granularity, not the PCM encoding.

## Commit and retry boundary

A frame is committed when the thread-to-async bridge receives a positive
acknowledgment that the bounded output queue accepted it.

| Outcome | Retry |
|---|---:|
| First RPC ends with genuine gRPC `UNKNOWN`, zero committed frames | Once |
| Empty or sub-frame private carry followed by `UNKNOWN` | Once |
| Any frame has been committed | Never |
| Publisher abort or rejection | Never |
| PCM validation or size-limit failure | Never |
| Explicit cancellation or disconnect | Never |
| Failure on the second RPC attempt | Never |

After a committed prefix, the system preserves and sends that prefix once,
then emits one terminal error. It never retries or replays already committed
audio.

### Tiny-target atomic fallback

Magpie has an observed intermittent failure on a validated two-character
target. Atomic synthesis safely recovered that exact shape because failed
attempt PCM remained private, but true incremental publication had already
committed two frames when the same failure recurred in the first clean
schema-3 canary. Retrying at that point would risk replaying stochastic audio.

Schema 3 therefore uses a narrow, configurable reliability envelope. A target
of at most four characters after the existing normalization and validation
step is synthesized atomically. A genuine `UNKNOWN` can be retried once while
all PCM remains private. Only a complete successful attempt is then reframed
into the ordinary configured frame size and published through the same
acknowledged callback. The value four covers the proven two-character shape
and similarly tiny outputs such as short affirmations; the extra atomic hold is
limited to tiny utterances. Set the threshold to zero to disable this fallback.

Fallback frames remain ordinary schema-3 frames. Their parent summary carries
`atomic_fallback_applied=true`, and publication timestamps must not precede the
parent's TTS completion. Direct first-frame lead measurements exclude fallback
parents because they intentionally do not publish from an active RPC. Audience
queue and end-to-end measurements continue to include every parent.

## Bounded thread-to-async bridge

The Riva iterator remains synchronous in the dedicated TTS executor thread.
For each aligned frame, the thread submits one enqueue coroutine to the owning
event loop and waits for an explicit committed/aborted result. This propagates
the existing output-queue backpressure to the TTS iterator without blocking
the event loop.

Timeout, session close, WebSocket failure, and task cancellation abort both:

- the active gRPC call/channel; and
- any bridge operation waiting for output capacity.

The abortable enqueue chooses a deterministic result if capacity and abort
become ready together: abort wins until queue insertion has linearized the
commit. Any acquired-but-uncommitted semaphore slot is released. This prevents
the reserved terminal error from overtaking a frame and prevents PCM from
appearing after that terminal.

## Integrity requirements

A successful schema-3 capture must prove:

- contiguous parent IDs and per-parent frame IDs;
- identical frame order at production, output dequeue, WebSocket send, and
  client receive;
- per-frame send/receive byte equality;
- per-parent frame counts and byte sums equal `tts/completed`;
- fallback identity agrees at production, dequeue, WebSocket completion, and
  the session-level fallback parent list;
- fallback frames are published only after successful TTS completion;
- exactly one `PARENT_COMPLETE` after all frames for each parent;
- completed parent IDs equal WebSocket-completed parent IDs;
- no PCM after the single terminal event;
- queue depth never exceeds configured capacity; and
- retry telemetry is compatible with the first-commit rule.

## Promotion sequence

1. Unit-test adapter reframing, retry, partial-output failure, cancellation,
   and backpressure.
2. Unit-test pipeline and WebSocket FIFO/completion accounting, including a
   partial-prefix terminal failure.
3. Run the complete regression suite with the flag disabled.
4. Run one matched 60-second atomic-versus-streaming probe with response
   cadence telemetry enabled in both arms.
5. Promote to a matched five-minute canary only if per-arm byte integrity,
   lifecycle, service health, and direct publication metrics pass. Magpie
   output duration can vary across otherwise matched calls, so cross-arm queue
   and tail deltas are classified as confounded when the generated workloads
   differ materially.
6. Run the selected configuration over all three long-form fixtures before
   making a real-time audience-experience claim.
