# Live tail-freshness shadow 60-second result — 2026-08-05

## Decision

Two automated live engineering probes passed, including a hardened
confirmation that hash-checked the fixture and read-only attested the live
container/image identity. The browser made every single-tail decision from
information available at frame arrival, left audible Spanish playback
unchanged, exported numeric-only evidence, and matched the independent Python
replay exactly in both runs.

This was a non-formal probe from a dirty working tree. It validates the live
mechanism and automation path, but it is not a release qualification result.
The 60-second input also never created enough translated playback backlog to
exercise truncation, so the next objective gate is a fresh five-minute live
shadow run.

## Exact runtime

- Nemotron streaming ASR 1.2.0;
- Riva Translate 1.5.2;
- Magpie multilingual TTS 1.7.0;
- staged schema-3 incremental publication in 500 ms frames;
- 800 ms ASR EOU with word offsets and automatic punctuation;
- existing punctuation splitting with minimum-context coalescing disabled;
- adaptive browser playback at 1.00x/1.05x/1.10x;
- a 10-second projected queue cap with a 100 ms cancellation guard; and
- rendered-digital PCM capture disabled.

The runner confirmed the three already-running pinned Riva containers were
healthy before starting its own backend, production frontend preview, and
headless browser. It gracefully stopped only those three owned application
processes after validation and left Riva running.

## Results

| Metric | Initial run | Hardened confirmation |
|---|---:|---:|
| Natural completion | yes | yes |
| Independent replay | exact match | exact match |
| Fixture hash check | tracked fixture | pass |
| Docker runtime identity | operator-verified | runner-attested |
| Frames / parents received | 114 / 23 | 113 / 23 |
| Generated translated audio | 50.667 s | 49.552 s |
| Peak queue before projected truncation | 5.567 s | 5.542 s |
| Peak queue after projected truncation | 5.567 s | 5.542 s |
| Truncation triggers | 0 | 0 |
| Frames dropped | 0 | 0 |
| Parents truncated / fully dropped | 0 / 0 | 0 / 0 |
| Audio retained | 100.00% | 100.00% |
| Residual cap breaches | 0 | 0 |
| Hard cap achieved | yes | yes |
| Single-tail invariant | pass | pass |

The empty loss sets are the correct result for this input: the largest live
projected queue remained 4.433 seconds below the 10-second limit. The small
frame-count and generated-duration difference is ordinary live synthesis
variation; it did not change the gate outcome. Together the runs show that the
default-off shadow can observe a complete live Riva run without changing audio
and that its evidence is independently reproducible. They do **not** yet prove
that the live implementation will perform the expected suffix truncations
under overload.

## Private evidence

The ignored, owner-private run directories are:

```text
experiment_results/live-tail-shadow-probe-20260805T023101Z/
experiment_results/live-tail-shadow-probe-20260805T023541Z/
```

It contains the browser timing CSV, numeric-only shadow JSON, aggregate
analysis JSON/Markdown, exact non-formal runtime record, and operational logs.
No PCM, transcript, or translation text is present in the shadow or analysis
artifacts.

## Reproduction

With the pinned Riva services already healthy and Node.js 22.12.0 or newer
available, run:

```bash
python3 run_live_tail_shadow_preflight.py \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

The runner fixes the staged probe controls, builds a production frontend,
loads the tracked 60-second fixture, enables adaptive playback and the shadow,
keeps rendered PCM capture off, waits for natural completion, downloads both
artifacts, and invokes `analyze_live_tail_shadow.py`.

## Completed follow-on

The five-minute live capacity gate passed and is documented in the
[five-minute result](LIVE_TAIL_FRESHNESS_SHADOW_5MIN_RESULT_2026-08-05.md).
Before running all three long-form samples, the next gate is a matched 100 ms
versus 500 ms publication comparison to minimize unnecessary suffix loss.

Even a five-minute mechanical pass will not prove that omitted speech is
semantically safe. Audible cancellation remains disabled pending a separate
canary and bilingual quality review.
