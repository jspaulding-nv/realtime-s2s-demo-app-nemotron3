#!/usr/bin/env python3
"""Run the registered long-form tail-freshness shadows sequentially.

Each sample receives its own immutable attempt directory.  A private manifest
is checkpointed before and after every child process so an interrupted VM can
resume without overwriting completed evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from run_live_tail_shadow_preflight import (
    LONG_FORM_SOURCES,
    REPOSITORY_ROOT,
    sha256_file,
)


SCHEMA = "live-tail-shadow-long-form-batch/v1"
MANIFEST_NAME = "manifest.json"


class LongFormBatchError(RuntimeError):
    """An operational failure that prevents a valid batch checkpoint."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def git_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout.strip())


def default_output_directory() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_commit = git_commit()[:7]
    return (
        REPOSITORY_ROOT
        / "experiment_results"
        / f"live-tail-shadow-long-form-100ms-{timestamp}-{short_commit}"
    )


def write_private_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def new_manifest(samples: Sequence[int]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "createdAtUtc": utc_now(),
        "updatedAtUtc": utc_now(),
        "status": "planned",
        "repositoryCommit": git_commit(),
        "repositoryDirtyAtStart": git_dirty(),
        "incrementalFrameMs": 100,
        "observationOnly": True,
        "liveAudioChanged": False,
        "samples": [
            {
                "sample": sample,
                "sourceSha256": LONG_FORM_SOURCES[sample][1],
                "status": "pending",
                "attempts": [],
            }
            for sample in samples
        ],
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LongFormBatchError("could not read the resume manifest") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise LongFormBatchError("resume manifest schema does not match")
    if value.get("incrementalFrameMs") != 100:
        raise LongFormBatchError("resume manifest is not the 100 ms profile")
    if value.get("repositoryCommit") != git_commit():
        raise LongFormBatchError("resume requires the original Git commit")
    samples = value.get("samples")
    if not isinstance(samples, list) or not samples:
        raise LongFormBatchError("resume manifest has no sample plan")
    for item in samples:
        if not isinstance(item, dict) or item.get("sample") not in LONG_FORM_SOURCES:
            raise LongFormBatchError("resume manifest contains an invalid sample")
        sample = int(item["sample"])
        if item.get("sourceSha256") != LONG_FORM_SOURCES[sample][1]:
            raise LongFormBatchError("resume manifest source identity changed")
    return value


def sample_record(manifest: dict[str, Any], sample: int) -> dict[str, Any]:
    for item in manifest["samples"]:
        if item["sample"] == sample:
            return item
    raise LongFormBatchError(f"sample {sample} is absent from the manifest")


def validate_sources(manifest: dict[str, Any]) -> None:
    for item in manifest["samples"]:
        sample = int(item["sample"])
        source, expected_sha256 = LONG_FORM_SOURCES[sample]
        if not source.is_file() or sha256_file(source) != expected_sha256:
            raise LongFormBatchError(
                f"registered source identity failed for sample {sample:02d}"
            )


def next_attempt_directory(root: Path, record: dict[str, Any]) -> Path:
    attempt = len(record["attempts"]) + 1
    return root / f"sample-{record['sample']:02d}-attempt-{attempt:02d}"


def build_child_command(
    *,
    sample: int,
    output_dir: Path,
    timeout_seconds: float,
    npm: Optional[Path],
    chrome: Optional[Path],
) -> list[str]:
    command = [
        sys.executable,
        str(REPOSITORY_ROOT / "run_live_tail_shadow_preflight.py"),
        "--long-form-sample",
        str(sample),
        "--incremental-frame-ms",
        "100",
        "--output-dir",
        str(output_dir),
        "--timeout-seconds",
        str(timeout_seconds),
        "--decode-timeout-seconds",
        "180",
        "--download-timeout-seconds",
        "120",
    ]
    if npm is not None:
        command.extend(("--npm", str(npm)))
    if chrome is not None:
        command.extend(("--chrome", str(chrome)))
    return command


def run_batch(args: argparse.Namespace) -> Path:
    if args.resume_dir is not None:
        root = args.resume_dir.expanduser().resolve(strict=True)
        manifest_path = root / MANIFEST_NAME
        manifest = load_manifest(manifest_path)
    else:
        root = (args.output_dir or default_output_directory()).expanduser().resolve()
        root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.mkdir(mode=0o700, exist_ok=False)
        manifest_path = root / MANIFEST_NAME
        manifest = new_manifest(args.samples)
        write_private_json(manifest_path, manifest)

    validate_sources(manifest)
    if args.dry_run:
        print(f"output: {root}")
        for item in manifest["samples"]:
            print(f"sample {item['sample']:02d}: {item['status']}")
        return manifest_path

    manifest["status"] = "running"
    manifest["updatedAtUtc"] = utc_now()
    write_private_json(manifest_path, manifest)

    for item in manifest["samples"]:
        if item.get("status") == "passed":
            continue
        sample = int(item["sample"])
        attempt_dir = next_attempt_directory(root, item)
        attempt = {
            "directory": attempt_dir.name,
            "startedAtUtc": utc_now(),
            "completedAtUtc": None,
            "status": "running",
            "returnCode": None,
            "analysisSha256": None,
        }
        item["status"] = "running"
        item["attempts"].append(attempt)
        manifest["updatedAtUtc"] = utc_now()
        write_private_json(manifest_path, manifest)

        command = build_child_command(
            sample=sample,
            output_dir=attempt_dir,
            timeout_seconds=args.timeout_seconds,
            npm=args.npm,
            chrome=args.chrome,
        )
        print(f"Starting complete sample {sample:02d}; evidence: {attempt_dir}")
        completed = subprocess.run(command, cwd=REPOSITORY_ROOT, check=False)
        analysis = attempt_dir / "tail-shadow.analysis.json"
        passed = completed.returncode == 0 and analysis.is_file()
        attempt["completedAtUtc"] = utc_now()
        attempt["returnCode"] = completed.returncode
        attempt["status"] = "passed" if passed else "failed"
        attempt["analysisSha256"] = sha256_file(analysis) if passed else None
        item["status"] = attempt["status"]
        manifest["updatedAtUtc"] = utc_now()
        write_private_json(manifest_path, manifest)
        if not passed:
            manifest["status"] = "failed"
            write_private_json(manifest_path, manifest)
            raise LongFormBatchError(
                f"sample {sample:02d} failed; resume from {root} after diagnosis"
            )
        print(f"PASS: complete sample {sample:02d}")

    manifest["status"] = "passed"
    manifest["completedAtUtc"] = utc_now()
    manifest["updatedAtUtc"] = manifest["completedAtUtc"]
    write_private_json(manifest_path, manifest)
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run all registered long-form live tail-freshness shadows "
            "sequentially with the selected 100 ms profile"
        )
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--output-dir", type=Path)
    group.add_argument("--resume-dir", type=Path)
    parser.add_argument(
        "--samples",
        type=int,
        choices=tuple(LONG_FORM_SOURCES),
        nargs="+",
        default=list(LONG_FORM_SOURCES),
    )
    parser.add_argument("--npm", type=Path)
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    previous_umask = os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        if args.timeout_seconds <= 0:
            raise LongFormBatchError("--timeout-seconds must be positive")
        manifest = run_batch(args)
        print(f"Batch manifest: {manifest}")
        return 0
    except (OSError, subprocess.SubprocessError, LongFormBatchError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
