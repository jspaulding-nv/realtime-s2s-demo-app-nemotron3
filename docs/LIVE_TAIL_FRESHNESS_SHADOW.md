# Observation-only live tail-freshness shadow

## Status

Implemented and unit-tested on 2026-08-05. Non-formal automated 60-second and
five-minute live engineering probes passed on the pinned Riva stack; see the
[60-second result](LIVE_TAIL_FRESHNESS_SHADOW_60S_RESULT_2026-08-05.md) and
[five-minute result](LIVE_TAIL_FRESHNESS_SHADOW_5MIN_RESULT_2026-08-05.md), and
[100 ms/500 ms frame comparison](LIVE_TAIL_FRESHNESS_SHADOW_FRAME_COMPARISON_2026-08-05.md).
The five-minute runs exercised live overload and suffix projection; 100 ms is
the preferred profile for the next observation gate. The feature remains
default-off and has not completed a clean-checkout qualification or the three
complete long-form samples.

The shadow projects what the 10-second single-tail policy would have done to
translated browser audio. It never calls `AudioBufferSourceNode.stop`, changes
the audible schedule, rewrites PCM, or suppresses WebSocket data. Actual
Spanish playback remains the existing no-drop adaptive 1.00x/1.05x/1.10x
control.

## Why this gate exists

The offline five-minute counterfactual enforced a 10-second scheduled-audio
queue while retaining 90.49% of generated translated audio. All nine affected
parents lost one trailing suffix rather than internal or fragmented holes.

That result used a completed saved trace. The live shadow is the next causal
check: can the same policy make each decision from information available at
that exact browser arrival, without knowledge of future speech and without
changing what a listener hears?

## Evidence contract

The dashboard consumes only protocol-v1 numeric metadata and scheduling
measurements:

- stream generation, parent ID, and frame ID;
- PCM byte count and duration, but not PCM samples;
- `AudioContext.currentTime` at scheduling;
- projected queue before and after truncation;
- adaptive playback mode/rate;
- per-event projected drop count and duration; and
- parent completion markers.

When a projected breach occurs, the shadow protects audio that has started or
is within the 100 ms cancellation guard. It keeps the largest possible prefix
of the oldest eligible parent, projects removal of one suffix, and suppresses
future frames from that parent through its completion marker. Later retained
frames are compacted without revising their causal rate decisions.

The exported artifact explicitly declares:

```text
observationOnly: true
liveAudioChanged: false
containsPcm: false
containsTranscriptOrTranslationText: false
```

It contains numeric parent/frame identities and timing evidence. Treat it as
private test evidence and keep it under an ignored `experiment_results/`
directory even though it contains no audio or text.

## Manual live preflight

Use the pinned staged schema-3 incremental pipeline with audio metadata
protocol v1. Confirm the Riva containers and backend are healthy, then start
the frontend and open:

```text
http://localhost:5173/#/test
```

For an internal VPN-accessible VM deployment, use the already configured
proxied Test Dashboard URL instead.

1. Select the tracked 60-second preflight WAV.
2. Leave **Adaptive Spanish playback** enabled.
3. Enable **Observation-only 10-second tail-freshness shadow**.
4. Leave the rendered-digital PCM capture disabled; it is a separate gate.
5. Start the test and let source input, server completion, and audible playback
   drain naturally. Do not use manual stop for formal evidence.
6. Confirm the shadow status is `complete`, the single-tail contract is
   `Pass`, and residual breaches are zero.
7. Select **Export CSV**. The same user action also exports a
   `tail-freshness-shadow-*.json` artifact. Allow multiple downloads if the
   browser asks.

The UI shows the last arrival-decision queue, projected peak, retention,
affected parents, dropped frames, longest suffix, residual breaches, and the
single-tail invariant while the unchanged audio continues to play.

## Automated 60-second engineering probe

The dedicated runner accepts a dirty checkout but marks the result non-formal.
It hash-checks the exact 60-second fixture, verifies Riva readiness and
read-only container/image identity without mutating the containers, builds the
current production frontend, starts and later cleans up its own
FastAPI/Vite/Chrome processes, drives the manual workflow above, and invokes
the independent validator automatically:

