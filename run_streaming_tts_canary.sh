#!/usr/bin/env bash
set -euo pipefail

# Run one shared source prefix through either matched atomic/schema-1 and
# incremental/schema-3 arms, one schema-3 publisher-handoff diagnostic arm,
# or one aggregate-only synthesized low-energy PCM diagnostic arm.
# This script owns only its FastAPI child processes; it never starts, stops, or
# mutates the Riva NIM containers.

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$REPOSITORY_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
CANARY_SOURCE="${CANARY_SOURCE:-test_audio/long-form-01.mp3}"
CANARY_DURATION_SECONDS="${CANARY_DURATION_SECONDS:-60}"
CANARY_MODE="${CANARY_MODE:-matched}"
if [[ -z "${CANARY_INCREMENTAL_FRAME_MS:-}" ]]; then
  if [[ "$CANARY_MODE" == "handoff" || "$CANARY_MODE" == "silence" ]]; then
    # Match the registered 500 ms profile used by the retained three-sample
    # publisher-gap baseline. Callers may override this for sensitivity runs.
    CANARY_INCREMENTAL_FRAME_MS=500
  else
    # Preserve the historical matched-canary profile.
    CANARY_INCREMENTAL_FRAME_MS=100
  fi
fi
CANARY_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS="${CANARY_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS:-4}"
CANARY_BACKEND_PORT="${CANARY_BACKEND_PORT:-8100}"
CANARY_OUTPUT_ROOT="${CANARY_OUTPUT_ROOT:-experiment_results}"
ALLOW_DIRTY_CANARY="${ALLOW_DIRTY_CANARY:-0}"
ASR_HTTP_PORT="${ASR_HTTP_PORT:-9002}"
NMT_HTTP_PORT="${NMT_HTTP_PORT:-9001}"
TTS_HTTP_PORT="${TTS_HTTP_PORT:-9003}"
ASR_IMAGE="${ASR_IMAGE:-nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0}"
NMT_IMAGE="${NMT_IMAGE:-nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2}"
TTS_IMAGE="${TTS_IMAGE:-nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0}"
CONFIGURED_ASR_IMAGE_DIGEST="${ASR_IMAGE_DIGEST:-}"
CONFIGURED_NMT_IMAGE_DIGEST="${NMT_IMAGE_DIGEST:-}"
CONFIGURED_TTS_IMAGE_DIGEST="${TTS_IMAGE_DIGEST:-}"
CANARY_CURL_CONNECT_TIMEOUT_SECONDS="${CANARY_CURL_CONNECT_TIMEOUT_SECONDS:-2}"
CANARY_CURL_MAX_TIME_SECONDS="${CANARY_CURL_MAX_TIME_SECONDS:-5}"
CANARY_BACKEND_STOP_TIMEOUT_SECONDS="${CANARY_BACKEND_STOP_TIMEOUT_SECONDS:-15}"

fail_usage() {
  echo "$1" >&2
  exit 2
}

[[ -f "$CANARY_SOURCE" ]] ||
  fail_usage "Canary source does not exist: $CANARY_SOURCE"
for setting in \
  "CANARY_DURATION_SECONDS:$CANARY_DURATION_SECONDS" \
  "CANARY_INCREMENTAL_FRAME_MS:$CANARY_INCREMENTAL_FRAME_MS" \
  "CANARY_BACKEND_PORT:$CANARY_BACKEND_PORT" \
  "ASR_HTTP_PORT:$ASR_HTTP_PORT" \
  "NMT_HTTP_PORT:$NMT_HTTP_PORT" \
  "TTS_HTTP_PORT:$TTS_HTTP_PORT" \
  "CANARY_CURL_CONNECT_TIMEOUT_SECONDS:$CANARY_CURL_CONNECT_TIMEOUT_SECONDS" \
  "CANARY_CURL_MAX_TIME_SECONDS:$CANARY_CURL_MAX_TIME_SECONDS" \
  "CANARY_BACKEND_STOP_TIMEOUT_SECONDS:$CANARY_BACKEND_STOP_TIMEOUT_SECONDS"; do
  name="${setting%%:*}"
  value="${setting#*:}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] ||
    fail_usage "$name must be a positive integer"
