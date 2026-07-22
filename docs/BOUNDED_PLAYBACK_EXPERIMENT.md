# Bounded playback experiment

## Objective

Determine whether Nemotron 3 ASR plus adaptive Spanish playback can keep the
listener backlog in an acceptable 5-10 second operating range over complete
sermons without unacceptable speech-quality loss.

The experiment must also measure semantic English-to-Spanish delay at marked
phrases. Queue depth alone cannot answer the live-audience joke question.

The 10-second threshold is a soft/SLA ceiling. No condition in this experiment
authorizes dropping translated speech.

The completed one-repeat live capture and deterministic matched-trace replay
from July 22, 2026 are documented in
[Three-sermon acceptance run](ACCEPTANCE_RUN_2026-07-22.md). The service path
completed, but all three sermon traces missed the overall candidate gate set.

## Fixed test configuration

Use the repository's pinned images for every comparable run:

| Stage | Image | Host endpoint |
|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `localhost:50052` |
| NMT/S2S | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `localhost:50051` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `localhost:50053` |

Also hold these settings constant:

- Nemotron English streaming profile, `batch_size=32`;
- English `en-US` to Spanish `es-US`;
- `Magpie-Multilingual.ES-US.Isabela`;
- 16 kHz mono Int16 PCM;
- 300 ms input chunks at real-time pace;
- automatic ASR punctuation enabled; and
- final EOU window of 800 ms.

Do not infer that a lower EOU is better. Riva's latest analysis found that 800
ms with punctuation handling outperformed a 300 ms configuration without
punctuation handling for this workload.

Record the exact Git commit, image references or digests, GPU model, driver,
free/used GPU memory, CPU load, and network path with every run.

## Prepare a new VM

```bash
git clone https://github.com/jspaulding-nv/realtime-s2s-demo-app-nemotron3.git
cd realtime-s2s-demo-app-nemotron3

cp .env.example .env
mkdir -p .cache/nim
chmod 777 .cache/nim
```

Add an active NGC Personal Key to `.env`. Compose reads `.env` automatically,
but `docker login` does not, so export the file into the current shell before
authenticating:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin
```

If login returns `unauthorized`, replace or reactivate the NGC Personal Key.
Do not paste the key into logs, Markdown, shell history, or Git.

Start and validate the pinned stack:

```bash
nvidia-ctk cdi list
docker compose config --images
docker compose pull
docker compose up -d
docker compose ps

curl --fail http://localhost:9002/v1/health/ready  # ASR
curl --fail http://localhost:9001/v1/health/ready  # NMT
curl --fail http://localhost:9003/v1/health/ready  # TTS
nvidia-smi
```

The selected profiles use approximately 26.4 GB of GPU memory in aggregate in
the observed setup. They fit the 96 GB RTX PRO 6000 Blackwell Server Edition;
a 48 GB RTX 6000 Ada should have nominal capacity, but capture peak memory
during a real stream before declaring that configuration supported.

Start the application after the services are healthy:

```bash
set -a
source .env
set +a
./start.sh
```

Open `http://localhost:5173/#/test` for the browser test dashboard.

## Pre-run software validation

From the repository root, run the backend suite in its configured Python
environment:

```bash
python -m pytest backend/tests tests -q
```

Use Node.js 22 LTS for the frontend:

```bash
cd frontend
npm ci
npm test
npm run build
npm run lint
cd ..
```

At the documentation snapshot, 70 combined Python backend/analysis/harness tests and
81 frontend tests passed, and the frontend production build and lint both
passed.

## Offline replay expectation

Before a new live run, the saved July 8 arrival traces were replayed through a
deterministic implementation of the browser policy:

| Saved trace | Simulated adaptive tail | Arrival-sampled queue p95 | Peak queue | Playback time over 10 s |
|---|---:|---:|---:|---:|
| Spirit | 27.183 s | 38.31 s | 46.54 s | 80.0% |
| Blessed | 40.206 s | 44.95 s | 53.11 s | 77.3% |
| Beholding | 16.613 s | 19.48 s | 23.81 s | 45.6% |

