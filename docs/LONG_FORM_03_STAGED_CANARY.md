# Sample 03 staged full-sample canary

## Scope

This is the first full-length promotion gate for the feature-flagged direct
`Nemotron ASR -> punctuation segmenter -> NMT -> Magpie TTS` path. The source
is the 1,888.1045-second *Long-form sample 03* file. Tests use real-time
input pacing, `S2S_PIPELINE_MODE=staged`, 800 ms ASR EOU, and the pinned
containers:

- `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0`
- `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2`
- `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0`

The VM has an NVIDIA RTX PRO 6000 Blackwell Server Edition with 97,887 MiB
VRAM. Attempts 1 and 2 found application/content defects; neither failure was
a GPU OOM, container restart, or queue-capacity failure. Attempt 3 passed the
full operational gate.

## Attempt 1: ASR observation-clock race

The first attempt's pipeline failed about 69.1 seconds after session start.
The batch client did not yet stop on a terminal error, so it was manually
terminated after approximately 162 seconds of source input.

The staged export reported:

- outcome/state: `failed` / `closed`;
- failure: ASR `monotonic time cannot move backwards`;
- 22 segments emitted, synthesized, dequeued, and WebSocket-sent;
- no incomplete sequence IDs or cleanup errors;
- maximum NMT/TTS/output queue depths: 2/1/1; and
- zero blocked queue puts.

Nemotron's word offsets did not move backward. The last interim used the
processed-audio horizon while the final used the last finalized-word offset,
which is normal hypothesis contraction. The crash came from mixing two
monotonic observations: the blocking ASR producer stamped a final just before
the asyncio consumer performed a newer age poll. The queued final then looked
slightly older to the strict single-owner segmenter.

The fix preserves producer capture time and source word offsets, but assigns a
nondecreasing event-loop observation time at the segmentation boundary. It is
covered by parser, segmenter, and end-to-end pipeline regressions using the
observed trace shape.

## Attempt 2: invalid short-segment NMT output

With the clock fix, the retry ran cleanly for 807.9 seconds and delivered 260
ordered translated-audio segments. It then failed fast on TTS sequence 260,
wrote partial JSON/CSV/plot artifacts, and exited nonzero as intended.

Operational evidence at failure:

- input: 2,693 300 ms chunks (807.9 seconds), intentionally incomplete after
  the error terminal;
- translated output: 25,321,540 bytes / 791.298 seconds;
- first translated audio: 5.018 seconds;
- 263 text segments emitted and 260 audio segments completed/sent;
- incomplete sequence IDs: 260, 261, 262;
- maximum NMT/TTS/output depths: 4/4/1;
- blocked puts: NMT 5, TTS 0, output 0; and
- one ordered WebSocket error after all 260 delivered PCM frames.

The affected ASR final covered source 799.44-806.00 seconds. It contained:

```text
It moves from. uh. from something that you are detached. You say I know
about it in a detached manner. You see the connections
```

Punctuation splitting isolated `uh.` as a three-character segment. NMT 1.5.2
returned Chinese `呃。` while declaring the response language `es-US`.
Magpie's Spanish request then logged that neither character existed in its
dictionary and intermittently failed inside Triton with an empty token tensor:

```text
Target sizes: [8, 6]. Tensor sizes: [8, 0]
```

This was not a transient payload worth retrying. Repeating unchanged `呃。`
sometimes returned audio, but every request used unsupported characters and
could produce wrong-language or invalid speech. Additional probes found the
same wrong-script behavior for isolated `Okay.` and `Amen.`.

The corrective policy is layered:

1. explicitly suppress known standalone hesitation fillers before allocating
   an NMT sequence, while retaining telemetry and preserving meaningful short
   utterances such as `No.`;
2. use narrow deterministic Spanish translations for known ambiguous
   standalone `Okay`/`OK` and `Amen` variants;
3. normalize and validate target text after NMT and defensively before TTS;
   Spanish must contain speakable content, use the explicit Magpie-safe
   punctuation allowlist, and contain no non-Latin letters or control/format
   characters; and
4. fail closed on any other invalid target text. Do not mask it with an
   unchanged NMT or TTS retry.

The exact 790-815 second region was then replayed through the complete staged
WebSocket path. It produced 10 ordered segments, discarded the isolated
filler once, completed with no missing IDs, and made no invalid Magpie request.

## Attempt 3: full operational pass

The third real-time run reached natural completion across the entire
1,888.1045-second source:

- input completed: 6,294 paced chunks;
- output: 646 PCM segments / 61,402,404 bytes, with contiguous IDs 0-645;
- completion/send/receive byte and sequence parity: exact;
- standalone fillers discarded before sequence allocation: 6;
- incomplete IDs, pipeline failure, cleanup errors, connection loss, and
  server errors: none;
- exactly one `completed` status after the final PCM frame, with no PCM after
  completion;
- first translated audio: 5.113 seconds;
- last-audio arrival tail: 0.850 seconds;
- completed-terminal arrival after input ended: 1.744 seconds;
- harness drain observation including polling/terminal-settle time: 2.255
  seconds;
- maximum NMT/TTS/output queue depths: 4/4/1 against 4/4/4 bounds; and
- blocked queue puts: 15/0/0, showing bounded NMT backpressure worked.

All three containers remained healthy with zero restarts. The post-run GPU
snapshot was 32,217 MiB used and 65,034 MiB free.

This is an operational pass, not an audience-latency pass. Magpie produced
1,918.825 seconds of Spanish audio, 1.01627x the English source or 30.721
seconds longer. Replaying client arrivals at fixed 1.00x ended 64.038 seconds
after English input. The 1.00x/1.05x/1.10x adaptive policy reduced that tail to
14.246 seconds (77.75%) without dropping audio, but its queue still peaked at
28.052 seconds and remained above 10 seconds for 633.583 seconds. It therefore
did not achieve the proposed 5-second target / 10-second soft ceiling.

These playback values are deterministic arrival-trace simulations. They are
not an executed browser Web Audio queue measurement or an English-punchline to
Spanish-audible semantic-delay measurement.

## Fast reproduction

The exact ASR region can be isolated without replaying 13 minutes in real
time. Decode roughly 790-815 seconds to 16 kHz mono PCM and use the existing
direct ASR smoke with `--fast`. A context-faithful accelerated replay of the
first 810 seconds completed in about 98 seconds:

```bash
PYTHONPATH=.python-packages:backend python3 direct_asr_smoke.py \
  --file "${S2S_TEST_AUDIO_DIR:-test_audio}/long-form-03-30min.wav" \
  --duration-seconds 810 \
  --fast \
  --json-output /tmp/sample_03-prefix-810-asr.json
```

The raw attempt captures were reviewed before being removed from public
history. This report retains the aggregate measurements, failure analysis, and
integrity conclusions; future full JSON/CSV/log/plot evidence remains local and
ignored unless separately minimized and sanitized.

## Post-capture hardening and evidence boundary

The successful live attempt predates the final release-hardening changes. Its
raw trace independently demonstrates an `end_input` boundary before the sole
completion and exact parity for all 646 server sends/client receives and all
61,402,404 PCM bytes. Its archived full summary does not include today's
`modelConfig`, `target_language`, or top-level terminal-arrival fields, so it
will deliberately fail the new resume validator. Treat it as historical live
evidence, not a new-format resumable checkpoint.

The compact capture also lacks a source SHA and immutable Git commit/worktree
snapshot, and the live work occurred on an uncommitted branch. Its image
evidence is version-tag level rather than verified digest level. The raw event
behavior is auditable, but the run is not exactly reproducible from an
immutable code/model revision. Future formal runs must start from a clean
commit, retain the harness's source SHA, and use independently verified image
digests.

The subsequent code now rejects completion before `end_input`, enforces exact
PCM count-and-byte parity automatically, freezes declared ASR/NMT/TTS
provenance, and separates the 1.744-second terminal-arrival lag from the
2.255-second harness polling/settle observation. Backend hardening also makes
cleanup cancellation-safe, prevents displaced sessions from restarting,
avoids lifecycle locks across WebSocket writes, latches protocol errors, and
rejects Spanish-target punctuation outside Magpie's explicit safe set. These
changes passed the deterministic test suite, but no full GPU canary has yet
been rerun on that final code snapshot.

## Promotion status

The one-minute staged WebSocket gate and historical Sample 03 operational gate
are passed. A short final-snapshot preflight should precede the remaining
staged Sample 01/Sample 02/full matrix,
an actual browser Web Audio run, marked-phrase/punchline delay, and native
Spanish review of sustained 1.05x/1.10x playback or TTS prosody changes.

For live listeners, retain a target queue near 5 seconds with 10 seconds as a
soft ceiling, but treat that as an objective requiring an explicit catch-up or
content policy. The current no-drop 1.10x ceiling cannot enforce it by itself.