done
[[ "$ALLOW_DIRTY_CANARY" == "0" || "$ALLOW_DIRTY_CANARY" == "1" ]] ||
  fail_usage "ALLOW_DIRTY_CANARY must be 0 or 1"
[[ "$CANARY_MODE" == "matched" ||
  "$CANARY_MODE" == "handoff" ||
  "$CANARY_MODE" == "silence" ]] ||
  fail_usage "CANARY_MODE must be matched, handoff, or silence"
[[ "$CANARY_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS" =~ ^[0-9]+$ ]] ||
  fail_usage \
    "CANARY_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS must be a non-negative integer"

EVIDENCE_CLASS="formal"
if [[ "$ALLOW_DIRTY_CANARY" == "1" ]]; then
  EVIDENCE_CLASS="non-formal-probe"
  echo \
    "NON-FORMAL PROBE: the clean-worktree evidence gate is bypassed; ignored output remains mandatory." \
    >&2
elif [[ -n "$(git status --porcelain)" ]]; then
  fail_usage \
    "Refusing a formal evidence run from a dirty worktree. Commit first or set ALLOW_DIRTY_CANARY=1."
fi

OUTPUT_RESOLVED="$(realpath -m -- "$CANARY_OUTPUT_ROOT")"
case "$OUTPUT_RESOLVED" in
  "$REPOSITORY_ROOT"/*) ;;
  *) fail_usage "CANARY_OUTPUT_ROOT must resolve inside the repository" ;;
esac
OUTPUT_RELATIVE="${OUTPUT_RESOLVED#"$REPOSITORY_ROOT"/}"
if ! git check-ignore --quiet -- "$OUTPUT_RELATIVE"; then
  fail_usage "Canary evidence requires an ignored CANARY_OUTPUT_ROOT"
fi
mkdir -p -- "$OUTPUT_RESOLVED"
CANARY_OUTPUT_ROOT="$(realpath -e -- "$OUTPUT_RESOLVED")"
[[ "$CANARY_OUTPUT_ROOT" == "$OUTPUT_RESOLVED" ]] ||
  fail_usage "CANARY_OUTPUT_ROOT changed while being prepared"

validate_pinned_image() {
  local service="$1"
  local image="$2"
  local leaf="${image##*/}"
  if [[ "$image" == *[$' \t\r\n']* ]] ||
    [[ "$leaf" != *:* ]] ||
    [[ "$leaf" == *:latest ]]; then
    echo "$service image must use an explicit, non-latest tag" >&2
    return 1
  fi
}

resolve_running_image_digest() {
  local service="$1"
  local host_port="$2"
  local expected_image="$3"
  local configured_digest="$4"
  local ids_text=""
  local container_id=""
  local ports_json=""
  local image_ref=""
  local image_id=""
  local repo_digests=""
  local repo="${expected_image%:*}"
  local repo_digest=""
  local digest=""
  local -a matches=()

  ids_text="$(docker ps --filter status=running --quiet)" ||
    return 1
  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    ports_json="$(
      docker inspect --format '{{json .NetworkSettings.Ports}}' "$container_id"
    )" || return 1
    if "$PYTHON_BIN" -c '
import json, sys
ports = json.loads(sys.argv[1]) or {}
target = sys.argv[2]
raise SystemExit(0 if any(
    item.get("HostPort") == target
    for bindings in ports.values()
    for item in (bindings or ())
    if isinstance(item, dict)
) else 1)
' "$ports_json" "$host_port"; then
      matches+=("$container_id")
    fi
  done <<<"$ids_text"
  if [[ "${#matches[@]}" != "1" ]]; then
    echo \
      "$service provenance requires exactly one running container publishing HTTP port $host_port; found ${#matches[@]}" \
      >&2
    return 1
  fi

  container_id="${matches[0]}"
  image_ref="$(docker inspect --format '{{.Config.Image}}' "$container_id")" ||
    return 1
  if [[ "$image_ref" != "$expected_image" ]]; then
    echo "$service running image does not match configured pinned tag" >&2
    return 1
  fi
  image_id="$(docker inspect --format '{{.Image}}' "$container_id")" ||
    return 1
  repo_digests="$(
    docker image inspect --format '{{json .RepoDigests}}' "$image_id"
  )" || return 1
  repo_digest="$(
    "$PYTHON_BIN" -c '
