#!/usr/bin/env bash
set -euo pipefail

# Run matched real-time post-NMT TTS-splitting canaries against one source
# prefix. The script manages only its own FastAPI process. It never starts,
# stops, or mutates the Riva NIM containers.

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
CANARY_DURATION_SECONDS="${CANARY_DURATION_SECONDS:-300}"
CANARY_CAPS="${CANARY_CAPS:-0 40 45 60}"
CANARY_MIN_CHARS="${CANARY_MIN_CHARS:-12}"
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

if [[ ! -f "$CANARY_SOURCE" ]]; then
  echo "Canary source does not exist: $CANARY_SOURCE" >&2
  exit 2
fi
if ! [[ "$CANARY_DURATION_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "CANARY_DURATION_SECONDS must be a positive integer" >&2
  exit 2
fi
if ! [[ "$CANARY_MIN_CHARS" =~ ^[1-9][0-9]*$ ]]; then
  echo "CANARY_MIN_CHARS must be a positive integer" >&2
  exit 2
fi
if ! [[ "$CANARY_BACKEND_PORT" =~ ^[1-9][0-9]*$ ]]; then
  echo "CANARY_BACKEND_PORT must be a positive integer" >&2
  exit 2
fi
for port_setting in \
  "ASR_HTTP_PORT:$ASR_HTTP_PORT" \
  "NMT_HTTP_PORT:$NMT_HTTP_PORT" \
  "TTS_HTTP_PORT:$TTS_HTTP_PORT"; do
  port_name="${port_setting%%:*}"
  port_value="${port_setting#*:}"
  if ! [[ "$port_value" =~ ^[1-9][0-9]*$ ]]; then
    echo "$port_name must be a positive integer" >&2
    exit 2
  fi
done
for timeout_setting in \
  "CANARY_CURL_CONNECT_TIMEOUT_SECONDS:$CANARY_CURL_CONNECT_TIMEOUT_SECONDS" \
  "CANARY_CURL_MAX_TIME_SECONDS:$CANARY_CURL_MAX_TIME_SECONDS" \
  "CANARY_BACKEND_STOP_TIMEOUT_SECONDS:$CANARY_BACKEND_STOP_TIMEOUT_SECONDS"; do
  timeout_name="${timeout_setting%%:*}"
  timeout_value="${timeout_setting#*:}"
  if ! [[ "$timeout_value" =~ ^[1-9][0-9]*$ ]]; then
    echo "$timeout_name must be a positive integer" >&2
    exit 2
  fi
done

if [[ "$ALLOW_DIRTY_CANARY" != "0" && "$ALLOW_DIRTY_CANARY" != "1" ]]; then
  echo "ALLOW_DIRTY_CANARY must be 0 or 1" >&2
  exit 2
fi

EVIDENCE_CLASS="formal"
if [[ "$ALLOW_DIRTY_CANARY" == "1" ]]; then
  EVIDENCE_CLASS="non-formal-probe"
  echo \
    "NON-FORMAL PROBE: ALLOW_DIRTY_CANARY=1 bypasses clean/ignored-output evidence gates." \
    >&2
elif [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing an evidence run from a dirty worktree." >&2
  echo "Commit the implementation first, or set ALLOW_DIRTY_CANARY=1 for a non-formal probe." >&2
  exit 2
fi

CANARY_OUTPUT_ROOT_RESOLVED="$(realpath -m -- "$CANARY_OUTPUT_ROOT")"
case "$CANARY_OUTPUT_ROOT_RESOLVED" in
  "$REPOSITORY_ROOT"/*) ;;
  *)
    echo "CANARY_OUTPUT_ROOT must resolve inside the repository." >&2
    exit 2
    ;;
esac
CANARY_OUTPUT_ROOT_RELATIVE="${CANARY_OUTPUT_ROOT_RESOLVED#"$REPOSITORY_ROOT"/}"
if [[ "$EVIDENCE_CLASS" == "formal" ]] &&
  ! git check-ignore --quiet -- "$CANARY_OUTPUT_ROOT_RELATIVE"; then
  echo "Formal evidence requires CANARY_OUTPUT_ROOT to be ignored by Git." >&2
  echo "Use an ignored in-repository path or ALLOW_DIRTY_CANARY=1 for a non-formal probe." >&2
  exit 2
fi
mkdir -p -- "$CANARY_OUTPUT_ROOT_RESOLVED"
CANARY_OUTPUT_ROOT_REAL="$(realpath -e -- "$CANARY_OUTPUT_ROOT_RESOLVED")"
if [[ "$CANARY_OUTPUT_ROOT_REAL" != "$CANARY_OUTPUT_ROOT_RESOLVED" ]]; then
  echo "CANARY_OUTPUT_ROOT changed while being prepared; refusing an unsafe output path." >&2
  exit 2
fi
CANARY_OUTPUT_ROOT="$CANARY_OUTPUT_ROOT_REAL"

validate_pinned_image() {
  local service_name="$1"
  local image_reference="$2"
  local image_leaf="${image_reference##*/}"

  if [[ "$image_reference" == *[$' \t\r\n']* ]] ||
    [[ "$image_leaf" != *:* ]] ||
    [[ "$image_leaf" == *:latest ]]; then
    echo "$service_name expected image must use an explicit, non-latest tag." >&2
    return 1
  fi
}

resolve_running_image_digest() {
  local service_name="$1"
  local host_port="$2"
  local expected_image="$3"
  local configured_digest="$4"
  local running_container_ids_text=""
  local container_id=""
  local ports_json=""
  local port_match_status=0
  local actual_image=""
  local image_id=""
  local repo_digests_json=""
  local expected_repository="${expected_image%:*}"
  local actual_repo_digest=""
  local actual_digest_only=""
  local -a matching_container_ids=()

  if ! running_container_ids_text="$(
    docker ps --filter status=running --quiet
  )"; then
    echo "Unable to enumerate running containers for $service_name provenance." >&2
    return 1
  fi

  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    if ! ports_json="$(
      docker inspect --format '{{json .NetworkSettings.Ports}}' "$container_id"
    )"; then
      echo "Unable to inspect published ports for $service_name provenance." >&2
      return 1
    fi
    if "$PYTHON_BIN" -c '