Aggregate tail was 82.2% lower than fixed 1.00x replay, with no chunks
dropped. This is not a new live Riva or browser run. It predicts two things the
formal experiment must test: 1.10x can sharply reduce the final tail, but it is
unlikely to hold the listener queue below the 10-second soft ceiling without
upstream improvements. It also predicts long exposure to accelerated audio,
so the listening-quality evaluation is essential.

Reproduce the offline analysis when the ignored event CSVs are available:

```bash
python analyze_playback_policy.py \
  --input-dir test_results_nemotron \
  --json-output docs/results/nemotron3/playback_policy_analysis.json \
  --markdown-output docs/results/nemotron3/playback_policy_analysis.md
```

Run its focused regression tests with:

```bash
python -m pytest \
  tests/test_playback_simulation.py \
  tests/test_analyze_playback_policy.py -q
```

The generated report is
[`results/nemotron3/playback_policy_analysis.md`](results/nemotron3/playback_policy_analysis.md).

## Automated three-sermon matched-trace run

Install the root experiment dependencies before the first run:

```bash
pip install -r requirements.txt
```

This installs the pinned long-form runner dependencies, including
`websockets==15.0.1`, `matplotlib==3.10.9`, and `imageio-ffmpeg==0.6.0`.

Start the pinned containers and FastAPI backend separately, verify that they
are ready, and run the full experiment from the repository root:

```bash
python run_three_sermon_experiment.py
```

This one command performs health checks and the one-minute preflight, then
streams all three sermon files at real-time pace:

```text
test_audio/200108_SpiritandPresenceofGod.mp3
test_audio/Blessed_Self-Forgetfulness.mp3
test_audio/gospel_in_life_tk_1-john-part-2-mp3_Beholding_the_Love_of_God.mp3
```

Inspect the complete plan without contacting the backend, collect three live
traces per sermon, or continue an interrupted output directory with:

```bash
python run_three_sermon_experiment.py --dry-run
python run_three_sermon_experiment.py --repeats 3
python run_three_sermon_experiment.py --resume-dir experiment_results/<run-id>
```

`--dry-run` prints the resolved run directory, backend, preflight setting,
repeat count, files, and order without contacting the backend or Riva and
without creating a run directory. A normal run creates this ignored artifact
tree:

```text
experiment_results/YYYYMMDDTHHMMSSZ_<git-short-sha>/
├── manifest.json
├── playback_policy_analysis.json
├── playback_policy_analysis.md
├── preflight/
│   ├── test-1min_results.csv
│   ├── test-1min_summary.json
│   └── test-1min_latency.png
└── repeat-01/
    ├── <sermon-stem>_results.csv
    ├── <sermon-stem>_summary.json
    └── <sermon-stem>_latency.png
```

Each one-based, zero-padded `repeat-NN` directory contains that three-file set
for all three sermon stems. The manifest is also the resumable status and
checkpoint record. `--resume-dir` must point to an existing run with a
compatible manifest. The harness validates required artifacts before skipping
a completed entry and retries incomplete or failed entries. Resume is allowed
only from the same clean Git commit with sermon size and SHA-256 unchanged.
Each saved CSV, summary, and plot must match its manifest hash; CSV sent/receive
counts and received-byte totals must also match the summary. Backend and repeat
settings come from the manifest, and explicit values must match. Optional
new-run controls are `--output-root`, `--run-id`, and `--skip-preflight`;
preflight cannot be changed on resume.

One repeat is one live Riva pass through each sermon. It takes approximately 1
hour 45 minutes (103.7 minutes) of source-audio time. A new experiment adds a
single one-minute preflight, and every capture adds its translated-tail drain
time. Repeats and sermons run sequentially because this backend supports one
active translation session and its timing logger represents one global test
session. Do not launch concurrent harnesses against the same backend.

The harness also takes a backend-keyed local `flock`, so a second harness on
the same machine fails before touching that backend. This does not coordinate
clients launched from another machine; keep the backend isolated for the run.