import json, re, sys
values = json.loads(sys.argv[1]) or []
repo = sys.argv[2]
matches = sorted({
    value for value in values
    if isinstance(value, str)
    and re.fullmatch(re.escape(repo) + r"@sha256:[0-9a-f]{64}", value)
})
if len(matches) != 1:
    raise SystemExit(1)
print(matches[0])
' "$repo_digests" "$repo"
  )" || {
    echo "$service running image lacks one unambiguous RepoDigest" >&2
    return 1
  }
  digest="${repo_digest#*@}"
  if [[ -n "$configured_digest" ]] &&
    [[ "$configured_digest" != "$digest" ]] &&
    [[ "$configured_digest" != "$repo_digest" ]]; then
    echo "$service configured digest does not match running image" >&2
    return 1
  fi
  printf '%s\n' "$digest"
}

validate_pinned_image ASR "$ASR_IMAGE"
validate_pinned_image NMT "$NMT_IMAGE"
validate_pinned_image TTS "$TTS_IMAGE"
ASR_IMAGE_DIGEST="$(
  resolve_running_image_digest \
    ASR "$ASR_HTTP_PORT" "$ASR_IMAGE" "$CONFIGURED_ASR_IMAGE_DIGEST"
)"
NMT_IMAGE_DIGEST="$(
  resolve_running_image_digest \
    NMT "$NMT_HTTP_PORT" "$NMT_IMAGE" "$CONFIGURED_NMT_IMAGE_DIGEST"
)"
TTS_IMAGE_DIGEST="$(
  resolve_running_image_digest \
    TTS "$TTS_HTTP_PORT" "$TTS_IMAGE" "$CONFIGURED_TTS_IMAGE_DIGEST"
)"
export \
  ASR_IMAGE NMT_IMAGE TTS_IMAGE \
  ASR_IMAGE_DIGEST NMT_IMAGE_DIGEST TTS_IMAGE_DIGEST

for service_port in \
  "ASR:$ASR_HTTP_PORT" \
  "NMT:$NMT_HTTP_PORT" \
  "TTS:$TTS_HTTP_PORT"; do
  service="${service_port%%:*}"
  port="${service_port#*:}"
  echo "Checking $service readiness on HTTP port $port"
  curl --fail --silent --show-error \
    --connect-timeout "$CANARY_CURL_CONNECT_TIMEOUT_SECONDS" \
    --max-time "$CANARY_CURL_MAX_TIME_SECONDS" \
    "http://127.0.0.1:${port}/v1/health/ready" >/dev/null
done

FFMPEG_BIN="$(command -v ffmpeg || true)"
if [[ -z "$FFMPEG_BIN" ]]; then
  FFMPEG_BIN="$(
    PYTHONPATH=".python-packages:backend:." \
      "$PYTHON_BIN" -c \
        'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'
  )"
fi
[[ -x "$FFMPEG_BIN" ]] ||
  fail_usage "ffmpeg is required to create the shared source prefix"

SHORT_COMMIT="$(git rev-parse --short HEAD)"
SYNTHESIZED_PCM_SILENCE_ENABLED=0
if [[ "$CANARY_MODE" == "handoff" ]]; then
  RUN_PREFIX="publisher-handoff-canary"
  CANARY_ARMS=(streaming)
elif [[ "$CANARY_MODE" == "silence" ]]; then
  RUN_PREFIX="synthesized-pcm-silence-canary"
  CANARY_ARMS=(streaming)
  SYNTHESIZED_PCM_SILENCE_ENABLED=1
