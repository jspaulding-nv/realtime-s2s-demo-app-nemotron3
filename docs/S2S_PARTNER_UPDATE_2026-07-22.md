# Speech-to-Speech (S2S) Experiment — Partner Update

**Status as of July 22, 2026**

The clearest conclusion is: Nemotron and the new staged design are promising, but the live-audience latency problem is not solved yet. The dominant remaining observable long-tail component is playback backlog, not a “server is still processing for three minutes after the sample” problem.

## Operational addendum: July 24, 2026

The July 22 snapshot below is preserved as the state known on that date. The
targeted post-recovery Sample 02 gate has since passed on commit `55b59bd`:

- 805/805 emitted, produced, sent, and received audio segments completed in
  order;
- all three guarded short-segment NMT recoveries passed target validation;
- no incomplete ID, stage failure, cleanup error, connection loss, or timeout;
- last audio arrived 0.308 seconds after input ended; and
- translated audio was 1.08831x the source duration, leaving a 239.156-second
  fixed 1.00x playback tail.

This closes the targeted recovery gate but reinforces the audience-delay
concern. It is a standalone canary, not a resumable matrix checkpoint. The
current sequence is to relaunch FastAPI after the VM restart, pass a fresh
60-second preflight, and run a clean three-sample matrix before browser,
marked-phrase, and native-listener evaluation.

The VM restart also confirmed the deployment boundary: all three
Compose-managed NIMs returned to healthy under `unless-stopped`, while the
separately launched FastAPI process required manual relaunch. See
[the Sample 02 recovery canary](STAGED_SAMPLE_02_RECOVERY_CANARY.md).

## What we have proven

- All three pinned models run concurrently on the tested 96 GB RTX PRO 6000. The observed snapshot used about 32.2 GB, leaving roughly 65 GB free.
- The pinned Nemotron 3 ASR, Riva NMT, and Magpie TTS stack completed all
  three samples through the monolithic control path. The direct staged path
  has now completed Sample 03; Sample 01 and Sample 02 staged runs remain.
- In the saved monolithic traces, last PCM arrived 0.06–0.91 seconds after input ended, while the polling-based harness observation ended after 1.00–2.00 seconds. In staged Sample 03, last PCM arrived after 0.850 seconds and the completed terminal after 1.744 seconds. The listener can still be tens of seconds or minutes behind because translated audio accumulates faster than it is played.
- The new direct staged ASR → NMT → TTS implementation preserves order, bounds its internal queues, overlaps NMT and TTS, drains deterministically, and reports detailed latency telemetry.

## Long-form audience results

The latest complete monolithic captures show:

| Sample | Output/input | Fixed playback tail | Adaptive tail | Adaptive queue p95 |
|---|---:|---:|---:|---:|
| Sample 01 | 1.039x | 142.433 s | 36.656 s | 35.823 s |
| Sample 02 | 1.080x | 204.144 s | 30.509 s | 37.460 s |
| Sample 03 | 0.994x | 41.806 s | 17.797 s | 18.622 s |

Adaptive 1.00x/1.05x/1.10x playback reduced aggregate tail by 78.1%, from 388.383 to 84.962 seconds, without dropping audio. But every sample still missed the 10-second queue objective. The queue remained above 10 seconds for 42–79% of playback.

This also shows that Spanish duration expansion is not the only cause: Sample 03 produced slightly less audio than its input yet still had a 42-second fixed tail. Bursty delivery, ASR segmentation, NMT/TTS timing, and playback scheduling also matter.

## What Nemotron appears to improve

Against @jgough-essextec's reconstructed earlier results, our initial Nemotron comparison showed:

- Sample 03: 51.5% listener-tail reduction
- Sample 01: 15.6% reduction
- Sample 02: essentially unchanged at 0.3%

That is directionally consistent with the Riva team’s findings—Sample 03 benefits most—but we do not have a same-day, identical Parakeet-versus-Nemotron A/B. We therefore cannot attribute every improvement solely to the ASR model.

Nemotron’s practical advantages are still meaningful:

- Better English streaming ASR foundation
- Automatic punctuation
- Tested 800 ms EOU configuration
- Better input for punctuation-aware segmentation
- Direct timestamps and final-result provenance
- Less reliance on the older CTC-specific endpointing behavior

## What the new staged pipeline tells us

The real-time 60-second staged preflight completed successfully:

- First translated audio: 5.109 seconds
- Tail after source input ended: 1.307 seconds
- NMT average: 319.84 ms per segment
- TTS first response audio: 145.98 ms average
- Full TTS segment: 493.69 ms average
- Maximum NMT/TTS/output queue depths: 2/2/2
- Blocked queue puts: zero
- Ordering errors or dropped segments: zero

So, over one minute, model processing kept pace with the speaker. Also, 10 of
the 23 segments used the two-second age fallback rather than punctuation, so
punctuation alone is not consistently determining segment boundaries.

The subsequent 31:28 Sample 03 staged canary also completed naturally:

- 646 ordered audio segments, IDs 0-645, with exact send/receive parity;
- no incomplete IDs, stage failure, cleanup error, connection loss, timeout,
  or container restart;
- first audio 5.113 seconds, last-audio arrival tail 0.850 seconds, and the
  completed terminal 1.744 seconds after input ended (the harness returned
  after 2.255 seconds including polling/settle time);
- NMT/TTS/output queue peaks 4/4/1 with 15/0/0 blocked puts; and
- six standalone hesitation fillers discarded before sequence allocation.

The canary also exposed the audience distinction. Spanish media was 1.01627x
the source (+30.721 seconds), while a fixed-rate replay of actual client
arrivals ended 64.038 seconds after English input. Adaptive 1.00x/1.05x/1.10x
playback reduced the simulated tail to 14.246 seconds, but the queue still
peaked at 28.052 seconds and spent 633.583 seconds above 10 seconds. Thus the
long-form server gate passed, but the proposed audience queue objective did
not.

## The joke/audience concern

Yes, the concern remains valid.

The per-segment NMT and TTS work is usually under one second, but a listener’s punchline delay is:

```text
ASR finalization and segmentation
+ NMT and TTS
+ all Spanish audio already waiting to play
```

The final term is the dangerous one. Based on the sample traces, accumulated playback queues can reach tens of seconds. A Spanish listener could therefore hear a joke well after the English-speaking audience laughs, especially later in the sample.

We have not yet measured an exact English punchline-to-Spanish-audible delay, so we should not claim a precise number. The existing queue statistics strongly suggest that it can be awkwardly long, however.

## Remaining unknowns as of July 22

- The staged route has not run Sample 01 and Sample 02 yet; Sample 03 passed.
- Actual browser playback queue depth under the staged route is unknown.
- Exact synchronized joke/marked-phrase delay is unknown.
- Spanish quality at sustained 1.05x or 1.10x playback has not been reviewed by native listeners.
- Short isolated inputs can produce wrong-script NMT output: `uh.` mapped to
  Chinese `呃。`, and the same class was reproduced for `Okay.` and `Amen.`.
  The staged route now suppresses known standalone fillers before sequence
  allocation, applies narrow Spanish OK/Okay/Amen overrides, validates target
  script after NMT and defensively before TTS, and fails closed rather than
  blindly retrying an unsafe payload.
- A 48 GB RTX 6000 should fit these profiles on paper, but only the 96 GB RTX PRO 6000 has been proven.

## Best next experiment as proposed July 22

The staged pipeline is now wired into `/ws/translate` behind a default-off
feature flag, and the first full-sample safety canary passed. Next:

1. Run Sample 01 and Sample 02, then repeat the complete three-sample staged matrix.
2. Measure actual browser queue seconds—not just model queue items or an
   arrival-trace replay.
3. Add synchronized phrase/punchline markers.
4. Target a playback queue around 5 seconds, with 10 seconds as the soft
   ceiling; define the catch-up/drop policy for cases where 1.10x cannot hold
   that bound.
5. Review sustained 1.05x/1.10x playback or TTS prosody changes with native
   Spanish listeners.

The detailed evidence is in [the staged pipeline report](STAGED_NMT_TTS_PIPELINE.md),
[the Sample 03 canary report](LONG_FORM_03_STAGED_CANARY.md), and
[the July 22 acceptance run](ACCEPTANCE_RUN_2026-07-22.md). The implementation
was subsequently committed and pushed on `agent/staged-nmt-tts-pipeline` at
`55b59bd`. Its historical
[draft PR #6](https://github.com/jspaulding-nv/realtime-s2s-demo-app-nemotron3/pull/6)
was superseded by the cumulative
[PR #10](https://github.com/jspaulding-nv/realtime-s2s-demo-app-nemotron3/pull/10).