import json
import sys

try:
    ports = json.loads(sys.argv[1]) or {}
    host_port = sys.argv[2]
    matched = any(
        binding.get("HostPort") == host_port
        for bindings in ports.values()
        for binding in (bindings or ())
        if isinstance(binding, dict)
    )
except (AttributeError, TypeError, ValueError):
    raise SystemExit(2)
raise SystemExit(0 if matched else 1)
' "$ports_json" "$host_port"; then
      matching_container_ids+=("$container_id")
    else
      port_match_status=$?
      if [[ "$port_match_status" != "1" ]]; then
        echo "Malformed Docker port metadata while resolving $service_name provenance." >&2
        return 1
      fi
    fi
  done <<<"$running_container_ids_text"

  if [[ "${#matching_container_ids[@]}" != "1" ]]; then
    echo \
      "$service_name provenance requires exactly one running container publishing HTTP port $host_port; found ${#matching_container_ids[@]}." \
      >&2
    return 1
  fi
  container_id="${matching_container_ids[0]}"

  if ! actual_image="$(
    docker inspect --format '{{.Config.Image}}' "$container_id"
  )"; then
    echo "Unable to inspect the running $service_name image reference." >&2
    return 1
  fi
  if [[ "$actual_image" != "$expected_image" ]]; then
    echo "Running $service_name image does not match its configured pinned tag." >&2
    return 1
  fi

  if ! image_id="$(docker inspect --format '{{.Image}}' "$container_id")"; then
    echo "Unable to inspect the running $service_name immutable image ID." >&2
    return 1
  fi
  if ! repo_digests_json="$(
    docker image inspect --format '{{json .RepoDigests}}' "$image_id"
  )"; then
    echo "Unable to inspect the running $service_name repository digest." >&2
    return 1
  fi
  if ! actual_repo_digest="$(
    "$PYTHON_BIN" -c '
import json
import re
import sys

try:
    digests = json.loads(sys.argv[1]) or []
    repository = sys.argv[2]
    matches = sorted({
        value
        for value in digests
        if isinstance(value, str)
        and re.fullmatch(
            re.escape(repository) + r"@sha256:[0-9a-f]{64}",
            value,
        )
    })
except (TypeError, ValueError):
    raise SystemExit(2)
if len(matches) != 1:
    raise SystemExit(1)