else
  RUN_PREFIX="streaming-tts-canary"
  CANARY_ARMS=(atomic streaming)
fi
RUN_ID="${RUN_PREFIX}-$(date -u +%Y%m%dT%H%M%SZ)-${SHORT_COMMIT}"
RUN_DIR="${CANARY_OUTPUT_ROOT%/}/${RUN_ID}"
PREFIX_WAV="$RUN_DIR/shared-prefix.wav"
mkdir -- "$RUN_DIR" ||
  fail_usage "Refusing to reuse an existing canary run directory"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$RUN_DIR/.matplotlib}"
mkdir -p -- "$MPLCONFIGDIR"

"$FFMPEG_BIN" -hide_banner -loglevel error -y \
  -t "$CANARY_DURATION_SECONDS" \
  -i "$CANARY_SOURCE" \
  -ar 16000 -ac 1 -c:a pcm_s16le \
  "$PREFIX_WAV"
PREFIX_SHA256="$(sha256sum "$PREFIX_WAV" | awk '{print $1}')"

{
  echo "run_id=$RUN_ID"
  echo "evidence_class=$EVIDENCE_CLASS"
  echo "git_commit=$(git rev-parse HEAD)"
  echo "canary_mode=$CANARY_MODE"
  echo "duration_seconds=$CANARY_DURATION_SECONDS"
  echo "prefix_sha256=$PREFIX_SHA256"
  echo "incremental_frame_ms=$CANARY_INCREMENTAL_FRAME_MS"
  echo "incremental_atomic_fallback_max_chars=$CANARY_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS"
  echo "streaming_audio_metadata_protocol_version=1"
  echo "synthesized_pcm_silence_enabled=$SYNTHESIZED_PCM_SILENCE_ENABLED"
  echo "asr_image=$ASR_IMAGE"
  echo "asr_image_digest=$ASR_IMAGE_DIGEST"
  echo "nmt_image=$NMT_IMAGE"
  echo "nmt_image_digest=$NMT_IMAGE_DIGEST"
  echo "tts_image=$TTS_IMAGE"
  echo "tts_image_digest=$TTS_IMAGE_DIGEST"
} >"$RUN_DIR/run_info.txt"

BACKEND_PID=""
stop_backend() {
  local owned_pid="$BACKEND_PID"
  local waited=0
  BACKEND_PID=""
  [[ -n "$owned_pid" ]] || return
  if ! kill -0 "$owned_pid" 2>/dev/null; then
    wait "$owned_pid" 2>/dev/null || true
    return
  fi
  kill -TERM "$owned_pid" 2>/dev/null || true
  while kill -0 "$owned_pid" 2>/dev/null; do
    if ((waited >= CANARY_BACKEND_STOP_TIMEOUT_SECONDS)); then
      echo "FastAPI PID $owned_pid did not stop; sending SIGKILL" >&2
      kill -KILL "$owned_pid" 2>/dev/null || true
      break
    fi
    sleep 1
    ((waited += 1))
  done
  wait "$owned_pid" 2>/dev/null || true
}
cleanup() {
  local status=$?
  trap - EXIT
  stop_backend
  exit "$status"
}
signal_exit() {
  local name="$1"
  local status="$2"
  trap - INT TERM
  echo "Received $name; stopping script-owned FastAPI" >&2
  stop_backend
  exit "$status"
}
trap cleanup EXIT
trap 'signal_exit INT 130' INT
trap 'signal_exit TERM 143' TERM

curl_local() {
  curl \
    --connect-timeout "$CANARY_CURL_CONNECT_TIMEOUT_SECONDS" \
    --max-time "$CANARY_CURL_MAX_TIME_SECONDS" \
    "$@"
}

