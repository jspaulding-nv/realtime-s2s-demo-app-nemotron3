#!/usr/bin/env python3
"""Run one private rendered-digital common-clock browser preflight.

This runner owns only the local FastAPI, Vite, and headless Chrome processes
that it starts.  It never starts, stops, or otherwise mutates the Riva
containers.  The four raw evidence artifacts and operational logs are written
to a new private output directory outside the repository and are retained on
both success and failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parent
# This repository supports an isolated ``.python-packages`` install used by
# its documented test commands.  Prefer it when present without requiring a
# caller to leak or mutate PYTHONPATH in their shell.
LOCAL_PYTHON_PACKAGES = REPOSITORY_ROOT / ".python-packages"
if LOCAL_PYTHON_PACKAGES.is_dir():
    sys.path.insert(0, str(LOCAL_PYTHON_PACKAGES))
DEFAULT_AUDIO = REPOSITORY_ROOT / "test_audio" / "preflight.wav"
DEFAULT_DASHBOARD_URL = "http://localhost:5173/#/test"
DEFAULT_API_CONFIG_URL = "http://127.0.0.1:8000/api/config"
PREFLIGHT_FILE_SHA256 = (
    "0c2cb04d9774f60472b55355f587da2148a053f3a55c05ff36c7dfc23be5c257"
)
APPROVED_ASR_IMAGE_DIGEST = (
    "sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850"
)
APPROVED_NMT_IMAGE_DIGEST = (
    "sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb"
)
APPROVED_TTS_IMAGE_DIGEST = (
    "sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d"
)
ARTIFACT_SUFFIXES = (
    ".manifest.json",
    ".timing.csv",
    ".blocks.csv",
    ".stereo.wav",
)
ARTIFACT_PREFIX = "rendered-digital-preflight-"
DOCKER_ATTESTATION_SCHEMA = "rendered-digital-docker-attestation/v1"
OPERATIONAL_OUTPUT_FILES = {
    "backend.log",
    "docker-attestation.json",
    "frontend-build.log",
    "frontend.log",
    "chrome.log",
}
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class PreflightRunnerError(RuntimeError):
    """An expected fail-closed preflight error safe to show to the operator."""


class ValidatorFailure(PreflightRunnerError):
    """The offline validator produced a conclusive nonzero status."""

    def __init__(self, return_code: int, report_path: Path) -> None:
        super().__init__(
            f"offline validation returned status {return_code}; "
            f"the report is retained at {report_path}"
        )
        self.return_code = return_code


@dataclass(frozen=True)
class RepositoryState:
    commit: str
    dirty: bool


@dataclass(frozen=True)
class EvidenceBundle:
    base_name: str
    manifest: Path
    wav: Path
    blocks: Path
    timing_csv: Path


@dataclass(frozen=True)
class DockerRoleSpec:
    role: str
    http_port_environment: str
    http_host_port_default: int
    grpc_port_environment: str
    grpc_host_port_default: int
    grpc_container_port: int
    image_repository: str
    approved_digest: str
    backend_model_key: str


@dataclass(frozen=True)
class DockerRoleAttestation:
    role: str
    container_id: str
    image_reference: str
    image_id: str
    repo_digest: str
    http_host_port: int
    http_container_port: int
    grpc_host_port: int
    grpc_container_port: int

    def safe_record(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "container_id": self.container_id,
            "image_reference": self.image_reference,
            "image_id": self.image_id,
            "repo_digest": self.repo_digest,
            "running": True,
            "health": "healthy",
            "http": {
                "host_port": self.http_host_port,
                "container_port": self.http_container_port,
            },
            "grpc": {
                "host_port": self.grpc_host_port,
                "container_port": self.grpc_container_port,
            },
        }


@dataclass(frozen=True)
class DockerRuntimeAttestation:
    roles: tuple[DockerRoleAttestation, ...]

    def identity_sha256(self) -> str:
        encoded = json.dumps(
            [role.safe_record() for role in self.roles],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass
class OwnedProcess:
    name: str
    process: subprocess.Popen[Any]
    log_handle: Any


DOCKER_ROLE_SPECS = (
    DockerRoleSpec(
        role="asr",
        http_port_environment="ASR_HTTP_PORT",
        http_host_port_default=9002,
        grpc_port_environment="ASR_GRPC_PORT",
        grpc_host_port_default=50052,
        grpc_container_port=50052,
        image_repository="nvcr.io/nim/nvidia/nemotron-asr-streaming",
        approved_digest=APPROVED_ASR_IMAGE_DIGEST,
        backend_model_key="asr",
    ),
    DockerRoleSpec(
        role="nmt",
        http_port_environment="NMT_HTTP_PORT",
        http_host_port_default=9001,
        grpc_port_environment="NMT_GRPC_PORT",
        grpc_host_port_default=50051,
        grpc_container_port=50051,
        image_repository="nvcr.io/nim/nvidia/riva-translate-1_6b",
        approved_digest=APPROVED_NMT_IMAGE_DIGEST,
        backend_model_key="nmt",
    ),
    DockerRoleSpec(
        role="tts",
        http_port_environment="TTS_HTTP_PORT",
        http_host_port_default=9003,
        grpc_port_environment="TTS_GRPC_PORT",
        grpc_host_port_default=50053,
        grpc_container_port=50053,
        image_repository="nvcr.io/nim/nvidia/magpie-tts-multilingual",
        approved_digest=APPROVED_TTS_IMAGE_DIGEST,
        backend_model_key="tts",
    ),
)


def sha256_file(path: Path, *, chunk_bytes: int = 1024 * 1024) -> str:
    """Hash a file without retaining its private contents in memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_bytes):
            digest.update(block)
    return digest.hexdigest()


