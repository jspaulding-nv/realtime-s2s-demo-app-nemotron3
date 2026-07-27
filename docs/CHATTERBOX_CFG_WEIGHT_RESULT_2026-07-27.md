# Chatterbox `cfg_weight` Result — 2026-07-27

## Decision

The pinned Chatterbox TTS Multilingual Speech NIM `1.0.0` accepted explicit
`cfg_weight` values `0.3`, `0.5`, and `0.7` through the Riva
`custom_configuration` map. Healthy no-key controls bracketed the compatibility
smoke, and all 40 requests in the promoted balanced matrix succeeded.

The balanced run did **not** demonstrate a stable `cfg_weight` duration effect.
It therefore produced no realtime promotion candidate. Keep the parameter out
of the staged speech-to-speech integration unless NVIDIA documents it as a
supported Speech NIM contract and a future controlled test shows a repeatable,
useful effect.

This result means `accepted`, not `supported`, `effective`, or `ignored`.

## Tested boundary

- GPU: NVIDIA RTX PRO 6000 Blackwell Server Edition, 97,887 MiB total
- Chatterbox image:
  `chatterbox-tts-multilingual:1.0.0@sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6`
- Chatterbox profile: `name=chatterbox-tts-multilingual`
- Client: exactly `nvidia-riva-client==2.26.0`
- Locale/voice: `es-ES` / `Chatterbox-Multilingual.es-ES.Male`
- Neutral fixture: the same versioned, text-free identity used by the July 26
  Chatterbox/Magpie comparison
- Exaggeration factors: `0.5`, `0.7`
- `cfg_weight` cells: omitted, `0.3`, `0.5`, `0.7`
- Repeats: five per cell, rotated across 40 measured requests
- Warm-up: one omitted-key request, discarded from all measurements
- Per-request limits: 60-second RPC deadline and 60-second PCM-duration cap

Speech NIM `26.05.0` documents only `exaggeration_factor` for this container.
The upstream model and a separate NVIDIA ACE plugin expose `cfg_weight`, but
neither establishes a supported API contract for this pinned Speech NIM.

## Compatibility smoke

The smoke order was:

```text
discarded no-key warm-up
no key -> 0.3 -> 0.5 -> 0.7 -> no key
```

The warm-up and all five measured requests returned valid, nonempty PCM. Both
no-key controls succeeded, and the three weighted requests were classified
`accepted`. The field was omitted entirely from control request maps.

The single smoke observations varied enough to require the balanced matrix.
Transport success alone was deliberately not treated as evidence that the
model applied the parameter.

## Balanced matrix

All 40 measured requests succeeded. The following values are medians of five
warm requests per cell:

| Exaggeration | `cfg_weight` | Audio duration | First audio | RPC wall | RTF | Immediate underruns | Median underrun risk |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.5 | omitted | 4.210 s | 0.885 s | 4.927 s | 1.198 | 5/5 | 0.752 s |
| 0.5 | 0.3 | 4.170 s | 0.870 s | 4.983 s | 1.187 | 5/5 | 0.655 s |
| 0.5 | 0.5 | 4.410 s | 0.870 s | 5.255 s | 1.175 | 5/5 | 0.665 s |
| 0.5 | 0.7 | 4.130 s | 0.885 s | 4.906 s | 1.209 | 5/5 | 0.683 s |
| 0.7 | omitted | 4.330 s | 0.880 s | 5.083 s | 1.198 | 5/5 | 0.703 s |
| 0.7 | 0.3 | 4.130 s | 0.866 s | 4.909 s | 1.191 | 5/5 | 0.681 s |
| 0.7 | 0.5 | 4.210 s | 0.874 s | 5.100 s | 1.215 | 5/5 | 0.792 s |
| 0.7 | 0.7 | 4.170 s | 0.886 s | 4.978 s | 1.194 | 5/5 | 0.685 s |

No cell met the predeclared 5% median duration-reduction threshold relative to
its same-exaggeration omitted control. The closest was `cfg_weight=0.3` at
exaggeration `0.7`, approximately 4.62% shorter.

## Effect gate

A stable effect required all of the following:

- at least four of five paired `0.3`-versus-`0.7` repetitions agree on the
  direction at each exaggeration level;
- the direction is the same at both exaggeration levels; and
- the extreme-cell median difference is at least 5% or 0.25 seconds.

At exaggeration `0.5`, `0.3` and `0.7` had median durations of 4.170 and
4.130 seconds, respectively. Directional agreement was only two of five. At
exaggeration `0.7`, the medians reversed to 4.130 and 4.170 seconds, with only
three of five directional agreements. Both median differences were 0.040
seconds, or approximately 0.96%.