for arm in "${CANARY_ARMS[@]}"; do
  if [[ "$arm" == "atomic" ]]; then
    incremental=0
    expected_schema=1
  else
    incremental=1
    expected_schema=3
  fi
  ARM_DIR="$RUN_DIR/$arm"
  BACKEND_LOG="$ARM_DIR/backend.log"
  mkdir -- "$ARM_DIR"

  echo
  echo "=== Starting canary arm: $arm (mode=$CANARY_MODE) ==="
  S2S_PIPELINE_MODE=staged \
  STAGED_TTS_SUBSEGMENT_MAX_CHARS=0 \
  STAGED_TTS_RESPONSE_CHUNK_TELEMETRY=1 \
  STAGED_TTS_INCREMENTAL_PUBLISH="$incremental" \
  STAGED_TTS_INCREMENTAL_FRAME_MS="$CANARY_INCREMENTAL_FRAME_MS" \
  STAGED_TTS_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS="$CANARY_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS" \
  PYTHONPATH=".python-packages:backend:." \
    "$PYTHON_BIN" -m uvicorn main:app \
      --app-dir backend \
      --host 127.0.0.1 \
      --port "$CANARY_BACKEND_PORT" \
      >"$BACKEND_LOG" 2>&1 &
  BACKEND_PID="$!"

  BACKEND_URL="http://127.0.0.1:${CANARY_BACKEND_PORT}"
  ready=0
  for _ in $(seq 1 180); do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      echo "FastAPI exited while starting $arm" >&2
      tail -80 "$BACKEND_LOG" >&2 || true
      exit 1
    fi
    if curl_local --fail --silent "$BACKEND_URL/api/config" >/dev/null; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "$ready" != "1" ]]; then
    echo "FastAPI was not ready within 180 seconds for $arm" >&2
    tail -80 "$BACKEND_LOG" >&2 || true
    exit 1
  fi

  curl_local --fail --silent "$BACKEND_URL/api/config" |
    "$PYTHON_BIN" -c '
import json, sys

expected_schema = int(sys.argv[1])
expected_incremental = sys.argv[2] == "1"
expected = sys.argv[3:]
config = json.load(sys.stdin)
staged = config["stagedConfig"]
if config.get("pipelineMode") != "staged":
    raise SystemExit("backend pipeline mode is not staged")
if staged.get("telemetrySchemaVersion") != expected_schema:
    raise SystemExit("backend telemetry schema mismatch")
if staged.get("ttsSubsegmentMaxChars") != 0:
    raise SystemExit("backend TTS cap is not zero")
if staged.get("ttsResponseChunkTelemetryEnabled") is not True:
    raise SystemExit("response-chunk telemetry is not enabled")
if staged.get("ttsIncrementalPublishEnabled", False) is not expected_incremental:
    raise SystemExit("incremental-publication flag mismatch")
if staged.get("ttsPublisherHandoffTelemetryEnabled", False) is not expected_incremental:
    raise SystemExit("publisher-handoff telemetry capability mismatch")
expected_metadata_versions = [1] if expected_incremental else []
if config.get("audioMetadataProtocolVersions") != expected_metadata_versions:
    raise SystemExit("audio metadata protocol capability mismatch")
if expected_incremental and staged.get("ttsIncrementalFrameMs") != int(sys.argv[9]):
    raise SystemExit("incremental frame duration mismatch")
reported_fallback = staged.get("ttsIncrementalAtomicFallbackMaxChars", 0)
expected_fallback = int(sys.argv[10]) if expected_incremental else 0
if reported_fallback != expected_fallback:
    raise SystemExit("incremental atomic-fallback threshold mismatch")
models = config["modelConfig"]
actual = [
    models["asr"]["image"], models["asr"]["imageDigest"],
    models["nmt"]["image"], models["nmt"]["imageDigest"],
    models["tts"]["image"], models["tts"]["imageDigest"],
]
if actual != expected[:6]:
    raise SystemExit("backend model provenance mismatch")