print(matches[0])
' "$repo_digests_json" "$expected_repository"
  )"; then
    echo "Running $service_name image lacks one unambiguous RepoDigest." >&2
    return 1
  fi

  actual_digest_only="${actual_repo_digest#*@}"
  if [[ -n "$configured_digest" ]] &&
    [[ "$configured_digest" != "$actual_repo_digest" ]] &&
    [[ "$configured_digest" != "$actual_digest_only" ]]; then
    echo "Configured $service_name image digest does not match the running image." >&2
    return 1
  fi

  printf '%s\n' "$actual_digest_only"
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

for ready_service in \
  "ASR:$ASR_HTTP_PORT" \
  "NMT:$NMT_HTTP_PORT" \
  "TTS:$TTS_HTTP_PORT"; do
  ready_name="${ready_service%%:*}"
  ready_port="${ready_service#*:}"
  echo "Checking ${ready_name} readiness on configured HTTP port ${ready_port}"
  curl --fail --silent --show-error \
    --connect-timeout "$CANARY_CURL_CONNECT_TIMEOUT_SECONDS" \
    --max-time "$CANARY_CURL_MAX_TIME_SECONDS" \
    "http://127.0.0.1:${ready_port}/v1/health/ready" >/dev/null
done

FFMPEG_BIN="$(command -v ffmpeg || true)"
if [[ -z "$FFMPEG_BIN" ]]; then
  FFMPEG_BIN="$(
    PYTHONPATH=".python-packages:backend:." \
      "$PYTHON_BIN" -c \
        'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'
  )"
fi
if [[ ! -x "$FFMPEG_BIN" ]]; then
  echo "ffmpeg is required to create the shared canary prefix" >&2
  exit 2
fi

SHORT_COMMIT="$(git rev-parse --short HEAD)"
RUN_ID="tts-subsegment-canary-$(date -u +%Y%m%dT%H%M%SZ)-${SHORT_COMMIT}"
RUN_DIR="${CANARY_OUTPUT_ROOT%/}/${RUN_ID}"
PREFIX_WAV="$RUN_DIR/shared-prefix.wav"
if ! mkdir -- "$RUN_DIR"; then
  echo "Refusing to reuse or mix an existing canary run directory." >&2
  exit 2
fi
export MPLCONFIGDIR="${MPLCONFIGDIR:-$RUN_DIR/.matplotlib}"
mkdir -p "$MPLCONFIGDIR"

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
  echo "source=$CANARY_SOURCE"
  echo "duration_seconds=$CANARY_DURATION_SECONDS"
  echo "prefix_sha256=$PREFIX_SHA256"
  echo "caps=$CANARY_CAPS"
  echo "min_chars=$CANARY_MIN_CHARS"
  echo "backend_port=$CANARY_BACKEND_PORT"
  echo "asr_http_port=$ASR_HTTP_PORT"
  echo "nmt_http_port=$NMT_HTTP_PORT"
  echo "tts_http_port=$TTS_HTTP_PORT"
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
  local waited_seconds=0

  # Clear first so repeated cleanup paths can never signal an unrelated PID.
  BACKEND_PID=""
  if [[ -z "$owned_pid" ]]; then
    return
  fi
  if ! kill -0 "$owned_pid" 2>/dev/null; then
    wait "$owned_pid" 2>/dev/null || true
    return
  fi

  kill -TERM "$owned_pid" 2>/dev/null || true
  while kill -0 "$owned_pid" 2>/dev/null; do
    if (( waited_seconds >= CANARY_BACKEND_STOP_TIMEOUT_SECONDS )); then
      echo \
        "FastAPI PID $owned_pid did not stop after ${CANARY_BACKEND_STOP_TIMEOUT_SECONDS}s; sending SIGKILL" \
        >&2
      kill -KILL "$owned_pid" 2>/dev/null || true
      break
    fi
    sleep 1
    ((waited_seconds += 1))
  done
  wait "$owned_pid" 2>/dev/null || true
}
cleanup_on_exit() {
  local exit_status=$?

  trap - EXIT
  stop_backend
  exit "$exit_status"
}
handle_signal() {
  local signal_name="$1"
  local exit_status="$2"

  trap - INT TERM
  echo "Received $signal_name; stopping the script-owned FastAPI process" >&2
  stop_backend
  exit "$exit_status"
}
trap cleanup_on_exit EXIT
trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM

curl_local() {
  curl \
    --connect-timeout "$CANARY_CURL_CONNECT_TIMEOUT_SECONDS" \
    --max-time "$CANARY_CURL_MAX_TIME_SECONDS" \
    "$@"
}

