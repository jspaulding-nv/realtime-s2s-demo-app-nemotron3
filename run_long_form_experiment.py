#!/usr/bin/env python3
"""Run the three long-form samples and compare fixed/adaptive playback.

Each sample is sent through the live Riva pipeline once per repeat. The
resulting translated-audio arrival trace is then replayed through both the
fixed 1.00x and adaptive playback policies. Playback is downstream of Riva, so
this produces a matched policy comparison without duplicating inference runs.

The derived playback schedules are deterministic simulations, not browser
Web Audio executions or native-listener quality measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import fcntl
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import requests

from analyze_playback_policy import build_analysis, render_markdown
from batch_latency_test import (
    LONG_FORM_FILES,
    PREFLIGHT_FILE,
    TestResult,
    generate_csv,
    generate_plot,
    generate_summary,
    run_test,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent
DEFAULT_BACKEND = "http://localhost:8000"
DEFAULT_OUTPUT_ROOT = Path("experiment_results")
ANALYSIS_JSON = "playback_policy_analysis.json"
ANALYSIS_MARKDOWN = "playback_policy_analysis.md"


class ExperimentError(RuntimeError):
    """An operational failure that invalidates an experiment capture."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def git_metadata() -> dict[str, Any]:
    commit = _run_git("rev-parse", "HEAD")
    short_commit = _run_git("rev-parse", "--short", "HEAD")
    status = _run_git("status", "--porcelain")
    return {
        "commit": commit,
        "short_commit": short_commit,
        "dirty": bool(status) if status is not None else None,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sample_slug(path: Path) -> str:
    name = path.stem.lower()
    for index in range(1, 4):
        neutral_ids = (
            f"long-form-{index:02d}",
            f"long_form_{index:02d}",
            f"sample-{index:02d}",
            f"sample_{index:02d}",
        )
        if any(identifier in name for identifier in neutral_ids):
            return f"sample_{index:02d}"
    return "".join(character if character.isalnum() else "-" for character in name).strip("-")


def make_run_id(now: datetime | None = None, short_commit: str | None = None) -> str:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{short_commit or 'unknown'}"


def _relative_to_repository(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(path.resolve())


def build_manifest(
    *,
    run_id: str,
    run_dir: Path,
    backend_url: str,
    repeats: int,
    skip_preflight: bool,
    files: Sequence[Path],
    include_hashes: bool = True,
) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")

    samples = []
    runs = []
    sample_order: dict[str, int] = {}
    for file_index, path in enumerate(files):
        resolved = path.resolve()
        if not resolved.is_file():
            raise ExperimentError(f"sample file not found: {path}")
        slug = sample_slug(resolved)
        sample_order[slug] = file_index
        samples.append(
            {
                "slug": slug,
                "audio_path": _relative_to_repository(resolved),
                "size_bytes": resolved.stat().st_size,
                "sha256": sha256_file(resolved) if include_hashes else None,
            }
        )
        for repeat in range(1, repeats + 1):
            artifact_dir = Path(f"repeat-{repeat:02d}")
            stem = resolved.stem
            runs.append(
                {
                    "repeat": repeat,
                    "sample": slug,
                    "audio_path": _relative_to_repository(resolved),
                    "status": "pending",
                    "artifact_dir": str(artifact_dir),
                    "csv": str(artifact_dir / f"{stem}_results.csv"),
                    "summary": str(artifact_dir / f"{stem}_summary.json"),
                    "plot": str(artifact_dir / f"{stem}_latency.png"),
                    "started_at_utc": None,
                    "completed_at_utc": None,
                    "error": None,
                }
            )

    return {
        "schema_version": 1,
        "run_id": run_id,
        "run_directory": str(run_dir.resolve()),
        "status": "planned",
        "created_at_utc": utc_now(),
        "updated_at_utc": utc_now(),
        "completed_at_utc": None,
        "backend_url": backend_url.rstrip("/"),
        # Filled atomically with the first successful backend readiness check.
        # Resumes and every capture summary must match this frozen snapshot.
        "pipeline_provenance": None,
        "requested_repeats": repeats,
        "git": git_metadata(),
        "preflight": {
            "required": not skip_preflight,
            "status": "skipped" if skip_preflight else "pending",
            "audio_path": PREFLIGHT_FILE,
            "artifact_dir": "preflight",
            "summary": f"preflight/{Path(PREFLIGHT_FILE).stem}_summary.json",
            "csv": f"preflight/{Path(PREFLIGHT_FILE).stem}_results.csv",
            "plot": f"preflight/{Path(PREFLIGHT_FILE).stem}_latency.png",
            "error": None,
        },
        "samples": samples,
        "runs": sorted(
            runs,
            key=lambda item: (
                item["repeat"],
                sample_order[item["sample"]],
            ),
        ),
        "analysis": {
            "status": "pending",
            "json": ANALYSIS_JSON,
            "markdown": ANALYSIS_MARKDOWN,
            "all_candidate_gates_pass": None,
            "error": None,
        },
        "provenance": {
            "live_riva_trace_per_sample_repeat": True,
            "fixed_and_adaptive_use_identical_arrival_trace": True,
            "playback_results_are_deterministic_simulations": True,
            "browser_web_audio_executed": False,
            "native_listener_quality_measured": False,
            "joke_or_marked_phrase_semantic_delay_measured": False,
            "containers_managed_by_harness": False,
        },
    }


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest["updated_at_utc"] = utc_now()
    destination = run_dir / "manifest.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "manifest.json"
    if not path.is_file():
        raise ExperimentError(f"resume manifest not found: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"invalid resume manifest: {path}: {exc}") from exc
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("runs"), list):
        raise ExperimentError(f"unsupported resume manifest: {path}")
    return manifest


def validate_resume_provenance(manifest: dict[str, Any]) -> None:
    original_git = manifest.get("git", {})
    current_git = git_metadata()
    if original_git.get("dirty") is True:
        raise ExperimentError(
            "the original run used a dirty worktree, so exact resume provenance "
            "cannot be verified; start a new run from a committed revision"
        )
    if current_git.get("dirty") is True:
        raise ExperimentError(
            "the current worktree is dirty; commit or clean it before resuming"
        )
    if (
        original_git.get("commit")
        and current_git.get("commit")
        and original_git["commit"] != current_git["commit"]
    ):
        raise ExperimentError(
            "resume git commit differs from the original run: "
            f"{original_git['commit']} != {current_git['commit']}"
        )

    for sample in manifest.get("samples", []):
        path = Path(sample["audio_path"])
        if not path.is_absolute():
            path = REPOSITORY_ROOT / path
        if not path.is_file():
            raise ExperimentError(f"resume sample file is missing: {path}")
        if path.stat().st_size != int(sample["size_bytes"]):
            raise ExperimentError(f"resume sample size differs: {path}")
        if sha256_file(path) != sample["sha256"]:
            raise ExperimentError(f"resume sample SHA-256 differs: {path}")


@contextmanager
def experiment_lock(backend_url: str):
    """Prevent concurrent local harnesses from sharing the single-session backend."""

    lock_key = hashlib.sha256(backend_url.rstrip("/").encode("utf-8")).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"s2s-eval-s2s-{lock_key}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExperimentError(
                f"another experiment is already using {backend_url} ({lock_path})"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} backend={backend_url}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validated_model_config(
    value: Any,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(value, dict):
        return None, "modelConfig is missing or invalid"
    required_fields = {
        "asr": (
            "endpoint",
            "image",
            "imageDigest",
            "profile",
            "eouMs",
            "wordTimeOffsets",
            "sourceLanguage",
        ),
        "nmt": (
            "endpoint",
            "image",
            "imageDigest",
            "profile",
            "model",
            "sourceLanguage",
            "targetLanguage",
        ),
        "tts": (
            "endpoint",
            "image",
            "imageDigest",
            "profile",
            "targetLanguage",
            "voice",
        ),
    }
    for stage, fields in required_fields.items():
        stage_config = value.get(stage)
        if not isinstance(stage_config, dict):
            return None, f"modelConfig.{stage} is missing or invalid"
        missing = [field for field in fields if field not in stage_config]
        if missing:
            return None, (
                f"modelConfig.{stage} is missing fields: "
                + ", ".join(missing)
            )
        for field in ("endpoint", "image"):
            field_value = stage_config[field]
            if not isinstance(field_value, str) or not field_value.strip():
                return None, f"modelConfig.{stage}.{field} is invalid"
        for field in ("imageDigest", "profile"):
            field_value = stage_config[field]
            if field_value is not None and (
                not isinstance(field_value, str) or not field_value.strip()
            ):
                return None, f"modelConfig.{stage}.{field} is invalid"

    asr = value["asr"]
    nmt = value["nmt"]
    tts = value["tts"]
    if (
        not isinstance(asr["eouMs"], int)
        or isinstance(asr["eouMs"], bool)
        or asr["eouMs"] <= 0
    ):
        return None, "modelConfig.asr.eouMs is invalid"
    if not isinstance(asr["wordTimeOffsets"], bool):
        return None, "modelConfig.asr.wordTimeOffsets is invalid"
    for stage, field in (
        (asr, "sourceLanguage"),
        (nmt, "model"),
        (nmt, "sourceLanguage"),
        (nmt, "targetLanguage"),
        (tts, "targetLanguage"),
        (tts, "voice"),
    ):
        if not isinstance(stage[field], str) or not stage[field].strip():
            return None, f"modelConfig field {field} is invalid"
    if asr["sourceLanguage"] != nmt["sourceLanguage"]:
        return None, "modelConfig source languages disagree"
    if nmt["targetLanguage"] != tts["targetLanguage"]:
        return None, "modelConfig target languages disagree"
    return json.loads(json.dumps(value)), "ok"


def _summary_pipeline_provenance(summary: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    mode = summary.get("pipeline_mode", "monolithic")
    if mode not in {"monolithic", "staged"}:
        return None, f"summary has invalid pipeline mode: {mode!r}"
    backend_config = summary.get("backend_config")
    if not isinstance(backend_config, dict):
        return None, "summary backend_config is missing or invalid"
    config_mode = backend_config.get("pipelineMode")
    if config_mode != mode:
        return None, (
            "summary pipeline mode/config mismatch: "
            f"pipeline_mode={mode!r}, backend_config={config_mode!r}"
        )
    model_config, reason = _validated_model_config(
        backend_config.get("modelConfig")
    )
    if model_config is None:
        return None, f"summary {reason}"
    target_language = summary.get("target_language")
    expected_target = model_config["nmt"]["targetLanguage"]
    if target_language != expected_target:
        return None, (
            "summary target language/config mismatch: "
            f"target_language={target_language!r}, configured={expected_target!r}"
        )
    staged_config = None
    if mode == "staged":
        if not isinstance(backend_config, dict):
            return None, "staged summary backend_config is missing or invalid"
        staged_config = backend_config.get("stagedConfig")
        if not isinstance(staged_config, dict):
            return None, "staged summary stagedConfig is missing or invalid"
    return {
        "pipeline_mode": mode,
        "stagedConfig": staged_config,
        "modelConfig": model_config,
    }, "ok"


def validate_summary(
    path: Path,
    *,
    expected_pipeline: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"missing summary: {path}"
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid summary {path}: {exc}"

    try:
        chunks_sent = int(summary.get("chunks_sent", 0))
        audio_responses = int(summary.get("audio_responses", 0))
        total_received_bytes = int(summary.get("total_received_bytes", 0))
    except (TypeError, ValueError):
        return False, "capture counters are invalid"

    input_end_timestamp_ms = summary.get("input_end_timestamp_ms")
    terminal_timestamp_ms = summary.get("terminal_arrival_timestamp_ms")
    terminal_lag_sec = summary.get("terminal_arrival_lag_sec")
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        for value in (
            input_end_timestamp_ms,
            terminal_timestamp_ms,
            terminal_lag_sec,
        )
    ):
        return False, "terminal timing evidence is missing or invalid"
    if input_end_timestamp_ms <= 0 or terminal_timestamp_ms <= 0:
        return False, "terminal timing timestamps must be positive"
    if terminal_timestamp_ms < input_end_timestamp_ms:
        return False, "completed terminal arrived before end_input"
    expected_terminal_lag = (
        terminal_timestamp_ms - input_end_timestamp_ms
    ) / 1000
    if not math.isclose(
        terminal_lag_sec,
        expected_terminal_lag,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        return False, "terminal arrival lag is inconsistent with timestamps"

    pipeline_provenance, reason = _summary_pipeline_provenance(summary)
    if pipeline_provenance is None:
        return False, reason

    staged_integrity = summary.get("staged_integrity")
    if staged_integrity is not None:
        if not isinstance(staged_integrity, dict):
            return False, "staged pipeline integrity result is invalid"
        staged_errors = staged_integrity.get("errors")
        if not isinstance(staged_errors, list):
            return False, "staged pipeline integrity errors are invalid"
        if staged_errors:
            return False, (
                "staged pipeline integrity failed: "
                + "; ".join(str(error) for error in staged_errors)
            )
        if summary.get("pipeline_mode") == "staged":
            if staged_integrity.get("applicable") is not True:
                return False, "staged pipeline integrity was not applied"
            if staged_integrity.get("passed") is not True:
                return False, "staged pipeline integrity did not pass"
    elif summary.get("pipeline_mode") == "staged":
        return False, "staged pipeline integrity result is missing"

    if pipeline_provenance["pipeline_mode"] == "staged":
        if not isinstance(summary.get("staged_pipeline"), dict):
            return False, "staged pipeline raw evidence is missing or invalid"
        if not isinstance(summary.get("websocket_receive_events"), list):
            return False, "staged WebSocket receive evidence is missing or invalid"

    if expected_pipeline is not None and pipeline_provenance != expected_pipeline:
        return False, (
            "capture pipeline provenance differs from manifest: "
            f"expected {expected_pipeline!r}, got {pipeline_provenance!r}"
        )

    checks = (
        (summary.get("input_completed") is True, "input did not complete"),
        (summary.get("connection_lost") is False, "WebSocket connection was lost"),
        (summary.get("drain_timed_out") is False, "translated tail drain timed out"),
        (summary.get("translation_completed") is True, "Riva did not confirm completion"),
        (not summary.get("server_error"), "backend reported an error"),
        (chunks_sent > 0, "no source chunks were sent"),
        (audio_responses > 0, "no translated audio responses"),
        (total_received_bytes > 0, "translated audio was empty"),
    )
    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, "ok"


def _artifact_hashes(run_dir: Path, entry: dict[str, Any]) -> dict[str, str]:
    return {
        key: sha256_file(run_dir / entry[key])
        for key in ("csv", "summary", "plot")
    }


def validate_artifact_set(
    csv_path: Path,
    summary_path: Path,
    plot_path: Path,
    *,
    expected_hashes: dict[str, str] | None = None,
    expected_pipeline: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    summary_valid, reason = validate_summary(
        summary_path,
        expected_pipeline=expected_pipeline,
    )
    if not summary_valid:
        return False, reason
    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        return False, f"missing or empty event CSV: {csv_path}"
    if not plot_path.is_file() or plot_path.stat().st_size == 0:
        return False, f"missing or empty latency plot: {plot_path}"

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        sent_count = 0
        received_count = 0
        received_bytes = 0
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            required = {"source", "stage", "audio_bytes"}
            missing = required.difference(reader.fieldnames or ())
            if missing:
                return False, (
                    "event CSV missing columns: " + ", ".join(sorted(missing))
                )
            for row in reader:
                if row["source"] != "client":
                    continue
                if row["stage"] == "chunk_sent":
                    sent_count += 1
                elif row["stage"] == "audio_received":
                    received_count += 1
                    received_bytes += int(row["audio_bytes"])
    except (OSError, csv.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        return False, f"invalid capture artifacts: {exc}"

    expected_counts = {
        "chunks_sent": sent_count,
        "audio_responses": received_count,
        "total_received_bytes": received_bytes,
    }
    for field, observed in expected_counts.items():
        try:
            recorded = int(summary[field])
        except (KeyError, TypeError, ValueError):
            return False, f"summary has invalid {field}"
        if observed != recorded:
            return False, (
                f"CSV/summary {field} mismatch: CSV={observed}, summary={recorded}"
            )

    if expected_hashes:
        actual_hashes = {
            "csv": sha256_file(csv_path),
            "summary": sha256_file(summary_path),
            "plot": sha256_file(plot_path),
        }
        for key, expected in expected_hashes.items():
            if actual_hashes.get(key) != expected:
                return False, f"{key} artifact hash differs from manifest"
    return True, "ok"


def capture_artifacts_valid(
    run_dir: Path,
    entry: dict[str, Any],
    *,
    expected_pipeline: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    return validate_artifact_set(
        run_dir / entry["csv"],
        run_dir / entry["summary"],
        run_dir / entry["plot"],
        expected_hashes=entry.get("artifact_sha256"),
        expected_pipeline=expected_pipeline,
    )


def check_backend_ready(backend_url: str) -> dict[str, Any]:
    root_url = f"{backend_url.rstrip('/')}/"
    try:
        response = requests.get(root_url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise ExperimentError(f"backend readiness check failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExperimentError("backend readiness response must be a JSON object")
    if payload.get("status") != "ok":
        raise ExperimentError(f"backend returned unexpected status: {payload}")

    pipeline_mode = payload.get("pipeline_mode", "monolithic")
    if pipeline_mode not in {"monolithic", "staged"}:
        raise ExperimentError(
            f"backend returned invalid pipeline mode: {pipeline_mode!r}"
        )

    if pipeline_mode == "monolithic" and payload.get("riva_connected") is not True:
        raise ExperimentError(
            "backend is reachable but not connected to Riva; start and verify "
            "ASR, NMT, TTS, and the FastAPI backend before retrying"
        )

    config_url = f"{backend_url.rstrip('/')}/api/config"
    try:
        config_response = requests.get(config_url, timeout=10)
        config_response.raise_for_status()
        config = config_response.json()
    except Exception as exc:
        raise ExperimentError(
            f"backend configuration check failed: {exc}"
        ) from exc
    if not isinstance(config, dict) or config.get("pipelineMode") != pipeline_mode:
        raise ExperimentError(
            "backend readiness/config mode mismatch: "
            f"root={pipeline_mode!r}, config={config!r}"
        )
    model_config, reason = _validated_model_config(config.get("modelConfig"))
    if model_config is None:
        raise ExperimentError(f"backend configuration {reason}")
    payload = dict(payload)
    payload["config"] = config

    return payload


def _pipeline_provenance_from_readiness(
    readiness: dict[str, Any],
) -> dict[str, Any]:
    mode = readiness.get("pipeline_mode", "monolithic")
    if mode not in {"monolithic", "staged"}:
        raise ExperimentError(
            f"backend returned invalid pipeline mode: {mode!r}"
        )
    staged_config = None
    config = readiness.get("config")
    if not isinstance(config, dict):
        raise ExperimentError("backend readiness config is missing")
    if mode == "staged":
        staged_config = config.get("stagedConfig")
        if not isinstance(staged_config, dict):
            raise ExperimentError("staged backend readiness stagedConfig is missing")
    model_config, reason = _validated_model_config(config.get("modelConfig"))
    if model_config is None:
        raise ExperimentError(f"backend readiness {reason}")
    return {
        "pipeline_mode": mode,
        # Round-trip to detach the immutable manifest snapshot from any
        # mutable response/test object retained elsewhere.
        "stagedConfig": json.loads(json.dumps(staged_config)),
        "modelConfig": model_config,
    }


def freeze_or_validate_pipeline_provenance(
    manifest: dict[str, Any],
    readiness: dict[str, Any],
) -> dict[str, Any]:
    observed = _pipeline_provenance_from_readiness(readiness)
    frozen = manifest.get("pipeline_provenance")
    if frozen is None:
        if manifest.get("backend_readiness") is not None:
            raise ExperimentError(
                "resume manifest has backend readiness evidence but no frozen "
                "pipeline provenance; start a new run to prevent mixed captures"
            )
        manifest["pipeline_provenance"] = observed
        return observed
    if frozen != observed:
        raise ExperimentError(
            "backend pipeline provenance differs from the frozen manifest: "
            f"expected {frozen!r}, got {observed!r}"
        )
    return frozen


def validate_result(result: TestResult) -> None:
    failures = []
    if not result.input_completed:
        failures.append("input did not complete")
    if result.connection_lost:
        failures.append("WebSocket connection was lost")
    if result.drain_timed_out:
        failures.append("translated tail drain timed out")
    if not result.translation_completed:
        failures.append("Riva did not confirm translated-stream completion")
    if result.server_error:
        failures.append(f"backend error: {result.server_error}")
    if result.audio_responses <= 0 or result.total_received_bytes <= 0:
        failures.append("no translated audio was received")
    if result.translation_completed:
        if result.input_end_timestamp_ms <= 0:
            failures.append("input end timestamp is missing")
        if result.terminal_arrival_timestamp_ms <= 0:
            failures.append("terminal arrival timestamp is missing")
        elif result.terminal_arrival_timestamp_ms < result.input_end_timestamp_ms:
            failures.append("completed terminal arrived before end_input")
        expected_lag = max(
            0.0,
            (
                result.terminal_arrival_timestamp_ms
                - result.input_end_timestamp_ms
            )
            / 1000,
        )
        if not math.isclose(
            result.terminal_arrival_lag_sec,
            expected_lag,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            failures.append("terminal arrival lag is inconsistent with timestamps")
    if result.staged_integrity_errors:
        failures.append(
            "staged pipeline integrity failed: "
            + "; ".join(str(error) for error in result.staged_integrity_errors)
        )
    if failures:
        raise ExperimentError("; ".join(failures))


async def capture_one(audio_path: Path, backend_url: str, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = audio_path.stem
    csv_path = output_dir / f"{stem}_results.csv"
    summary_path = output_dir / f"{stem}_summary.json"
    plot_path = output_dir / f"{stem}_latency.png"

    try:
        result = await run_test(str(audio_path), backend_url)
    except Exception:
        try:
            requests.post(f"{backend_url.rstrip('/')}/api/test/stop", timeout=10)
        except Exception:
            pass
        raise

    generate_plot(result, str(plot_path))
    generate_csv(result, str(csv_path))
    generate_summary(result, str(summary_path))
    validate_result(result)
    return {
        "csv": str(csv_path),
        "summary": str(summary_path),
        "plot": str(plot_path),
    }


async def capture_and_promote(
    audio_path: Path,
    backend_url: str,
    run_dir: Path,
    entry: dict[str, Any],
    expected_pipeline: dict[str, Any],
) -> None:
    staging_root = run_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    prefix = f"repeat-{entry.get('repeat', 0):02d}-{entry.get('sample', 'preflight')}-"
    with tempfile.TemporaryDirectory(prefix=prefix, dir=staging_root) as temporary:
        staging_dir = Path(temporary)
        artifacts = await capture_one(audio_path, backend_url, staging_dir)
        valid, reason = validate_artifact_set(
            Path(artifacts["csv"]),
            Path(artifacts["summary"]),
            Path(artifacts["plot"]),
            expected_pipeline=expected_pipeline,
        )
        if not valid:
            raise ExperimentError(reason)

        for key in ("csv", "summary", "plot"):
            destination = run_dir / entry[key]
            destination.parent.mkdir(parents=True, exist_ok=True)
            Path(artifacts[key]).replace(destination)

    entry["artifact_sha256"] = _artifact_hashes(run_dir, entry)
    valid, reason = capture_artifacts_valid(
        run_dir,
        entry,
        expected_pipeline=expected_pipeline,
    )
    if not valid:
        raise ExperimentError(f"promoted artifact validation failed: {reason}")


def _entry_audio_path(entry: dict[str, Any]) -> Path:
    path = Path(entry["audio_path"])
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def add_candidate_gate_results(analysis: dict[str, Any]) -> None:
    all_pass = True
    for trace in analysis["traces"]:
        adaptive = trace["adaptive"]
        gates = {
            "queue_p95_at_or_below_10_seconds": (
                adaptive["time_weighted_queue_p95_seconds"] <= 10.0
            ),
            "time_over_10_seconds_below_1_percent": (
                adaptive["percent_playback_window_above_limit"] < 1.0
            ),
            "no_audio_chunks_dropped": adaptive["chunks_dropped"] == 0,
        }
        trace["candidate_acceptance"] = {
            "gates": gates,
            "pass": all(gates.values()),
        }
        all_pass = all_pass and trace["candidate_acceptance"]["pass"]
    analysis["candidate_acceptance"] = {
        "all_traces_pass": all_pass,
        "note": (
            "An SLA miss is an experiment result, not an operational runner failure. "
            "Browser parity, semantic joke delay, and native-listener quality remain separate gates."
        ),
    }


def render_experiment_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        render_markdown(analysis).rstrip(),
        "",
        "## Candidate 5-10 second SLA evaluation",
        "",
        (
            "These rows replay newly captured live Riva arrival traces. They do "
            "not represent a browser/Web Audio execution or listening-quality review."
        ),
        "",
        (
            "| Trace | Time-weighted adaptive p95 | Time >10s | Longest continuous 1.10x "
            "playback | Candidate gates |"
        ),
        "|---|---:|---:|---:|---|",
    ]
    for trace in analysis["traces"]:
        adaptive = trace["adaptive"]
        passed = trace["candidate_acceptance"]["pass"]
        lines.append(
            "| {trace} | {p95:.3f}s | {above:.3f}% | {urgent:.3f}s | {status} |".format(
                trace=trace["trace_csv"],
                p95=adaptive["time_weighted_queue_p95_seconds"],
                above=adaptive["percent_playback_window_above_limit"],
                urgent=adaptive["max_continuous_urgent_playback_seconds"],
                status="PASS" if passed else "MISS",
            )
        )
    lines.extend(
        [
            "",
            (
                "Overall candidate queue gates: **{}**. A miss is retained as a "
                "measurement and does not make the experiment command fail."
            ).format(
                "PASS" if analysis["candidate_acceptance"]["all_traces_pass"] else "MISS"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def write_analysis(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    entries = [entry for entry in manifest["runs"] if entry["status"] == "completed"]
    expected = manifest["requested_repeats"] * len(manifest["samples"])
    if len(entries) != expected:
        raise ExperimentError(
            f"cannot analyze incomplete matrix: expected {expected} captures, got {len(entries)}"
        )

    csv_paths = sorted(run_dir / entry["csv"] for entry in entries)
    analysis = build_analysis(csv_paths)
    for trace, path in zip(analysis["traces"], csv_paths):
        trace["trace_csv"] = str(path.relative_to(run_dir))
    analysis["capture_provenance"] = {
        "run_id": manifest["run_id"],
        "backend_url": manifest["backend_url"],
        "git": manifest["git"],
        "pipeline": manifest.get("pipeline_provenance"),
        "paired_policy_comparison_from_identical_live_trace": True,
        "browser_web_audio_executed": False,
    }
    add_candidate_gate_results(analysis)

    json_path = run_dir / ANALYSIS_JSON
    markdown_path = run_dir / ANALYSIS_MARKDOWN
    json_path.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_experiment_markdown(analysis), encoding="utf-8")
    return analysis


def _merge_resume_settings(
    manifest: dict[str, Any],
    *,
    backend_url: str | None,
    repeats: int | None,
) -> tuple[str, int]:
    existing_backend = str(manifest.get("backend_url", DEFAULT_BACKEND))
    if backend_url is not None and backend_url.rstrip("/") != existing_backend.rstrip("/"):
        raise ExperimentError(
            f"resume backend mismatch: manifest has {existing_backend}, requested {backend_url}"
        )
    existing_repeats = int(manifest.get("requested_repeats", 1))
    if repeats is not None and repeats != existing_repeats:
        raise ExperimentError(
            f"resume repeat mismatch: manifest has {existing_repeats}, requested {repeats}"
        )
    return existing_backend, existing_repeats


def print_plan(run_dir: Path, manifest: dict[str, Any]) -> None:
    print("Three-sample experiment plan")
    print(f"  Run directory: {run_dir}")
    print(f"  Backend: {manifest['backend_url']}")
    print(f"  Preflight: {'yes' if manifest['preflight']['required'] else 'skipped'}")
    print(f"  Repeats: {manifest['requested_repeats']}")
    print("  Execution: sequential; one live Riva trace produces fixed + adaptive analysis")
    for entry in manifest["runs"]:
        print(
            f"    repeat {entry['repeat']:02d}: {entry['sample']} "
            f"({entry['audio_path']})"
        )
    print("  Containers/backend: checked but never started or stopped by this command")


async def execute_experiment(run_dir: Path, manifest: dict[str, Any]) -> int:
    manifest["status"] = "running"
    manifest["completed_at_utc"] = None
    write_manifest(run_dir, manifest)

    try:
        readiness = check_backend_ready(manifest["backend_url"])
        pipeline_provenance = freeze_or_validate_pipeline_provenance(
            manifest,
            readiness,
        )
        manifest["backend_readiness"] = readiness
        write_manifest(run_dir, manifest)

        preflight = manifest["preflight"]
        if preflight["required"]:
            valid, _ = capture_artifacts_valid(
                run_dir,
                preflight,
                expected_pipeline=pipeline_provenance,
            )
            if valid:
                preflight["status"] = "completed"
                preflight["error"] = None
                preflight["artifact_sha256"] = _artifact_hashes(
                    run_dir, preflight
                )
                write_manifest(run_dir, manifest)
                print("\n=== Preflight: using valid completed checkpoint ===")
            else:
                print("\n=== Preflight: one-minute service validation ===")
                preflight["status"] = "running"
                preflight["error"] = None
                write_manifest(run_dir, manifest)
                try:
                    await capture_and_promote(
                        REPOSITORY_ROOT / preflight["audio_path"],
                        manifest["backend_url"],
                        run_dir,
                        preflight,
                        pipeline_provenance,
                    )
                except Exception as exc:
                    preflight["status"] = "failed"
                    preflight["error"] = str(exc)
                    raise ExperimentError(f"preflight failed: {exc}") from exc
                preflight["status"] = "completed"
                write_manifest(run_dir, manifest)

        total = len(manifest["runs"])
        for index, entry in enumerate(manifest["runs"], start=1):
            valid, reason = capture_artifacts_valid(
                run_dir,
                entry,
                expected_pipeline=pipeline_provenance,
            )
            if valid:
                entry["status"] = "completed"
                entry["error"] = None
                entry["completed_at_utc"] = entry.get("completed_at_utc") or utc_now()
                entry["artifact_sha256"] = _artifact_hashes(run_dir, entry)
                write_manifest(run_dir, manifest)
                print(
                    f"\n=== Capture {index}/{total}: repeat {entry['repeat']:02d} "
                    f"{entry['sample']} (checkpoint valid; skipped) ==="
                )
                continue
            if entry.get("status") == "completed" and not valid:
                print(f"Invalid checkpoint will be rerun: {reason}")

            print(
                f"\n=== Capture {index}/{total}: repeat {entry['repeat']:02d} "
                f"{entry['sample']} ==="
            )
            entry["status"] = "running"
            entry["started_at_utc"] = utc_now()
            entry["completed_at_utc"] = None
            entry["error"] = None
            write_manifest(run_dir, manifest)
            try:
                await capture_and_promote(
                    _entry_audio_path(entry),
                    manifest["backend_url"],
                    run_dir,
                    entry,
                    pipeline_provenance,
                )
                valid, reason = capture_artifacts_valid(
                    run_dir,
                    entry,
                    expected_pipeline=pipeline_provenance,
                )
                if not valid:
                    raise ExperimentError(reason)
            except Exception as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)
                manifest["status"] = "failed"
                write_manifest(run_dir, manifest)
                raise ExperimentError(
                    f"repeat {entry['repeat']:02d} {entry['sample']} failed: {exc}"
                ) from exc
            entry["status"] = "completed"
            entry["completed_at_utc"] = utc_now()
            write_manifest(run_dir, manifest)

        manifest["analysis"]["status"] = "running"
        manifest["analysis"]["error"] = None
        manifest["analysis"]["all_candidate_gates_pass"] = None
        write_manifest(run_dir, manifest)
        analysis = write_analysis(run_dir, manifest)
        manifest["analysis"]["status"] = "completed"
        manifest["analysis"]["all_candidate_gates_pass"] = analysis[
            "candidate_acceptance"
        ]["all_traces_pass"]
        manifest["status"] = "completed"
        manifest["completed_at_utc"] = utc_now()
        write_manifest(run_dir, manifest)
    except Exception as exc:
        manifest["status"] = "failed"
        if manifest["analysis"].get("status") == "running":
            manifest["analysis"]["status"] = "failed"
            manifest["analysis"]["error"] = str(exc)
        write_manifest(run_dir, manifest)
        print(f"\nERROR: {exc}", file=sys.stderr)
        print(f"Resume after correction with: --resume-dir {run_dir}", file=sys.stderr)
        return 1

    print(f"\nExperiment complete: {run_dir}")
    print(f"Analysis: {run_dir / ANALYSIS_MARKDOWN}")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture all three samples through live Riva and compare fixed/adaptive playback"
        )
    )
    parser.add_argument(
        "--backend",
        help=f"FastAPI backend URL (new-run default: {DEFAULT_BACKEND})",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"parent directory for new runs (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument("--run-id", help="optional deterministic new-run directory name")
    parser.add_argument(
        "--repeats",
        type=int,
        help="live Riva traces per sample (new-run default: 1)",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="skip the one-minute service-path validation",
    )
    parser.add_argument(
        "--resume-dir",
        type=Path,
        help="continue an existing run using its manifest and valid checkpoints",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved plan without contacting the backend or writing artifacts",
    )
    args = parser.parse_args(argv)
    if args.repeats is not None and args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.resume_dir is not None and args.run_id is not None:
        parser.error("--run-id cannot be used with --resume-dir")
    if args.resume_dir is not None and args.skip_preflight:
        parser.error("--skip-preflight cannot change an existing resumed run")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    files = [REPOSITORY_ROOT / path for path in LONG_FORM_FILES]

    try:
        if args.resume_dir is not None:
            run_dir = args.resume_dir.resolve()
            manifest = load_manifest(run_dir)
            validate_resume_provenance(manifest)
            backend_url, repeats = _merge_resume_settings(
                manifest,
                backend_url=args.backend,
                repeats=args.repeats,
            )
            manifest["backend_url"] = backend_url.rstrip("/")
            manifest["requested_repeats"] = repeats
        else:
            metadata = git_metadata()
            repeats = args.repeats or 1
            backend_url = (args.backend or DEFAULT_BACKEND).rstrip("/")
            run_id = args.run_id or make_run_id(short_commit=metadata["short_commit"])
            run_dir = (args.output_root / run_id).resolve()
            if run_dir.exists() and not args.dry_run:
                raise ExperimentError(
                    f"new-run directory already exists: {run_dir}; use --resume-dir"
                )
            manifest = build_manifest(
                run_id=run_id,
                run_dir=run_dir,
                backend_url=backend_url,
                repeats=repeats,
                skip_preflight=args.skip_preflight,
                files=files,
                include_hashes=not args.dry_run,
            )

        print_plan(run_dir, manifest)
        if args.dry_run:
            return 0
        with experiment_lock(manifest["backend_url"]):
            return asyncio.run(execute_experiment(run_dir, manifest))
    except ExperimentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