' \
      "$expected_schema" "$incremental" \
      "$ASR_IMAGE" "$ASR_IMAGE_DIGEST" \
      "$NMT_IMAGE" "$NMT_IMAGE_DIGEST" \
      "$TTS_IMAGE" "$TTS_IMAGE_DIGEST" \
      "$CANARY_INCREMENTAL_FRAME_MS" \
      "$CANARY_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS"

  metadata_args=()
  if [[ "$arm" == "streaming" ]]; then
    metadata_args+=(--audio-metadata-protocol-v1)
  fi
  if [[ "$CANARY_MODE" == "silence" ]]; then
    metadata_args+=(--measure-synthesized-pcm-silence)
  fi
  PYTHONPATH=".python-packages:backend:." \
    "$PYTHON_BIN" batch_latency_test.py \
      --file "$PREFIX_WAV" \
      --backend "$BACKEND_URL" \
      --output-dir "$ARM_DIR" \
      "${metadata_args[@]}"

  PYTHONPATH=".python-packages:backend:." \
    "$PYTHON_BIN" analyze_playback_policy.py \
      --input-dir "$ARM_DIR" \
      --json-output "$ARM_DIR/playback_policy_analysis.json" \
      --markdown-output "$ARM_DIR/playback_policy_analysis.md"

  # The duration model treats each atomic PCM message as a complete TTS
  # request, so retain it on the schema-1 control. The streaming-latency
  # analyzer is schema-3-aware and runs on both arms.
  if [[ "$arm" == "atomic" ]]; then
    PYTHONPATH=".python-packages:backend:." \
      "$PYTHON_BIN" analyze_tts_duration.py \
        --input-dir "$ARM_DIR" \
        --json-output "$ARM_DIR/tts_duration_model.json" \
        --markdown-output "$ARM_DIR/tts_duration_model.md"
  fi
  PYTHONPATH=".python-packages:backend:." \
    "$PYTHON_BIN" analyze_streaming_latency.py \
      "$ARM_DIR/shared-prefix_summary.json" \
      --json-output "$ARM_DIR/streaming_latency_analysis.json" \
      --markdown-output "$ARM_DIR/streaming_latency_analysis.md"

  if [[ "$arm" == "streaming" ]]; then
    PYTHONPATH=".python-packages:backend:." \
      "$PYTHON_BIN" analyze_stage_burst_attribution.py \
        "$ARM_DIR/shared-prefix_results.csv" \
        --window-seconds 30 \
        --top-window-count 5 \
        --json-output "$ARM_DIR/stage_burst_attribution.json" \
        --markdown-output "$ARM_DIR/stage_burst_attribution.md"

    PYTHONPATH=".python-packages:backend:." \
      "$PYTHON_BIN" analyze_freshness_cap.py \
        --results-csv "$ARM_DIR/shared-prefix_results.csv" \
        --summary-json "$ARM_DIR/shared-prefix_summary.json" \
        --json-output "$ARM_DIR/schema3_freshness_cap_analysis.json" \
        --markdown-output "$ARM_DIR/schema3_freshness_cap_analysis.md"
  fi

  if [[ "$CANARY_MODE" == "silence" ]]; then
    PYTHONPATH=".python-packages:backend:." \
      "$PYTHON_BIN" analyze_synthesized_silence.py \
        "$ARM_DIR/shared-prefix_summary.json" \
        --json-output "$ARM_DIR/synthesized_pcm_silence_analysis.json" \
        --markdown-output "$ARM_DIR/synthesized_pcm_silence_analysis.md"
  fi

  stop_backend
  echo "=== Completed canary arm: $arm (mode=$CANARY_MODE) ==="
done

if [[ "$CANARY_MODE" == "matched" ]]; then
  PYTHONPATH=".python-packages:backend:." \
    "$PYTHON_BIN" summarize_streaming_tts_canary.py \
      --input-dir "$RUN_DIR" \
      --json-output "$RUN_DIR/streaming_tts_canary_comparison.json" \
      --markdown-output "$RUN_DIR/streaming_tts_canary_comparison.md"
fi

trap - EXIT INT TERM
echo
echo "Streaming TTS canary complete (mode=$CANARY_MODE): $RUN_DIR"