```bash
python3 run_live_tail_shadow_preflight.py \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

After the 60-second gate, reproduce the hash-bound five-minute source and run
the same workflow with:

```bash
python3 run_live_tail_shadow_preflight.py \
  --five-minute \
  --incremental-frame-ms 100 \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

After the 100 ms five-minute profile passes, run all three complete registered
fixtures sequentially with the checkpointed batch wrapper:

```bash
python3 run_live_tail_shadow_long_form.py \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

Resume an interrupted batch without overwriting completed evidence:

```bash
python3 run_live_tail_shadow_long_form.py \
  --resume-dir experiment_results/<batch-directory> \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

See the [complete three-sample runbook](LONG_FORM_TAIL_SHADOW_RUNBOOK.md) for
the fixed contract, per-sample checkpoint behavior, and claim boundaries.

Outputs go to a fresh ignored
`experiment_results/live-tail-shadow-probe-<UTC>/` directory. The runner fixes
the exact 800 ms EOU, schema-3 500 ms incremental publication, adaptive
playback, 10-second cap, and 100 ms guard controls. It forces rendered-digital
PCM capture off and never starts or stops Riva containers.

Use this runner for rapid engineering checks. A promotion or release claim
still requires a clean committed checkout and the applicable formal runtime
attestations.

## Independent validation

Move the exported JSON into a fresh ignored evidence directory and run:

```bash
python3 analyze_live_tail_shadow.py \
  --evidence-json \
    experiment_results/live-tail-shadow/tail-freshness-shadow-<timestamp>.json
```

The analyzer fails closed unless:

- the schema and privacy declarations are exact;
- parent/frame identities are contiguous and complete;
- frame and duration accounting reconcile;
- every loss is a full parent or one trailing suffix;
- per-arrival queue depths and projected losses match an independent replay
  through `playback_simulation.py`; and
- aggregate retention, cap, and loss metrics match that replay.

It writes owner-private aggregate `.analysis.json` and `.analysis.md` files
beside the evidence. Those reports omit the input path, filenames, endpoints,
session identity, PCM, text, and wall-clock timestamps.

## Promotion order

1. Pass one natural-completion 60-second preflight. **Engineering probe passed.**
2. Pass a five-minute real-time run and compare it with the retained offline
   five-minute result. **Engineering probe passed at the current 500 ms profile.**
3. Select 100 ms versus 500 ms publication with one matched live comparison.
   **100 ms selected for further observation.**
4. Run the live shadow across all three long-form samples and verify every
   artifact independently. **Passed mechanically at 100 ms; rejected for
   audible promotion because projected aggregate removal was 10.97%.**
5. Require all runs to hold the 10-second projected scheduled-audio cap, keep
   the single-tail invariant, and report the complete loss distribution.
6. Only then design a separate default-off audible canary with fade and
   boundary-restart behavior.

An audible canary would still require bilingual quality review. A mechanical
shadow pass cannot establish that omitted suffixes preserve meaning.

See the
[complete long-form result](LIVE_TAIL_FRESHNESS_SHADOW_LONG_FORM_RESULT_2026-08-05.md)
for the three per-sample queue peaks, retention values, loss distribution, and
the decision to keep audible cancellation disabled.

## Claim boundaries

A 10-second shadow pass applies only to already-delivered translated audio in
the browser scheduler. It does not include ASR endpointing, NMT, TTS startup,
network delivery, DAC output, room acoustics, or the exact English-event to
Spanish-event delay. It therefore cannot by itself prove that a translated
joke or punchline reaches the audience within ten seconds.

The shadow is an overload-capacity experiment, not a deployment policy. Until
an audible canary and bilingual review pass, live audio must remain unchanged.

## Development verification

The frontend requires Node.js 22.12.0 or newer within the supported engine
range. Run:

```bash
cd frontend
npm run lint
npm test
npm run build
```

The Python evidence verifier is covered by the root test suite.