def _strip_unquoted_comment(value: str) -> str:
    """Remove shell-style comments that begin after whitespace."""

    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character == "#" and (
            index == 0 or value[index - 1].isspace()
        ):
            return value[:index].rstrip()
    return value.strip()


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple dotenv file without executing shell code."""

    if not path.is_file():
        raise PreflightRunnerError("the requested environment file is missing")
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PreflightRunnerError(
            "the environment file could not be read"
        ) from exc
    for line_number, original in enumerate(lines, start=1):
        stripped = original.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise PreflightRunnerError(
                f"invalid environment assignment on line {line_number}"
            )
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not ENV_KEY_PATTERN.fullmatch(key):
            raise PreflightRunnerError(
                f"invalid environment key on line {line_number}"
            )
        raw_value = _strip_unquoted_comment(raw_value.strip())
        try:
            tokens = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise PreflightRunnerError(
                f"invalid environment quoting on line {line_number}"
            ) from exc
        if not tokens:
            value = ""
        elif len(tokens) == 1:
            value = tokens[0]
        else:
            raise PreflightRunnerError(
                f"unquoted whitespace in environment value on line "
                f"{line_number}"
            )
        result[key] = value
    return result


def build_child_environment(
    base: Mapping[str, str],
    loaded: Mapping[str, str],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, str]:
    """Build a least-privilege child environment for backend and Vite."""

    environment = dict(base)
    environment.update(loaded)
    # The already-running Riva containers own registry authentication.  The
    # application processes do not need this credential.
    environment.pop("NGC_API_KEY", None)
    local_packages = repository_root / ".python-packages"
    python_path = environment.get("PYTHONPATH", "")
    path_entries = [str(local_packages)]
    if python_path:
        path_entries.append(python_path)
    environment["PYTHONPATH"] = os.pathsep.join(path_entries)
    return environment


def inspect_repository(
    repository_root: Path = REPOSITORY_ROOT,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RepositoryState:
    """Return repository provenance, failing when it cannot be established."""

    def git(*arguments: str) -> str:
        try:
            completed = run(
                ["git", "-C", str(repository_root), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PreflightRunnerError(
                "repository provenance could not be inspected"
            ) from exc
        if completed.returncode != 0:
            raise PreflightRunnerError(
                "repository provenance could not be inspected"
            )
        return completed.stdout.strip()

    commit = git("rev-parse", "HEAD").lower()
    if not GIT_COMMIT_PATTERN.fullmatch(commit):
        raise PreflightRunnerError("repository commit is not a full Git SHA")
    status = git("status", "--porcelain", "--untracked-files=normal")
    return RepositoryState(commit=commit, dirty=bool(status))


def require_clean_repository(
    repository_root: Path = REPOSITORY_ROOT,
    *,
    expected_commit: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    state = inspect_repository(repository_root, run=run)
    if state.dirty:
        raise PreflightRunnerError(
            "repository is dirty; commit or remove all tracked and untracked "
            "changes before capture"
        )
    if expected_commit is not None and state.commit != expected_commit:
        raise PreflightRunnerError(
            "repository commit changed after preflight startup"
        )
    return state.commit


def _nested_value(payload: Mapping[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            raise PreflightRunnerError(
                f"/api/config is missing required field {'.'.join(keys)}"
            )
        value = value[key]
    return value


def validate_api_config(payload: Mapping[str, Any], commit: str) -> None:
    """Fail early unless the backend advertises the registered runtime."""

    exact_numeric_paths = {
        ("stagedConfig", "nmtRpcTimeoutSeconds"),
        ("stagedConfig", "ttsRpcTimeoutSeconds"),
        ("stagedConfig", "ttsMaxSegmentAudioSeconds"),
        ("stagedConfig", "closeTimeoutSeconds"),
    }
    required_values: tuple[tuple[tuple[str, ...], Any], ...] = (
        (("pipelineMode",), "staged"),
        (("repositoryProvenance", "commit"), commit),
        (("repositoryProvenance", "dirty"), False),
        (("modelConfig", "asr", "imageDigest"), APPROVED_ASR_IMAGE_DIGEST),
        (
            ("modelConfig", "asr", "profile"),
            "name=nemotron-asr-streaming,type=en-US,batch_size=32",
        ),
        (("modelConfig", "asr", "eouMs"), 800),
        (("modelConfig", "asr", "wordTimeOffsets"), True),
        (("modelConfig", "asr", "sourceLanguage"), "en-US"),
        (("modelConfig", "nmt", "imageDigest"), APPROVED_NMT_IMAGE_DIGEST),
        (("modelConfig", "nmt", "model"), "megatronnmt_any_any_1b"),
        (("modelConfig", "nmt", "sourceLanguage"), "en-US"),
        (("modelConfig", "nmt", "targetLanguage"), "es-US"),
        (("modelConfig", "tts", "imageDigest"), APPROVED_TTS_IMAGE_DIGEST),
        (
            ("modelConfig", "tts", "profile"),
            "name=magpie-tts-multilingual,batch_size=8",
        ),
        (
            ("modelConfig", "tts", "voice"),
            "Magpie-Multilingual.ES-US.Isabela",
        ),
        (("modelConfig", "tts", "targetLanguage"), "es-US"),
        (("stagedConfig", "telemetrySchemaVersion"), 3),
        (("stagedConfig", "segmentMaxChars"), 240),
        (("stagedConfig", "segmentMaxAgeMs"), 2000),
        (("stagedConfig", "asrEventQueueMaxSize"), 32),
        (("stagedConfig", "nmtQueueMaxSize"), 4),
        (("stagedConfig", "ttsQueueMaxSize"), 4),
        (("stagedConfig", "outputQueueMaxSize"), 4),
        (("stagedConfig", "nmtRpcTimeoutSeconds"), 15),
        (("stagedConfig", "ttsRpcTimeoutSeconds"), 60),
        (("stagedConfig", "ttsMaxSegmentAudioSeconds"), 60),
        (("stagedConfig", "ttsMaxRetries"), 1),
        (
            ("stagedConfig", "ttsResponseChunkTelemetryEnabled"),
            False,
        ),
        (("stagedConfig", "ttsSubsegmentMaxChars"), 0),
        (("stagedConfig", "ttsSubsegmentMinChars"), 12),
        (
            ("stagedConfig", "ttsIncrementalAtomicFallbackMaxChars"),
            4,
        ),
        (("stagedConfig", "closeTimeoutSeconds"), 10),
        (("stagedConfig", "ttsIncrementalPublishEnabled"), True),
        (("stagedConfig", "ttsIncrementalFrameMs"), 100),
    )
    versions = _nested_value(payload, "audioMetadataProtocolVersions")
    if (
        not isinstance(versions, list)
        or not any(type(item) is int and item == 1 for item in versions)
    ):
        raise PreflightRunnerError(
            "/api/config does not advertise audio metadata protocol 1"
        )
    for key_path, expected in required_values:
        actual = _nested_value(payload, *key_path)
        if key_path in exact_numeric_paths:
            matches = (
                not isinstance(actual, bool)
                and isinstance(actual, (int, float))
                and actual == expected
            )
        else:
            matches = type(actual) is type(expected) and actual == expected
        if not matches:
            raise PreflightRunnerError(
                f"/api/config field {'.'.join(key_path)} does not match "
                "the registered preflight runtime"
            )


@dataclass(frozen=True)
class _DockerContainerObservation:
    container_id: str
    image_reference: str
    image_id: str
    running: bool
    status: str
    health: str
    published_port_pairs: frozenset[tuple[int, int]]


@dataclass(frozen=True)
class _DockerImageObservation:
    image_id: str
    repo_digests: tuple[str, ...]


def _configured_port(
    environment: Mapping[str, str],
    variable: str,
    default: int,
) -> int:
    raw: Any = environment.get(variable, str(default))
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise PreflightRunnerError(
            f"{variable} must name a valid local TCP port"
        ) from exc
    if isinstance(raw, bool) or not 1 <= port <= 65535:
        raise PreflightRunnerError(
            f"{variable} must name a valid local TCP port"
        )
    return port


def _run_readonly_docker(
    arguments: Sequence[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Execute one allowlisted, read-only Docker identity query."""

    operation = tuple(arguments[:2])
    if operation not in {
        ("container", "ls"),
        ("container", "inspect"),
        ("image", "inspect"),
    }:
        raise PreflightRunnerError(
            "internal Docker attestation attempted a non-read-only operation"
        )
    try:
        completed = run(
            ["docker", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightRunnerError(
            "read-only Docker attestation command could not be executed"
        ) from exc
    if completed.returncode != 0:
        raise PreflightRunnerError(
            "read-only Docker attestation command failed"
        )
    if len(completed.stdout.encode("utf-8")) > 2_000_000:
        raise PreflightRunnerError(
            "read-only Docker attestation returned excessive output"
        )
    return completed.stdout


def _container_ids_publishing_port(
    port: int,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> set[str]:
    output = _run_readonly_docker(
        [
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            f"publish={port}",
            "--format",
            "{{.ID}}",
        ],
        run=run,
    )
    ids: list[str] = [
        line.strip().lower()
        for line in output.splitlines()
        if line.strip()
    ]
    if any(CONTAINER_ID_PATTERN.fullmatch(value) is None for value in ids):
        raise PreflightRunnerError(
            "Docker returned an invalid container identity"
        )
    if len(ids) != len(set(ids)):
        raise PreflightRunnerError(
            "Docker returned a duplicate container identity"
        )
    return set(ids)


def _parse_published_port_pairs(value: Any) -> frozenset[tuple[int, int]]:
    if not isinstance(value, Mapping):
        raise PreflightRunnerError(
            "Docker container port inspection was malformed"
        )
    pairs: set[tuple[int, int]] = set()
    for container_key, bindings in value.items():
        if not isinstance(container_key, str):
            raise PreflightRunnerError(
                "Docker container port inspection was malformed"
            )
        match = re.fullmatch(r"(\d+)/(tcp|udp)", container_key)
        if match is None:
            raise PreflightRunnerError(
                "Docker container port inspection was malformed"
            )
        if bindings is None:
            continue
        if not isinstance(bindings, list):
            raise PreflightRunnerError(
                "Docker container port inspection was malformed"
            )
        container_port = int(match.group(1))
        protocol = match.group(2)
        for binding in bindings:
            if not isinstance(binding, Mapping):
                raise PreflightRunnerError(
                    "Docker container port inspection was malformed"
                )
            host_port_raw = binding.get("HostPort")
            if (
                protocol != "tcp"
                or not isinstance(host_port_raw, str)
                or not host_port_raw.isdigit()
            ):
                raise PreflightRunnerError(
                    "Docker container port inspection was malformed"
                )
            host_port = int(host_port_raw)
            if not (
                1 <= container_port <= 65535
                and 1 <= host_port <= 65535
            ):
                raise PreflightRunnerError(
                    "Docker container port inspection was malformed"
                )
            pairs.add((container_port, host_port))
    return frozenset(pairs)


def _inspect_docker_containers(
    container_ids: Sequence[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, _DockerContainerObservation]:
    if not container_ids:
        raise PreflightRunnerError(
            "no Docker containers were selected for attestation"
        )
    safe_format = (
        "{{json .Id}}\t"
        "{{json .Config.Image}}\t"
        "{{json .Image}}\t"
        "{{json .State.Running}}\t"
        "{{json .State.Status}}\t"
        "{{json .State.Health.Status}}\t"
        "{{json .NetworkSettings.Ports}}"
    )
    output = _run_readonly_docker(
        [
            "container",
            "inspect",
            "--format",
            safe_format,
            *container_ids,
        ],
        run=run,
    )
    observations: dict[str, _DockerContainerObservation] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise PreflightRunnerError(
                "Docker container inspection was malformed"
            )
        try:
            (
                container_id,
                image_reference,
                image_id,
                running,
                status,
                health,
                ports,
            ) = (json.loads(field) for field in fields)
        except json.JSONDecodeError as exc:
            raise PreflightRunnerError(
                "Docker container inspection was malformed"
            ) from exc
        if (
            not isinstance(container_id, str)
            or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
            or not isinstance(image_reference, str)
            or not image_reference
            or not isinstance(image_id, str)
            or IMAGE_ID_PATTERN.fullmatch(image_id) is None
            or type(running) is not bool
            or not isinstance(status, str)
            or not isinstance(health, str)
        ):
            raise PreflightRunnerError(
                "Docker container inspection was malformed"
            )
        container_id = container_id.lower()
        if container_id in observations:
            raise PreflightRunnerError(
                "Docker returned duplicate container inspection records"
            )
        observations[container_id] = _DockerContainerObservation(
            container_id=container_id,
            image_reference=image_reference,
            image_id=image_id.lower(),
            running=running,
            status=status,
            health=health,
            published_port_pairs=_parse_published_port_pairs(ports),
        )
    expected_ids = set(container_ids)
    if set(observations) != expected_ids:
        raise PreflightRunnerError(
            "Docker container inspection did not resolve every selected "
            "container exactly once"
        )
    return observations


def _inspect_docker_images(
    image_ids: Sequence[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, _DockerImageObservation]:
    if not image_ids:
        raise PreflightRunnerError(
            "no Docker images were selected for attestation"
        )
    safe_format = "{{json .Id}}\t{{json .RepoDigests}}"
    output = _run_readonly_docker(
        [
            "image",
            "inspect",
            "--format",
            safe_format,
            *image_ids,
        ],
        run=run,
    )
    observations: dict[str, _DockerImageObservation] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise PreflightRunnerError(
                "Docker image inspection was malformed"
            )
        try:
            image_id, repo_digests = (
                json.loads(field) for field in fields
            )
        except json.JSONDecodeError as exc:
            raise PreflightRunnerError(
                "Docker image inspection was malformed"
            ) from exc
        if (
            not isinstance(image_id, str)
            or IMAGE_ID_PATTERN.fullmatch(image_id) is None
            or not isinstance(repo_digests, list)
            or not repo_digests
            or any(
                not isinstance(digest, str)
                for digest in repo_digests
            )
        ):
            raise PreflightRunnerError(
                "Docker image is tag-only or lacks an immutable RepoDigest"
            )
        image_id = image_id.lower()
        if image_id in observations:
            raise PreflightRunnerError(
                "Docker returned duplicate image inspection records"
            )
        observations[image_id] = _DockerImageObservation(
            image_id=image_id,
            repo_digests=tuple(repo_digests),
        )
    expected_ids = set(image_ids)
    if set(observations) != expected_ids:
        raise PreflightRunnerError(
            "Docker image inspection did not resolve every selected image "
            "exactly once"
        )
    return observations


def _image_repository(image_reference: str) -> str:
    repository = image_reference.split("@", 1)[0]
    last_slash = repository.rfind("/")
    last_colon = repository.rfind(":")
    if last_colon > last_slash:
        repository = repository[:last_colon]
    return repository


def attest_docker_runtime(
    environment: Mapping[str, str],
    backend_config: Mapping[str, Any],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> DockerRuntimeAttestation:
    """Bind published model endpoints to resolved immutable local images."""

    selected_ids: dict[str, str] = {}
    expected_ports: dict[str, tuple[int, int]] = {}
    for spec in DOCKER_ROLE_SPECS:
        http_port = _configured_port(
            environment,
            spec.http_port_environment,
            spec.http_host_port_default,
        )
        grpc_port = _configured_port(
            environment,
            spec.grpc_port_environment,
            spec.grpc_host_port_default,
        )
        if http_port == grpc_port:
            raise PreflightRunnerError(
                f"Docker {spec.role} HTTP and gRPC ports overlap"
            )
        http_ids = _container_ids_publishing_port(http_port, run=run)
        grpc_ids = _container_ids_publishing_port(grpc_port, run=run)
        if len(http_ids) != 1 or len(grpc_ids) != 1:
            raise PreflightRunnerError(
                f"Docker {spec.role} port mapping is missing or duplicated"
            )
        http_id = next(iter(http_ids))
        grpc_id = next(iter(grpc_ids))
        if http_id != grpc_id:
            raise PreflightRunnerError(
                f"Docker {spec.role} HTTP and gRPC ports map to different "
                "containers"
            )
        selected_ids[spec.role] = http_id
        expected_ports[spec.role] = (http_port, grpc_port)
    if len(set(selected_ids.values())) != len(DOCKER_ROLE_SPECS):
        raise PreflightRunnerError(
            "Docker model roles are mapped to duplicate containers"
        )

    containers = _inspect_docker_containers(
        list(selected_ids.values()),
        run=run,
    )
    image_ids = [
        containers[selected_ids[spec.role]].image_id
        for spec in DOCKER_ROLE_SPECS
    ]
    if len(set(image_ids)) != len(image_ids):
        raise PreflightRunnerError(
            "Docker model roles unexpectedly share one local image identity"
        )
    images = _inspect_docker_images(image_ids, run=run)

    roles: list[DockerRoleAttestation] = []
    for spec in DOCKER_ROLE_SPECS:
        container = containers[selected_ids[spec.role]]
        http_port, grpc_port = expected_ports[spec.role]
        expected_pairs = frozenset(
            {
                (9000, http_port),
                (spec.grpc_container_port, grpc_port),
            }
        )
        if (
            not container.running
            or container.status != "running"
            or container.health != "healthy"
        ):
            raise PreflightRunnerError(
                f"Docker {spec.role} container is not running and healthy"
            )
        if container.published_port_pairs != expected_pairs:
            raise PreflightRunnerError(
                f"Docker {spec.role} container ports are mis-mapped"
            )
        if _image_repository(container.image_reference) != spec.image_repository:
            raise PreflightRunnerError(
                f"Docker {spec.role} container uses the wrong image "
                "repository"
            )
        backend_digest = _nested_value(
            backend_config,
            "modelConfig",
            spec.backend_model_key,
            "imageDigest",
        )
        if backend_digest != spec.approved_digest:
            raise PreflightRunnerError(
                f"Docker {spec.role} digest disagrees with the backend "
                "declaration"
            )
        image = images.get(container.image_id)
        if image is None or image.image_id != container.image_id:
            raise PreflightRunnerError(
                f"Docker {spec.role} local image identity did not resolve"
            )
        expected_repo_digest = (
            f"{spec.image_repository}@{spec.approved_digest}"
        )
        if expected_repo_digest not in image.repo_digests:
            raise PreflightRunnerError(
                f"Docker {spec.role} local image RepoDigest is stale or "
                "unapproved"
            )
        roles.append(
            DockerRoleAttestation(
                role=spec.role,
                container_id=container.container_id,
                image_reference=container.image_reference,
                image_id=container.image_id,
                repo_digest=expected_repo_digest,
                http_host_port=http_port,
                http_container_port=9000,
                grpc_host_port=grpc_port,
                grpc_container_port=spec.grpc_container_port,
            )
        )
    return DockerRuntimeAttestation(roles=tuple(roles))


def write_docker_attestation(
    path: Path,
    attestation: DockerRuntimeAttestation,
    *,
    repository_commit: str,
    phase: str,
    browser_bundle_manifest_sha256: str | None = None,
) -> None:
    if (
        GIT_COMMIT_PATTERN.fullmatch(repository_commit) is None
        or phase not in {"verified_pre_capture", "verified_post_capture"}
        or (
            browser_bundle_manifest_sha256 is not None
            and re.fullmatch(
                r"[0-9a-f]{64}",
                browser_bundle_manifest_sha256,
            )
            is None
        )
    ):
        raise PreflightRunnerError(
            "Docker attestation metadata was malformed"
        )
    record = {
        "schema": DOCKER_ATTESTATION_SCHEMA,
        "phase": phase,
        "created_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "repository_commit": repository_commit,
        "identity_sha256": attestation.identity_sha256(),
        "browser_bundle_manifest_sha256": (
            browser_bundle_manifest_sha256
        ),
        "roles": {
            role.role: role.safe_record()
            for role in attestation.roles
        },
    }
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        raise PreflightRunnerError(
            "private Docker attestation could not be written"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class LocalHttpClient:
    """Small no-proxy HTTP client used only for localhost readiness."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        timeout_seconds: float = 2,
    ) -> bytes:
        request = urllib.request.Request(url, method=method)
        with self._opener.open(request, timeout=timeout_seconds) as response:
            if not 200 <= response.status < 300:
                raise urllib.error.HTTPError(
                    url,
                    response.status,
                    "unexpected response",
                    response.headers,
                    None,
                )
            return response.read() or b"ok"

    def json(
        self,
        url: str,
        *,
        method: str = "GET",
        timeout_seconds: float = 2,
    ) -> Mapping[str, Any]:
        try:
            payload = json.loads(
                self.request(
                    url,
                    method=method,
                    timeout_seconds=timeout_seconds,
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PreflightRunnerError(
                "localhost readiness returned invalid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise PreflightRunnerError(
                "localhost readiness returned a non-object JSON value"
            )
        return payload


def wait_for(
    predicate: Callable[[], Any],
    *,
    timeout_seconds: float,
    description: str,
    interval_seconds: float = 0.25,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    deadline = monotonic() + timeout_seconds
    last_value: Any = None
    while monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        sleep(interval_seconds)
    raise PreflightRunnerError(f"timed out waiting for {description}")


def ensure_local_http_url(url: str, *, label: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PreflightRunnerError(
            f"{label} must be an HTTP URL on localhost"
        )


def port_is_bound(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _open_private_log(path: Path):
    return path.open("w", encoding="utf-8")


def _node_major(node_path: Path) -> int | None:
    try:
        completed = subprocess.run(
            [str(node_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+\s*", completed.stdout)
    return int(match.group(1)) if completed.returncode == 0 and match else None


def find_npm(explicit: Path | None = None) -> Path:
    """Locate an npm executable backed by Node.js 22 or newer."""

    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    env_npm = os.environ.get("S2S_NPM")
    if env_npm:
        candidates.append(Path(env_npm).expanduser())
    candidates.extend(
        sorted(
            (REPOSITORY_ROOT.parent / ".tools").glob("node-v*/bin/npm"),
            reverse=True,
        )
    )
    candidates.extend(
        sorted(
            (Path.home() / ".nvm" / "versions" / "node").glob(
                "*/bin/npm"
            ),
            reverse=True,
        )
    )
    candidates.append(Path.home() / ".local" / "bin" / "npm")
    discovered = shutil.which("npm")
    if discovered:
        candidates.append(Path(discovered))

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.expanduser().absolute()
        if (
            candidate in seen
            or not candidate.is_file()
            or not os.access(candidate, os.X_OK)
        ):
            continue
        seen.add(candidate)
        node = candidate.with_name("node")
        node_major = _node_major(node)
        if node_major is not None and node_major >= 22:
            return candidate
    raise PreflightRunnerError(
        "Node.js 22+ npm was not found; pass --npm or set S2S_NPM"
    )


class OwnedProcessSet:
    """Tracks and gracefully cleans only child processes launched here."""

    def __init__(self) -> None:
        self._processes: list[OwnedProcess] = []

    @property
    def processes(self) -> tuple[OwnedProcess, ...]:
        return tuple(self._processes)

    def start(
        self,
        name: str,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
        popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    ) -> subprocess.Popen[Any]:
        log_handle = _open_private_log(log_path)
        try:
            process = popen(
                list(command),
                cwd=str(cwd),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            raise
        self._processes.append(
            OwnedProcess(name=name, process=process, log_handle=log_handle)
        )
        return process

    def require_alive(self) -> None:
        for owned in self._processes:
            status = owned.process.poll()
            if status is not None:
                raise PreflightRunnerError(
                    f"owned {owned.name} process exited early with status "
                    f"{status}; inspect its private log"
                )

    def close(self) -> None:
        for owned in reversed(self._processes):
            if owned.process.poll() is None:
                try:
                    os.killpg(owned.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for owned in reversed(self._processes):
            if owned.process.poll() is None:
                try:
                    owned.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(owned.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    owned.process.wait(timeout=10)
            owned.log_handle.close()


def start_application_services(
    owned: OwnedProcessSet,
    *,
    environment: Mapping[str, str],
    output_dir: Path,
    npm_path: Path,
    python_executable: Path,
    expected_commit: str,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Build immutable frontend assets and start only owned app processes."""

    occupied = [
        str(port) for port in (8000, 5173) if port_is_bound(port)
    ]
    if occupied:
        raise PreflightRunnerError(
            "application port(s) already in use; use --no-start-services "
            "only if those processes are the intended clean deployment"
        )
    frontend_environment = dict(environment)
    frontend_environment["PATH"] = (
        str(npm_path.parent)
        + os.pathsep
        + frontend_environment.get("PATH", "")
    )
    build_log = _open_private_log(output_dir / "frontend-build.log")
    try:
        try:
            build = run(
                [str(npm_path), "run", "build"],
                cwd=str(REPOSITORY_ROOT / "frontend"),
                env=frontend_environment,
                stdin=subprocess.DEVNULL,
                stdout=build_log,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PreflightRunnerError(
                "immutable frontend build could not be executed; inspect "
                "frontend-build.log"
            ) from exc
    finally:
        build_log.close()
    if build.returncode != 0:
        raise PreflightRunnerError(
            "immutable frontend build failed; inspect frontend-build.log"
        )
    index_path = REPOSITORY_ROOT / "frontend" / "dist" / "index.html"
    if not index_path.is_file() or index_path.stat().st_size <= 0:
        raise PreflightRunnerError(
            "immutable frontend build did not produce dist/index.html"
        )
    # Vite snapshots Git provenance while evaluating its build configuration.
    # A clean, unchanged checkout on both sides of that build binds its
    # compile-time constants to the expected commit.
    require_clean_repository(expected_commit=expected_commit)

    owned.start(
        "backend",
        [
            str(python_executable),
            "-m",
            "uvicorn",
            "main:app",
            "--app-dir",
            "backend",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=REPOSITORY_ROOT,
        environment=environment,
        log_path=output_dir / "backend.log",
    )
    owned.start(
        "frontend",
        [
            str(npm_path),
            "run",
            "preview",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            "5173",
            "--strictPort",
        ],
        cwd=REPOSITORY_ROOT / "frontend",
        environment=frontend_environment,
        log_path=output_dir / "frontend.log",
    )


class _ScriptSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "script":
            return
        attributes = dict(attrs)
        source = attributes.get("src")
        if source:
            self.sources.append(source)


def validate_served_frontend_provenance(
    client: LocalHttpClient,
    dashboard_url: str,
    expected_commit: str,
) -> str:
    """Reject dev/HMR pages and production bundles from another commit."""

    page_url = urllib.parse.urldefrag(dashboard_url).url
    try:
        html_bytes = client.request(page_url, timeout_seconds=5)
        html = html_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError, urllib.error.URLError) as exc:
        raise PreflightRunnerError(
            "served frontend provenance could not be inspected"
        ) from exc
    if "/@vite/client" in html or "/@react-refresh" in html:
        raise PreflightRunnerError(
            "served frontend is a mutable Vite development/HMR page"
        )
    parser = _ScriptSourceParser()
    parser.feed(html)
    if not parser.sources:
        raise PreflightRunnerError(
            "served production frontend has no script bundle"
        )
    expected_bytes = expected_commit.encode("ascii")
    clean_provenance_pattern = re.compile(
        rb"commit\s*:\s*['\"]"
        + re.escape(expected_bytes)
        + rb"['\"]\s*,\s*dirty\s*:\s*(?:!1|false)"
    )
    found_clean_provenance = False
    fingerprint = hashlib.sha256()
    fingerprint.update(page_url.encode("utf-8"))
    fingerprint.update(b"\0")
    fingerprint.update(html_bytes)
    page_origin = urllib.parse.urlsplit(page_url)
    for source in parser.sources:
        script_url = urllib.parse.urljoin(page_url, source)
        parsed_script = urllib.parse.urlsplit(script_url)
        if (
            parsed_script.scheme != "http"
            or parsed_script.hostname != page_origin.hostname
            or parsed_script.port != page_origin.port
        ):
            raise PreflightRunnerError(
                "served frontend references a nonlocal script bundle"
            )
        try:
            script = client.request(script_url, timeout_seconds=10)
        except (OSError, urllib.error.URLError) as exc:
            raise PreflightRunnerError(
                "served frontend script bundle could not be inspected"
            ) from exc
        if b"/@vite/client" in script or b"/@react-refresh" in script:
            raise PreflightRunnerError(
                "served frontend script contains Vite development/HMR code"
            )
        found_clean_provenance = (
            found_clean_provenance
            or clean_provenance_pattern.search(script) is not None
        )
        fingerprint.update(b"\0")
        fingerprint.update(script_url.encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(script)
    if not found_clean_provenance:
        raise PreflightRunnerError(
            "served frontend bundle provenance is dirty or disagrees with "
            "the current repository commit"
        )
    return fingerprint.hexdigest()


def wait_for_http_ok(
    client: LocalHttpClient,
    url: str,
    *,
    timeout_seconds: float,
    description: str,
    owned: OwnedProcessSet | None = None,
) -> bytes:
    def ready() -> bytes | None:
        if owned is not None:
            owned.require_alive()
        try:
            return client.request(url, timeout_seconds=2)
        except (OSError, urllib.error.URLError):
            return None

    return wait_for(
        ready,
        timeout_seconds=timeout_seconds,
        description=description,
        interval_seconds=0.5,
    )


def riva_health_urls(environment: Mapping[str, str]) -> tuple[str, ...]:
    ports: list[int] = []
    for variable, default in (
        ("ASR_HTTP_PORT", "9002"),
        ("NMT_HTTP_PORT", "9001"),
        ("TTS_HTTP_PORT", "9003"),
    ):
        raw = environment.get(variable, default)
        try:
            port = int(raw)
        except (TypeError, ValueError) as exc:
            raise PreflightRunnerError(
                f"{variable} must name a valid local TCP port"
            ) from exc
        if not 1 <= port <= 65535:
            raise PreflightRunnerError(
                f"{variable} must name a valid local TCP port"
            )
        ports.append(port)
    return tuple(
        f"http://127.0.0.1:{port}/v1/health/ready" for port in ports
    )


def find_chrome(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    candidates.extend(
        [
            Path("/opt/google/chrome/chrome"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
        ]
    )
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser"):
        discovered = shutil.which(name)
        if discovered:
            candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise PreflightRunnerError(
        "a Chrome/Chromium executable was not found; pass --chrome"
    )


class CDP:
    """Minimal synchronous Chrome DevTools Protocol client."""

    def __init__(self, websocket_url: str) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise PreflightRunnerError(
                "the pinned websockets dependency is unavailable"
            ) from exc
        try:
            self.socket = connect(
                websocket_url,
                open_timeout=15,
                close_timeout=5,
                origin="http://localhost",
                proxy=None,
            )
        except Exception as exc:
            raise PreflightRunnerError(
                "could not connect to the local Chrome debugging endpoint"
            ) from exc
        self.next_id = 1

    def close(self) -> None:
        self.socket.close()

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.socket.send(
            json.dumps(
                {
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                },
                separators=(",", ":"),
            )
        )
        while True:
            try:
                response = json.loads(self.socket.recv(timeout=30))
            except Exception as exc:
                raise PreflightRunnerError(
                    f"Chrome stopped responding during {method}"
                ) from exc
            if not isinstance(response, Mapping):
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise PreflightRunnerError(
                    f"Chrome rejected the {method} command"
                )
            result = response.get("result", {})
            return result if isinstance(result, Mapping) else {}

    def evaluate(self, expression: str, *, user_gesture: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": user_gesture,
            },
        )
        if "exceptionDetails" in result:
            raise PreflightRunnerError(
                "the dashboard raised an exception during browser automation"
            )
        remote_value = result.get("result", {})
        if not isinstance(remote_value, Mapping):
            return None
        return remote_value.get("value")


def _chrome_debugging_port(
    chrome: subprocess.Popen[Any],
    profile_dir: Path,
    *,
    timeout_seconds: float = 20,
) -> int:
    active_port_file = profile_dir / "DevToolsActivePort"

    def read_port() -> int | None:
        if chrome.poll() is not None:
            raise PreflightRunnerError(
                "Chrome exited before its debugging endpoint was ready"
            )
        try:
            first_line = active_port_file.read_text(
                encoding="utf-8"
            ).splitlines()[0]
            port = int(first_line)
        except (FileNotFoundError, IndexError, ValueError, OSError):
            return None
        return port if 1 <= port <= 65535 else None

    return wait_for(
        read_port,
        timeout_seconds=timeout_seconds,
        description="Chrome debugging endpoint",
    )


def start_chrome(
    *,
    chrome_path: Path,
    profile_dir: Path,
    output_dir: Path,
    popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> tuple[subprocess.Popen[Any], Any]:
    log_handle = _open_private_log(output_dir / "chrome.log")
    try:
        process = popen(
            [
                str(chrome_path),
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--autoplay-policy=no-user-gesture-required",
                "--remote-allow-origins=*",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile_dir}",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        log_handle.close()
        raise
    return process, log_handle


def stop_owned_process(
    process: subprocess.Popen[Any] | None,
    log_handle: Any | None,
) -> None:
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
    if log_handle is not None:
        log_handle.close()


class DashboardDriver:
    """Selector-stable automation for the Test Dashboard evidence mode."""

    PHASE_SELECTOR = '[data-s2s-phase]'
    FILE_SELECTOR = '[data-s2s-control="audio-file"]'
    START_SELECTOR = '[data-s2s-control="start-test"]'
    CAPTURE_SELECTOR = '[data-s2s-control="rendered-digital-capture"]'
    EXPORT_SELECTOR = '[data-s2s-control="export-evidence"]'

    def __init__(
        self,
        cdp: CDP,
        *,
        chrome_process: subprocess.Popen[Any],
    ) -> None:
        self.cdp = cdp
        self.chrome_process = chrome_process

    def _require_chrome(self) -> None:
        status = self.chrome_process.poll()
        if status is not None:
            raise PreflightRunnerError(
                f"Chrome exited early with status {status}; inspect chrome.log"
            )

    def state(self) -> Mapping[str, Any]:
        self._require_chrome()
        value = self.cdp.evaluate(
            "(() => {"
            f"const root=document.querySelector('{self.PHASE_SELECTOR}');"
            "return {"
            "phase:root?.getAttribute('data-s2s-phase')||null,"
            "alert:Boolean(document.querySelector('[role=\"alert\"]')),"
            "devClient:Boolean(document.querySelector("
            "'script[src*=\"/@vite/client\"],script[src*=\"/@react-refresh\"]'"
            "))"
            "};"
            "})()"
        )
        state = value if isinstance(value, Mapping) else {}
        if state.get("devClient") is True:
            raise PreflightRunnerError(
                "dashboard is running through mutable Vite development/HMR"
            )
        return state

    def _fail_on_alert(self, state: Mapping[str, Any]) -> None:
        if state.get("alert") is True:
            raise PreflightRunnerError(
                "the dashboard displayed an alert; its text was omitted from "
                "automation logs for privacy"
            )

    def wait_until_ready(self, timeout_seconds: float) -> None:
        wait_for(
            lambda: self.cdp.evaluate(
                "document.readyState === 'complete'"
            ),
            timeout_seconds=timeout_seconds,
            description="Test Dashboard document",
        )

        def application_ready() -> bool:
            state = self.state()
            self._fail_on_alert(state)
            return (
                state.get("phase") == "idle"
                and self.cdp.evaluate(
                    f"Boolean(document.querySelector('{self.FILE_SELECTOR}'))"
                )
                is True
            )

        wait_for(
            application_ready,
            timeout_seconds=timeout_seconds,
            description="Test Dashboard application",
        )

    def configure_and_upload(
        self,
        audio_path: Path,
        *,
        timeout_seconds: float,
    ) -> None:
        configured = self.cdp.evaluate(
            "(() => {"
            f"const capture=document.querySelector('{self.CAPTURE_SELECTOR}');"
            "const adaptive=[...document.querySelectorAll("
            "'input[type=\"checkbox\"]')].find(input=>"
            "input.closest('label')?.innerText.includes("
            "'Adaptive Spanish playback'));"
            "if(!capture||!adaptive)return false;"
            "if(!adaptive.checked)adaptive.click();"
            "if(!capture.checked)capture.click();"
            "return adaptive.checked===true&&capture.checked===true;"
            "})()",
            user_gesture=True,
        )
        if configured is not True:
            raise PreflightRunnerError(
                "could not enable adaptive playback and rendered capture"
            )

        document = self.cdp.call(
            "DOM.getDocument", {"depth": -1, "pierce": True}
        )
        root = document.get("root", {})
        if not isinstance(root, Mapping) or not isinstance(
            root.get("nodeId"), int
        ):
            raise PreflightRunnerError(
                "Chrome did not expose the dashboard document"
            )
        query = self.cdp.call(
            "DOM.querySelector",
            {
                "nodeId": root["nodeId"],
                "selector": self.FILE_SELECTOR,
            },
        )
        node_id = query.get("nodeId")
        if not isinstance(node_id, int) or node_id <= 0:
            raise PreflightRunnerError(
                "the dashboard audio file input was not found"
            )
        self.cdp.call(
            "DOM.setFileInputFiles",
            {"nodeId": node_id, "files": [str(audio_path)]},
        )
        dispatched = self.cdp.evaluate(
            "(() => {"
            f"const input=document.querySelector('{self.FILE_SELECTOR}');"
            "if(!input)return false;"
            "input.dispatchEvent(new Event('change',{bubbles:true}));"
            "return true;"
            "})()"
        )
        if dispatched is not True:
            raise PreflightRunnerError(
                "could not notify the dashboard of the selected audio file"
            )

        def decoded() -> bool:
            state = self.state()
            self._fail_on_alert(state)
            return self.cdp.evaluate(
                "(() => {"
                f"const start=document.querySelector('{self.START_SELECTOR}');"
                f"const capture=document.querySelector('{self.CAPTURE_SELECTOR}');"
                "const adaptive=[...document.querySelectorAll("
                "'input[type=\"checkbox\"]')].find(input=>"
                "input.closest('label')?.innerText.includes("
                "'Adaptive Spanish playback'));"
                "return Boolean(start&&!start.disabled&&capture?.checked&&"
                "adaptive?.checked);"
                "})()"
            ) is True

        wait_for(
            decoded,
            timeout_seconds=timeout_seconds,
            description="exact preflight audio decode",
            interval_seconds=0.25,
        )

    def start(self) -> None:
        state = self.state()
        self._fail_on_alert(state)
        if state.get("phase") != "idle":
            raise PreflightRunnerError(
                "dashboard was not idle immediately before capture"
            )
        clicked = self.cdp.evaluate(
            "(() => {"
            f"const button=document.querySelector('{self.START_SELECTOR}');"
            "if(!button||button.disabled)return false;"
            "button.click();return true;"
            "})()",
            user_gesture=True,
        )
        if clicked is not True:
            raise PreflightRunnerError("could not start the dashboard capture")

    def wait_for_completed(self, timeout_seconds: float) -> None:
        def terminal() -> bool:
            state = self.state()
            self._fail_on_alert(state)
            phase = state.get("phase")
            if phase == "failed":
                raise PreflightRunnerError(
                    "the dashboard entered its failed phase"
                )
            return phase == "completed"

        wait_for(
            terminal,
            timeout_seconds=timeout_seconds,
            description="completed rendered-digital capture and queue drain",
            interval_seconds=1,
        )
        final_state = self.state()
        self._fail_on_alert(final_state)
        if final_state.get("phase") != "completed":
            raise PreflightRunnerError(
                "dashboard did not remain in its completed phase"
            )

    def export(self) -> None:
        state = self.state()
        self._fail_on_alert(state)
        if state.get("phase") != "completed":
            raise PreflightRunnerError(
                "dashboard evidence export requires completed phase"
            )
        clicked = self.cdp.evaluate(
            "(() => {"
            f"const button=document.querySelector('{self.EXPORT_SELECTOR}');"
            "if(!button||button.disabled)return false;"
            "button.click();return true;"
            "})()",
            user_gesture=True,
        )
        if clicked is not True:
            raise PreflightRunnerError(
                "could not request the four-file evidence export"
            )


def scan_evidence_bundle(output_dir: Path) -> EvidenceBundle | None:
    """Return an exact complete bundle, rejecting duplicates and extras."""

    output_files = [path for path in output_dir.iterdir() if path.is_file()]
    # Chrome can briefly use an implementation-defined temporary basename
    # before committing the server-provided download name.
    if any(path.name.endswith(".crdownload") for path in output_files):
        return None
    unexpected_outputs = [
        path
        for path in output_files
        if (
            path.name not in OPERATIONAL_OUTPUT_FILES
            and not path.name.startswith(ARTIFACT_PREFIX)
        )
    ]
    if unexpected_outputs:
        raise PreflightRunnerError(
            "evidence export produced an unexpected download"
        )
    candidates = [
        path
        for path in output_files
        if path.name.startswith(ARTIFACT_PREFIX)
    ]
    recognized: dict[str, list[Path]] = {
        suffix: [] for suffix in ARTIFACT_SUFFIXES
    }
    extras: list[Path] = []
    for path in candidates:
        matching = [
            suffix for suffix in ARTIFACT_SUFFIXES
            if path.name.endswith(suffix)
        ]
        if len(matching) != 1:
            extras.append(path)
            continue
        recognized[matching[0]].append(path)
    if extras or any(len(paths) > 1 for paths in recognized.values()):
        raise PreflightRunnerError(
            "evidence export produced extra or duplicate artifacts"
        )
    if any(len(paths) == 0 for paths in recognized.values()):
        return None
    paths_by_suffix = {
        suffix: paths[0] for suffix, paths in recognized.items()
    }
    if any(path.stat().st_size <= 0 for path in paths_by_suffix.values()):
        return None
    base_names = {
        path.name[: -len(suffix)]
        for suffix, path in paths_by_suffix.items()
    }
    if len(base_names) != 1:
        raise PreflightRunnerError(
            "evidence artifacts do not share one timestamp base"
        )
    base_name = base_names.pop()
    return EvidenceBundle(
        base_name=base_name,
        manifest=paths_by_suffix[".manifest.json"],
        wav=paths_by_suffix[".stereo.wav"],
        blocks=paths_by_suffix[".blocks.csv"],
        timing_csv=paths_by_suffix[".timing.csv"],
    )


def wait_for_evidence_bundle(
    output_dir: Path,
    driver: DashboardDriver,
    *,
    timeout_seconds: float,
) -> EvidenceBundle:
    stable_signature: tuple[tuple[str, int], ...] | None = None

    def complete() -> EvidenceBundle | None:
        nonlocal stable_signature
        state = driver.state()
        driver._fail_on_alert(state)
        if state.get("phase") != "completed":
            raise PreflightRunnerError(
                "dashboard left completed phase during evidence export"
            )
        bundle = scan_evidence_bundle(output_dir)
        if bundle is None:
            stable_signature = None
            return None
        signature = tuple(
            sorted(
                (path.name, path.stat().st_size)
                for path in (
                    bundle.manifest,
                    bundle.wav,
                    bundle.blocks,
                    bundle.timing_csv,
                )
            )
        )
        if signature != stable_signature:
            stable_signature = signature
            return None
        return bundle

    return wait_for(
        complete,
        timeout_seconds=timeout_seconds,
        description="exact four-file evidence download",
        interval_seconds=0.5,
    )


def run_validator(
    bundle: EvidenceBundle,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    python_executable: Path = Path(sys.executable),
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    report = bundle.manifest.with_name(f"{bundle.base_name}.report.json")
    command = [
        str(python_executable),
        str(repository_root / "analyze_rendered_digital_preflight.py"),
        "--manifest",
        str(bundle.manifest),
        "--wav",
        str(bundle.wav),
        "--blocks",
        str(bundle.blocks),
        "--timing-csv",
        str(bundle.timing_csv),
        "--output",
        str(report),
    ]
    try:
        completed = run(
            command,
            cwd=str(repository_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightRunnerError(
            "offline validator could not be executed"
        ) from exc
    if completed.returncode != 0:
        raise ValidatorFailure(completed.returncode, report)
    if not report.is_file() or report.stat().st_size <= 0:
        raise PreflightRunnerError(
            "offline validator returned success without a report"
        )
    try:
        status = json.loads(report.read_text(encoding="utf-8")).get("status")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise PreflightRunnerError(
            "offline validator produced an unreadable report"
        ) from exc
    if status != "PASS":
        raise PreflightRunnerError(
            "offline validator returned zero without a PASS report"
        )
    return report


def _new_output_directory(requested: Path | None) -> Path:
    if requested is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        requested = (
            Path.home()
            / "private-s2s-evidence"
            / f"rendered-digital-run-{timestamp}"
        )
    path = requested.expanduser().resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError:
        pass
    else:
        raise PreflightRunnerError(
            "private evidence output must be outside the repository"
        )
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise PreflightRunnerError(
            "output directory already exists; choose a new directory"
        ) from exc
    except OSError as exc:
        raise PreflightRunnerError(
            "private output directory could not be created"
        ) from exc
    return path


def _new_profile_directory() -> Path:
    path = Path(tempfile.mkdtemp(prefix="s2s-common-clock-chrome-"))
    path.chmod(0o700)
    return path


def _json_target(
    client: LocalHttpClient,
    debugging_base_url: str,
) -> Mapping[str, Any]:
    payload = client.json(
        f"{debugging_base_url}/json/new?"
        f"{urllib.parse.quote('about:blank', safe='')}",
        method="PUT",
        timeout_seconds=5,
    )
    websocket_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(websocket_url, str):
        raise PreflightRunnerError(
            "Chrome did not return a page debugging endpoint"
        )
    return payload


def _run(args: argparse.Namespace) -> Path:
    ensure_local_http_url(args.dashboard_url, label="dashboard URL")
    ensure_local_http_url(args.api_config_url, label="API config URL")
    commit = require_clean_repository()

    audio_path = args.audio.expanduser().resolve(strict=True)
    if sha256_file(audio_path) != PREFLIGHT_FILE_SHA256:
        raise PreflightRunnerError(
            "audio input is not the registered exact 60-second fixture"
        )

    output_dir = _new_output_directory(args.output_dir)
    print(f"Repository commit: {commit}")
    print(f"Private output directory: {output_dir}")

    loaded_environment: dict[str, str] = {}
    if args.env_file is not None:
        env_path = args.env_file.expanduser()
        if env_path.exists() or args.env_file_was_explicit:
            loaded_environment = parse_env_file(env_path)
    environment = build_child_environment(
        os.environ, loaded_environment
    )

    http = LocalHttpClient()
    owned = OwnedProcessSet()
    chrome: subprocess.Popen[Any] | None = None
    chrome_log: Any | None = None
    cdp: CDP | None = None
    profile_dir: Path | None = None
    try:
        for index, health_url in enumerate(
            riva_health_urls(environment), start=1
        ):
            wait_for_http_ok(
                http,
                health_url,
                timeout_seconds=args.service_timeout_seconds,
                description=f"Riva service {index} readiness",
            )

        if not args.no_start_services:
            npm_path = find_npm(args.npm)
            start_application_services(
                owned,
                environment=environment,
                output_dir=output_dir,
                npm_path=npm_path,
                python_executable=Path(sys.executable),
                expected_commit=commit,
            )

        wait_for_http_ok(
            http,
            args.api_config_url,
            timeout_seconds=args.service_timeout_seconds,
            description="FastAPI configuration endpoint",
            owned=owned if not args.no_start_services else None,
        )
        wait_for_http_ok(
            http,
            urllib.parse.urldefrag(args.dashboard_url).url,
            timeout_seconds=args.service_timeout_seconds,
            description="Vite dashboard",
            owned=owned if not args.no_start_services else None,
        )
        frontend_fingerprint = validate_served_frontend_provenance(
            http, args.dashboard_url, commit
        )
        config = http.json(args.api_config_url, timeout_seconds=5)
        validate_api_config(config, commit)
        docker_attestation = attest_docker_runtime(
            environment,
            config,
        )
        docker_identity = docker_attestation.identity_sha256()
        docker_attestation_path = output_dir / "docker-attestation.json"
        write_docker_attestation(
            docker_attestation_path,
            docker_attestation,
            repository_commit=commit,
            phase="verified_pre_capture",
        )
        require_clean_repository(expected_commit=commit)

        profile_dir = _new_profile_directory()
        chrome_path = find_chrome(args.chrome)
        chrome, chrome_log = start_chrome(
            chrome_path=chrome_path,
            profile_dir=profile_dir,
            output_dir=output_dir,
        )
        debugging_port = _chrome_debugging_port(chrome, profile_dir)
        debugging_base_url = f"http://127.0.0.1:{debugging_port}"
        wait_for_http_ok(
            http,
            f"{debugging_base_url}/json/version",
            timeout_seconds=20,
            description="Chrome DevTools HTTP endpoint",
        )
        target = _json_target(http, debugging_base_url)
        cdp = CDP(str(target["webSocketDebuggerUrl"]))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("DOM.enable")
        try:
            cdp.call(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(output_dir),
                    "eventsEnabled": True,
                },
            )
        except PreflightRunnerError:
            cdp.call(
                "Page.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(output_dir),
                },
            )
        cdp.call("Page.navigate", {"url": args.dashboard_url})
        driver = DashboardDriver(cdp, chrome_process=chrome)
        driver.wait_until_ready(args.service_timeout_seconds)
        driver.configure_and_upload(
            audio_path, timeout_seconds=args.decode_timeout_seconds
        )

        # Re-attest immediately before the only operation that starts capture.
        require_clean_repository(expected_commit=commit)
        if (
            validate_served_frontend_provenance(
                http, args.dashboard_url, commit
            )
            != frontend_fingerprint
        ):
            raise PreflightRunnerError(
                "served production frontend changed before capture"
            )
        current_config = http.json(
            args.api_config_url,
            timeout_seconds=5,
        )
        validate_api_config(current_config, commit)
        if (
            attest_docker_runtime(
                environment,
                current_config,
            ).identity_sha256()
            != docker_identity
        ):
            raise PreflightRunnerError(
                "Docker model container identity changed before capture"
            )
        driver.start()
        print("Capture started; waiting for server completion and queue drain.")
        driver.wait_for_completed(args.timeout_seconds)
        require_clean_repository(expected_commit=commit)
        driver.export()
        bundle = wait_for_evidence_bundle(
            output_dir,
            driver,
            timeout_seconds=args.download_timeout_seconds,
        )
        require_clean_repository(expected_commit=commit)
        if (
            validate_served_frontend_provenance(
                http, args.dashboard_url, commit
            )
            != frontend_fingerprint
        ):
            raise PreflightRunnerError(
                "served production frontend changed during capture"
            )
        current_config = http.json(
            args.api_config_url,
            timeout_seconds=5,
        )
        validate_api_config(current_config, commit)
        final_docker_attestation = attest_docker_runtime(
            environment,
            current_config,
        )
        if final_docker_attestation.identity_sha256() != docker_identity:
            raise PreflightRunnerError(
                "Docker model container identity changed during capture"
            )
        write_docker_attestation(
            docker_attestation_path,
            final_docker_attestation,
            repository_commit=commit,
            phase="verified_post_capture",
            browser_bundle_manifest_sha256=sha256_file(bundle.manifest),
        )
        report = run_validator(bundle)
        print(f"PASS: {report}")
        return report
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except Exception:
                pass
        stop_owned_process(chrome, chrome_log)
        if profile_dir is not None:
            shutil.rmtree(profile_dir, ignore_errors=True)
        owned.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run and validate one private rendered-digital common-clock "
            "preflight"
        )
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=DEFAULT_AUDIO,
        help="exact 60-second WAV fixture (default: test_audio/preflight.wav)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "new private directory outside the repository; defaults below "
            "~/private-s2s-evidence"
        ),
    )
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--api-config-url", default=DEFAULT_API_CONFIG_URL)
    environment_group = parser.add_mutually_exclusive_group()
    environment_group.add_argument(
        "--env-file",
        type=Path,
        default=REPOSITORY_ROOT / ".env",
        help="dotenv configuration loaded without shell execution",
    )
    environment_group.add_argument(
        "--no-env-file",
        action="store_true",
        help="do not load a dotenv file",
    )
    parser.add_argument(
        "--no-start-services",
        action="store_true",
        help=(
            "use already-running clean localhost FastAPI and immutable "
            "production-preview processes"
        ),
    )
    parser.add_argument("--npm", type=Path, help="Node.js 22+ npm executable")
    parser.add_argument("--chrome", type=Path, help="Chrome executable")
    parser.add_argument(
        "--service-timeout-seconds", type=float, default=180
    )
    parser.add_argument("--decode-timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=600,
        help=(
            "runner wait ceiling for completion (does not extend the "
            "browser's registered 300-second drain or 420-second capture "
            "limits)"
        ),
    )
    parser.add_argument(
        "--download-timeout-seconds", type=float, default=60
    )
    args = parser.parse_args(argv)
    parsed_argv = list(sys.argv[1:] if argv is None else argv)
    args.env_file_was_explicit = any(
        item == "--env-file" or item.startswith("--env-file=")
        for item in parsed_argv
    )
    if args.no_env_file:
        args.env_file = None
    for name in (
        "service_timeout_seconds",
        "decode_timeout_seconds",
        "timeout_seconds",
        "download_timeout_seconds",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    previous_umask = os.umask(0o077)
    try:
        args = parse_args(argv)
        _run(args)
        return 0
    except ValidatorFailure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.return_code if exc.return_code in {1, 2} else 2
    except PreflightRunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("ERROR: a required local file is missing", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: preflight interrupted; private output retained", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            "ERROR: unexpected preflight failure "
            f"({type(exc).__name__}); private output retained",
            file=sys.stderr,
        )
        return 2
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