The result is therefore:

```text
compatibility: accepted
effect: effect_not_demonstrated
realtime candidate: not evaluated
```

The NIM response does not echo applied custom configuration, and synthesis is
stochastic. The result cannot distinguish a parsed-but-weak parameter from an
accepted-but-ignored parameter. It must not be described as proof that
`cfg_weight` is ignored.

## Realtime interpretation

The parameter did not address the existing Chatterbox streaming constraint:

- every cell showed immediate-playback underruns in all five requests;
- median RTF remained approximately `1.175`-`1.215`;
- median first audio remained approximately `0.866`-`0.886` seconds; and
- no cell demonstrated the required stable duration reduction.

The original Chatterbox model comparison remains promising because its output
was materially shorter than Magpie for the matched fixture. This follow-up
shows that `cfg_weight` is not currently an evidence-backed way to improve that
candidate. The subsequent
[multi-text mechanical gate](TTS_MULTITEXT_RESULT_2026-07-27.md) completed but
failed its predeclared stream-continuity cap, so it created no blinded native
Spanish review bundle and did not permit default-off staged integration.

## Runtime and operational result

The final smoke and matrix ran with ASR, NMT, Magpie, and Chatterbox healthy.
Chatterbox had zero restarts and no OOM. Post-run GPU use was 85,278 MiB with
11,973 MiB free.

An initial pre-run container start used an empty checkout-local cache that was
not writable by the container and exited before model load. Restarting the
isolated service with the documented writable shared-cache override resolved
the host-mount issue. That failed startup is excluded from all model and
parameter conclusions.

After the measurements, only Chatterbox was stopped. The running container had
been created before the Compose file first gained a two-minute stop grace
period, so it retained Docker's prior ten-second stop limit and exited `137`
when model shutdown exceeded that limit. Docker reported `OOMKilled=false`.
ASR, NMT, and Magpie remained healthy, and GPU use returned to 32,300 MiB. A
later multi-text run showed that even two minutes was insufficient, so the
current Compose definition grants five minutes to subsequently recreated
Chatterbox containers. These shutdown outcomes are operational evidence, not
synthesis failures.

## Reproduction

Start the isolated pinned service using a writable cache:

```bash
LOCAL_NIM_CACHE=/absolute/path/to/shared/nim-cache \
  docker compose --profile chatterbox-canary up -d chatterbox-tts

curl --fail http://localhost:9004/v1/health/ready
```

Run the compatibility smoke:

```bash
PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/.python-packages-chatterbox:$PWD" \
  python3 -S chatterbox_cfg_weight_probe.py
```

Only if it reports `accepted`, run the matrix:

```bash
PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/.python-packages-chatterbox:$PWD" \
  python3 -S chatterbox_cfg_weight_probe.py \
    --balanced-matrix \
    --repeats-per-cell 5
```

Balanced mode now exits `3` when transport is accepted but the effect/realtime
promotion gate is not met, as in this result. Exit zero is reserved for a
demonstrated realtime candidate; the JSON report remains the authoritative
classification.

Generated JSON and WAV artifacts stay under ignored `experiment_results/`.
Directories are mode `0700`; files are mode `0600`. JSON excludes input text,
paths, service hostnames, raw exception messages, PCM, and audio fingerprints.
WAV files contain synthesized speech and require review before sharing.

## Validation

- Focused Chatterbox/probe tests: 76 passed
- Maintained `tests/` suite: 882 passed, 1 skipped
- Live smoke: 5/5 measured requests plus warm-up succeeded
- Live matrix: 40/40 measured requests plus warm-up succeeded
- Pre-shutdown health: all four services healthy; zero Chatterbox restarts;
  no OOM
- Post-shutdown health: ASR, NMT, and Magpie healthy; Chatterbox stopped;
  GPU use returned to 32,300 MiB

Official context:

- [NVIDIA Speech NIM customization](https://docs.nvidia.com/nim/speech/26.05.0/tts/customization.html#emotion-exaggeration)
- [NVIDIA Speech NIM release notes](https://docs.nvidia.com/nim/speech/26.05.0/about/release-notes.html)
- [NVIDIA ACE Chatterbox plugin options](https://docs.nvidia.com/ace-for-games/chatterbox-tts/samples.html#command-line-options)
- [Upstream Chatterbox tuning notes](https://github.com/resemble-ai/chatterbox#original-chatterbox-tips)