The harness does not run `docker compose up`, `docker compose down`, start the
FastAPI backend, or stop any service after the test. Operators retain control
of those processes, and the harness leaves them running for inspection or a
resumed run.

Failure to meet the 5-10 second queue objective is an experimental result, not
an operational harness failure. The final playback-policy analysis flags those
SLA misses; the process fails for problems such as an unavailable backend,
failed preflight, zero translated responses, or missing required artifacts.
After `end_input`, the CLI requires the backend's terminal `completed` status,
which requires the request iterator to consume its stop sentinel, successful
Riva response-generator exhaustion, and completion of every pending PCM
WebSocket send. The sentinel proves every queued source chunk was read; if ASR
endpointing closes earlier, the client restarts the generator to drain the
remaining input. Generator/final-flush errors or audio-send failures emit
`error` and invalidate the capture. Five seconds of silence is not completion,
and reaching the 300-second drain maximum is a capture failure.

New artifacts are generated in a temporary `.staging` directory. The harness
validates the full CSV/summary/plot set, promotes it into `preflight` or
`repeat-NN`, records SHA-256 hashes, and validates the promoted set again before
marking the manifest entry complete.

Each live arrival trace produces two derived playback conditions:

| Derived condition | Playback policy | Purpose |
|---|---|---|
| Fixed control | Fixed 1.00x | Estimate listener backlog without catch-up |
| Adaptive | 1.00x / 1.05x / 1.10x at 5/8 seconds | Estimate backlog reduction on the identical Riva output |

This pairing is intentional. Playback speed is downstream of Riva and cannot
change ASR, NMT, TTS, translated PCM, or its arrival timestamps. Replaying the
same trace through both policies therefore removes inference and network
variation from the fixed-versus-adaptive comparison and halves the number of
long-form Riva passes. Use at least three live repeats per sermon with
`--repeats 3`; every repeat supplies both derived conditions.

These derived conditions use the unit-tested Python policy scheduler, not a real
`AudioContext`. They do not test browser timer behavior, Web Audio scheduling,
pitch or chunk-boundary artifacts, subjective intelligibility, or listening
fatigue. They also cannot locate corresponding English and Spanish semantics,
so they do not measure how long an audience waits for a translated joke or
marked phrase.

The candidate queue gate uses the exact time-weighted p95 across the simulated
playback window. It is not the nearest-rank percentile of queue depths sampled
at audio arrivals. Reports also include exact time above 10 seconds and the
longest continuous interval scheduled at 1.10x.

## Run procedure

For an automated run:

1. Commit the intended code, confirm the worktree is clean, then confirm all
   three readiness endpoints and save `docker compose ps` and `nvidia-smi`
   output. A run started dirty cannot later pass resume provenance checks.
2. Run `--dry-run` and review the three resolved input paths, backend URL,
   repeat count, and output location.
3. Run the harness normally. Let every file finish at real-time pace and allow
   its complete translated tail to drain. Accept a capture only after the
   backend sends terminal `completed` with no terminal error; a 300-second
   timeout is a failure.
4. If the process is interrupted, correct the underlying problem and use
   `--resume-dir` with that experiment directory instead of overwriting valid
   completed traces.
5. Confirm every requested repeat contains results for all three sermons and
   that each trace has nonzero translated responses, valid CSV/summary counts,
   and manifest hashes.
6. Review the compact fixed-versus-adaptive analysis and retain the raw CSVs,
   plots, summaries, and manifest under the same experiment run.
7. Save backend/container logs, WebSocket failures, container restarts, and GPU
   memory/utilization separately with the generated run ID.

Then perform a browser and listener validation. Open `/#/test`, select or clear
**Adaptive Spanish playback**, load a representative sermon or shorter excerpt,
and let the browser drain naturally. Export the dashboard CSV and compare its
queue schedule with the Python replay. Use headphones and native Spanish
listeners to evaluate 1.05x/1.10x intelligibility, pitch, discontinuities, and
fatigue. Finally, mark English phrases or jokes and their corresponding Spanish
audio on a synchronized timeline; neither the automated queue replay nor the
dashboard backlog metric supplies that semantic alignment.