for cap in $CANARY_CAPS; do
  if ! [[ "$cap" =~ ^[0-9]+$ ]]; then
    echo "Every CANARY_CAPS value must be a non-negative integer: $cap" >&2
    exit 2
  fi

  ARM_NAME="cap-${cap}"
  ARM_DIR="$RUN_DIR/$ARM_NAME"
  mkdir -p "$ARM_DIR"
  BACKEND_LOG="$ARM_DIR/backend.log"

  echo
  echo "=== Starting matched canary arm: $ARM_NAME ==="
  S2S_PIPELINE_MODE=staged \
  STAGED_TTS_SUBSEGMENT_MAX_CHARS="$cap" \
  STAGED_TTS_SUBSEGMENT_MIN_CHARS="$CANARY_MIN_CHARS" \
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
      echo "FastAPI exited while starting $ARM_NAME" >&2
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
    echo "FastAPI was not ready within 180 seconds for $ARM_NAME" >&2
    tail -80 "$BACKEND_LOG" >&2 || true
    exit 1
  fi

  CONFIG_REPORT="$(
    curl_local --fail --silent "$BACKEND_URL/api/config" |
      "$PYTHON_BIN" -c \
        'import json,sys
config = json.load(sys.stdin)
staged = config["stagedConfig"]
models = config["modelConfig"]
print(
    staged["ttsSubsegmentMaxChars"],
    models["asr"]["image"],
    models["asr"]["imageDigest"],
    models["nmt"]["image"],
    models["nmt"]["imageDigest"],
    models["tts"]["image"],
    models["tts"]["imageDigest"],
    sep="\t",
)'
  )"
  IFS=$'\t' read -r \
    CONFIG_CAP \
    CONFIG_ASR_IMAGE CONFIG_ASR_DIGEST \
    CONFIG_NMT_IMAGE CONFIG_NMT_DIGEST \
    CONFIG_TTS_IMAGE CONFIG_TTS_DIGEST \
    <<<"$CONFIG_REPORT"
  if [[ "$CONFIG_CAP" != "$cap" ]]; then
    echo "Backend cap mismatch: requested $cap, reported $CONFIG_CAP" >&2
    exit 1
  fi
  if [[ "$CONFIG_ASR_IMAGE" != "$ASR_IMAGE" ]] ||
    [[ "$CONFIG_ASR_DIGEST" != "$ASR_IMAGE_DIGEST" ]] ||
    [[ "$CONFIG_NMT_IMAGE" != "$NMT_IMAGE" ]] ||
    [[ "$CONFIG_NMT_DIGEST" != "$NMT_IMAGE_DIGEST" ]] ||
    [[ "$CONFIG_TTS_IMAGE" != "$TTS_IMAGE" ]] ||
    [[ "$CONFIG_TTS_DIGEST" != "$TTS_IMAGE_DIGEST" ]]; then
    echo "Backend model provenance does not match the inspected running images." >&2
    exit 1
  fi

  PYTHONPATH=".python-packages:backend:." \
    "$PYTHON_BIN" batch_latency_test.py \
      --file "$PREFIX_WAV" \
      --backend "$BACKEND_URL" \
      --output-dir "$ARM_DIR"

  PYTHONPATH=".python-packages:backend:." \
    "$PYTHON_BIN" analyze_tts_duration.py \
      --input-dir "$ARM_DIR" \
      --json-output "$ARM_DIR/tts_duration_model.json" \
      --markdown-output "$ARM_DIR/tts_duration_model.md"

  PYTHONPATH=".python-packages:backend:." \
    "$PYTHON_BIN" analyze_playback_policy.py \
      --input-dir "$ARM_DIR" \
      --json-output "$ARM_DIR/playback_policy_analysis.json" \
      --markdown-output "$ARM_DIR/playback_policy_analysis.md"

  stop_backend
  echo "=== Completed matched canary arm: $ARM_NAME ==="
done

PYTHONPATH=".python-packages:backend:." \
  "$PYTHON_BIN" summarize_tts_subsegment_canary.py \
    --input-dir "$RUN_DIR" \
    --json-output "$RUN_DIR/canary_comparison.json" \
    --markdown-output "$RUN_DIR/canary_comparison.md"

trap - EXIT INT TERM
echo
echo "Matched canary complete: $RUN_DIR"