The generated directory name is the automated run ID, for example:

```text
20260722T143000Z_d46451d
```

Keep large CSV, audio, and plot artifacts outside Git or in an approved
artifact store. Commit compact, sanitized summaries only.

## Required measurements

For every automated live trace and matched replay, report:

- first translated audio latency;
- service flush tail;
- output/input duration ratio and duration excess;
- fixed and adaptive simulated listener playback tail;
- simulated time-weighted queue p50 and p95, plus maximum, for each policy;
- seconds and percent over 5 seconds and over 10 seconds;
- number of over-limit excursions;
- simulated audio duration scheduled at each playback rate;
- longest continuous interval scheduled at 1.10x;
- container/runtime failures and reconnects; and
- GPU memory and utilization peaks.

For the separate browser and listener validation, report actual Web Audio queue
statistics, any scheduling or playback artifacts, native-listener quality, and
synchronized English-to-Spanish semantic delay for marked phrases or jokes.
Follow the procedure in [Audience latency metrics](AUDIENCE_LATENCY_METRICS.md).
Do not relabel Python replay metrics as browser observations.

## Candidate acceptance gates

Agree final gates with Pellera before treating the experiment as a product
decision. A reasonable starting point is:

- automated time-weighted simulated queue p95 at or below 10 seconds for every sermon;
- no sustained upward queue trend in the final third of a sermon;
- time over 10 seconds below 1% of the observed run;
- no dropped or reordered speech;
- no container restart or WebSocket failure;
- marked-phrase delay acceptable for a live room; and
- native Spanish listeners accept intelligibility and naturalness at the
  selected speeds for long-form listening.

The automated p95 gate is a deterministic replay result. Confirm it separately
against actual browser/Web Audio behavior before using it as a product gate.

Treat failure to satisfy the 10-second queue gate as an SLA breach, not a
request to discard audio. If 1.10x cannot consume output as fast as it is
produced, proceed to server-side prosody or pitch-preserving time-scale work
and the staged backend design.

## Next-run checklist

- [ ] Active NGC key loaded with `source .env`; no secret in Git or logs
- [ ] Exact pinned images shown by `docker compose config --images`
- [ ] ASR, NMT, and TTS readiness probes pass
- [ ] GPU model, driver, memory, and utilization recorded
- [ ] Git commit and playback policy recorded in the harness manifest
- [ ] Backend tests, frontend tests, build, and lint pass
- [ ] 800 ms EOU and automatic punctuation confirmed
- [ ] Harness dry run resolves the expected backend, inputs, and output directory
- [ ] Automated one-minute preflight produces translated audio
- [ ] Every automated capture receives terminal `completed` before 300 seconds
- [ ] No generator, final-flush, or PCM WebSocket send error is reported
- [ ] Three sermons complete sequentially with nonzero translated responses
- [ ] At least three live traces per file; each is replayed as a matched fixed and adaptive pair
- [ ] Interrupted-run recovery is verified with `--resume-dir` when applicable
- [ ] Resume commit, clean worktree, sermon hashes, and artifact hashes validate
- [ ] CSV event counts and received-byte totals match every summary
- [ ] Generated manifest, raw traces, summaries, plots, and compact analysis retained
- [ ] Separate browser run reaches natural `end_input` and complete Web Audio drain
- [ ] Browser CSV contains playback schedule and queue sample fields
- [ ] Python replay is cross-checked against browser queue scheduling
- [ ] Marked phrase/joke delay measured on a synchronized clock
- [ ] Native-listener quality notes captured at 1.05x and 1.10x
- [ ] Compact summary explicitly distinguishes historical and new runs

## Graceful shutdown

```bash
docker compose down
```

This preserves `.cache/nim`. Do not use `docker compose down --rmi all` unless
removing the downloaded model images is intentional.
