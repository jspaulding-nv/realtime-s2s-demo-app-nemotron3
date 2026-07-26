import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import run_rendered_digital_preflight as runner


COMMIT = "a" * 40


def valid_api_config():
    return {
        "pipelineMode": "staged",
        "audioMetadataProtocolVersions": [1],
        "repositoryProvenance": {
            "commit": COMMIT,
            "dirty": False,
        },
        "modelConfig": {
            "asr": {
                "imageDigest": runner.APPROVED_ASR_IMAGE_DIGEST,
                "profile": (
                    "name=nemotron-asr-streaming,type=en-US,batch_size=32"
                ),
                "eouMs": 800,
                "wordTimeOffsets": True,
                "sourceLanguage": "en-US",
            },
            "nmt": {
                "imageDigest": runner.APPROVED_NMT_IMAGE_DIGEST,
                "model": "megatronnmt_any_any_1b",
                "sourceLanguage": "en-US",
                "targetLanguage": "es-US",
            },
            "tts": {
                "imageDigest": runner.APPROVED_TTS_IMAGE_DIGEST,
                "profile": "name=magpie-tts-multilingual,batch_size=8",
                "voice": "Magpie-Multilingual.ES-US.Isabela",
                "targetLanguage": "es-US",
            },
        },
        "stagedConfig": {
            "telemetrySchemaVersion": 3,
            "segmentMaxChars": 240,
            "segmentMaxAgeMs": 2000,
            "asrEventQueueMaxSize": 32,
            "nmtQueueMaxSize": 4,
            "ttsQueueMaxSize": 4,
            "outputQueueMaxSize": 4,
            "nmtRpcTimeoutSeconds": 15.0,
            "ttsRpcTimeoutSeconds": 60.0,
            "ttsMaxSegmentAudioSeconds": 60.0,
            "ttsMaxRetries": 1,
            "ttsResponseChunkTelemetryEnabled": False,
            "ttsSubsegmentMaxChars": 0,
            "ttsSubsegmentMinChars": 12,
            "ttsIncrementalAtomicFallbackMaxChars": 4,
            "closeTimeoutSeconds": 10.0,
            "ttsIncrementalPublishEnabled": True,
            "ttsIncrementalFrameMs": 500,
        },
    }


def write_bundle(directory, base="rendered-digital-preflight-stamp"):
    paths = {}
    for suffix in runner.ARTIFACT_SUFFIXES:
        path = directory / f"{base}{suffix}"
        path.write_bytes(f"private-{suffix}".encode())
        paths[suffix] = path
    return paths


def completed_process(stdout="", returncode=0):
    return subprocess.CompletedProcess(
        args=["test"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


class DockerFixture:
    def __init__(self):
        self.calls = []
        self.fail_commands = False
        self.role_container_ids = {
            "asr": "a" * 64,
            "nmt": "b" * 64,
            "tts": "c" * 64,
        }
        self.role_image_ids = {
            "asr": f"sha256:{'1' * 64}",
            "nmt": f"sha256:{'2' * 64}",
            "tts": f"sha256:{'3' * 64}",
        }
        self.port_ids = {}
        self.containers = {}
        self.images = {}
        for spec in runner.DOCKER_ROLE_SPECS:
            container_id = self.role_container_ids[spec.role]
            image_id = self.role_image_ids[spec.role]
            self.port_ids[spec.http_host_port_default] = [container_id]
            self.port_ids[spec.grpc_host_port_default] = [container_id]
            self.containers[container_id] = {
                "image_reference": f"{spec.image_repository}:pinned",
                "image_id": image_id,
                "running": True,
                "status": "running",
                "health": "healthy",
                "ports": {
                    "9000/tcp": [
                        {
                            "HostIp": "0.0.0.0",
                            "HostPort": str(spec.http_host_port_default),
                        },
                        {
                            "HostIp": "::",
                            "HostPort": str(spec.http_host_port_default),
                        },
                    ],
                    f"{spec.grpc_container_port}/tcp": [
                        {
                            "HostIp": "0.0.0.0",
                            "HostPort": str(spec.grpc_host_port_default),
                        },
                    ],
                },
            }
            self.images[image_id] = {
                "repo_digests": [
                    f"{spec.image_repository}@{spec.approved_digest}"
                ],
            }

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.fail_commands:
            return completed_process(returncode=1)
        assert command[0] == "docker"
        operation = tuple(command[1:3])
        if operation == ("container", "ls"):
            publish = next(
                item for item in command if item.startswith("publish=")
            )
            port = int(publish.split("=", 1)[1])
            values = self.port_ids.get(port, [])
            output = "".join(f"{value}\n" for value in values)
            return completed_process(output)
        if operation == ("container", "inspect"):
            ids = command[command.index("--format") + 2 :]
            rows = []
            for container_id in ids:
                value = self.containers[container_id]
                rows.append(
                    "\t".join(
                        json.dumps(field)
                        for field in (
                            container_id,
                            value["image_reference"],
                            value["image_id"],
                            value["running"],
                            value["status"],
                            value["health"],
                            value["ports"],
                        )
                    )
                )
            return completed_process("\n".join(rows) + "\n")
        if operation == ("image", "inspect"):
            ids = command[command.index("--format") + 2 :]
            rows = []
            for image_id in ids:
                value = self.images[image_id]
                rows.append(
                    "\t".join(
                        json.dumps(field)
                        for field in (
                            image_id,
                            value["repo_digests"],
                        )
                    )
                )
            return completed_process("\n".join(rows) + "\n")
        raise AssertionError(f"unexpected Docker command: {command}")


def test_sha256_file_streams_exact_bytes(tmp_path):
    value = b"private-pcm" * 100
    path = tmp_path / "fixture.wav"
    path.write_bytes(value)

    assert runner.sha256_file(path, chunk_bytes=7) == hashlib.sha256(
        value
    ).hexdigest()


def test_parse_env_file_is_non_executing_and_supports_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# ignored",
                "export SIMPLE=value",
                'QUOTED="two words"',
                "HASH=abc#literal",
                "COMMENTED=value # comment",
                "EMPTY=",
                "NOT_EXECUTED=$(id)",
            ]
        ),
        encoding="utf-8",
    )

    assert runner.parse_env_file(env_file) == {
        "SIMPLE": "value",
        "QUOTED": "two words",
        "HASH": "abc#literal",
        "COMMENTED": "value",
        "EMPTY": "",
        "NOT_EXECUTED": "$(id)",
    }


@pytest.mark.parametrize(
    "contents",
    [
        "MISSING_EQUALS",
        "1INVALID=value",
        'BROKEN="unterminated',
        "SPACES=two words",
    ],
)
def test_parse_env_file_rejects_ambiguous_assignments(tmp_path, contents):
    env_file = tmp_path / ".env"
    env_file.write_text(contents, encoding="utf-8")

    with pytest.raises(runner.PreflightRunnerError):
        runner.parse_env_file(env_file)


def test_child_environment_drops_registry_secret(tmp_path):
    environment = runner.build_child_environment(
        {"PATH": "/bin", "PYTHONPATH": "/existing"},
        {"NGC_API_KEY": "secret", "S2S_PIPELINE_MODE": "staged"},
        repository_root=tmp_path,
    )

    assert "NGC_API_KEY" not in environment
    assert environment["S2S_PIPELINE_MODE"] == "staged"
    assert environment["PYTHONPATH"].split(":") == [
        str(tmp_path / ".python-packages"),
        "/existing",
    ]


def test_require_clean_repository_returns_full_commit(tmp_path):
    responses = iter(
        [
            completed_process(f"{COMMIT}\n"),
            completed_process(""),
        ]
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return next(responses)

    assert runner.require_clean_repository(tmp_path, run=fake_run) == COMMIT
    assert calls[1][0][-3:] == [
        "status",
        "--porcelain",
        "--untracked-files=normal",
    ]
    assert all(call[1]["capture_output"] for call in calls)


def test_require_clean_repository_fails_closed_on_dirty_tree(tmp_path):
    responses = iter(
        [
            completed_process(f"{COMMIT}\n"),
            completed_process("?? private.wav\n"),
        ]
    )

    with pytest.raises(runner.PreflightRunnerError, match="dirty"):
        runner.require_clean_repository(
            tmp_path, run=lambda *args, **kwargs: next(responses)
        )


def test_require_clean_repository_detects_checkout_change(tmp_path):
    responses = iter(
        [
            completed_process(f"{'b' * 40}\n"),
            completed_process(""),
        ]
    )

    with pytest.raises(runner.PreflightRunnerError, match="changed"):
        runner.require_clean_repository(
            tmp_path,
            expected_commit=COMMIT,
            run=lambda *args, **kwargs: next(responses),
        )


def test_validate_api_config_accepts_registered_runtime():
    runner.validate_api_config(valid_api_config(), COMMIT)


@pytest.mark.parametrize(
    "value",
    [15, 15.0],
)
def test_validate_api_config_accepts_exact_numeric_timeout_forms(value):
    config = valid_api_config()
    config["stagedConfig"]["nmtRpcTimeoutSeconds"] = value

    runner.validate_api_config(config, COMMIT)


@pytest.mark.parametrize(
    "value",
    [True, "15", 15.001, float("nan")],
)
def test_validate_api_config_rejects_invalid_numeric_timeout_forms(value):
    config = valid_api_config()
    config["stagedConfig"]["nmtRpcTimeoutSeconds"] = value

    with pytest.raises(runner.PreflightRunnerError, match="does not match"):
        runner.validate_api_config(config, COMMIT)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("pipelineMode",), "monolithic"),
        (("repositoryProvenance", "dirty"), True),
        (("modelConfig", "asr", "eouMs"), 300),
        (("stagedConfig", "ttsIncrementalFrameMs"), 100),
    ],
)
def test_validate_api_config_rejects_runtime_drift(path, value):
    config = valid_api_config()
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(runner.PreflightRunnerError, match="does not match"):
        runner.validate_api_config(config, COMMIT)


def test_validate_api_config_requires_protocol_one():
    config = valid_api_config()
    config["audioMetadataProtocolVersions"] = [2]

    with pytest.raises(runner.PreflightRunnerError, match="protocol 1"):
        runner.validate_api_config(config, COMMIT)


def test_validate_api_config_rejects_float_protocol_alias():
    config = valid_api_config()
    config["audioMetadataProtocolVersions"] = [1.0]

    with pytest.raises(runner.PreflightRunnerError, match="protocol 1"):
        runner.validate_api_config(config, COMMIT)


def test_docker_attestation_resolves_ports_to_immutable_images():
    docker = DockerFixture()

    attestation = runner.attest_docker_runtime(
        {},
        valid_api_config(),
        run=docker,
    )

    assert [role.role for role in attestation.roles] == [
        "asr",
        "nmt",
        "tts",
    ]
    assert len(attestation.identity_sha256()) == 64
    assert {
        role.repo_digest for role in attestation.roles
    } == {
        (
            f"{spec.image_repository}@{spec.approved_digest}"
        )
        for spec in runner.DOCKER_ROLE_SPECS
    }
    operations = [tuple(call[0][1:3]) for call in docker.calls]
    assert set(operations) <= {
        ("container", "ls"),
        ("container", "inspect"),
        ("image", "inspect"),
    }
    assert all(
        word not in {"compose", "create", "pull", "restart", "start", "stop", "up"}
        for call, _ in docker.calls
        for word in call
    )


def test_docker_attestation_rejects_stale_repo_digest():
    docker = DockerFixture()
    image_id = docker.role_image_ids["asr"]
    docker.images[image_id]["repo_digests"] = [
        (
            "nvcr.io/nim/nvidia/nemotron-asr-streaming@sha256:"
            f"{'f' * 64}"
        )
    ]

    with pytest.raises(runner.PreflightRunnerError, match="stale"):
        runner.attest_docker_runtime(
            {},
            valid_api_config(),
            run=docker,
        )


def test_docker_attestation_rejects_tag_only_unresolved_image():
    docker = DockerFixture()
    docker.images[docker.role_image_ids["tts"]]["repo_digests"] = []

    with pytest.raises(runner.PreflightRunnerError, match="tag-only"):
        runner.attest_docker_runtime(
            {},
            valid_api_config(),
            run=docker,
        )


def test_docker_attestation_rejects_missing_port_container():
    docker = DockerFixture()
    docker.port_ids[9002] = []

    with pytest.raises(runner.PreflightRunnerError, match="missing"):
        runner.attest_docker_runtime(
            {},
            valid_api_config(),
            run=docker,
        )


def test_docker_attestation_rejects_duplicate_port_mapping():
    docker = DockerFixture()
    docker.port_ids[9001].append("d" * 64)

    with pytest.raises(runner.PreflightRunnerError, match="duplicated"):
        runner.attest_docker_runtime(
            {},
            valid_api_config(),
            run=docker,
        )


def test_docker_attestation_rejects_split_role_mapping():
    docker = DockerFixture()
    docker.port_ids[50053] = ["d" * 64]

    with pytest.raises(
        runner.PreflightRunnerError,
        match="different containers",
    ):
        runner.attest_docker_runtime(
            {},
            valid_api_config(),
            run=docker,
        )


def test_docker_attestation_rejects_stopped_container():
    docker = DockerFixture()
    container = docker.containers[docker.role_container_ids["nmt"]]
    container.update(
        {
            "running": False,
            "status": "exited",
            "health": "unhealthy",
        }
    )

    with pytest.raises(runner.PreflightRunnerError, match="not running"):
        runner.attest_docker_runtime(
            {},
            valid_api_config(),
            run=docker,
        )


def test_docker_attestation_rejects_mis_mapped_inspected_ports():
    docker = DockerFixture()
    container = docker.containers[docker.role_container_ids["asr"]]
    container["ports"]["9000/tcp"][0]["HostPort"] = "9012"
    container["ports"]["9000/tcp"][1]["HostPort"] = "9012"

    with pytest.raises(runner.PreflightRunnerError, match="mis-mapped"):
        runner.attest_docker_runtime(
            {},
            valid_api_config(),
            run=docker,
        )


def test_docker_attestation_rejects_backend_digest_disagreement():
    docker = DockerFixture()
    config = valid_api_config()
    config["modelConfig"]["nmt"]["imageDigest"] = f"sha256:{'f' * 64}"

    with pytest.raises(runner.PreflightRunnerError, match="backend"):
        runner.attest_docker_runtime({}, config, run=docker)


def test_docker_attestation_rejects_command_error():
    docker = DockerFixture()
    docker.fail_commands = True

    with pytest.raises(runner.PreflightRunnerError, match="command failed"):
        runner.attest_docker_runtime(
            {},
            valid_api_config(),
            run=docker,
        )


def test_docker_attestation_record_is_private_and_manifest_bound(tmp_path):
    docker = DockerFixture()
    attestation = runner.attest_docker_runtime(
        {},
        valid_api_config(),
        run=docker,
    )
    output = tmp_path / "docker-attestation.json"
    manifest_sha = "e" * 64

    runner.write_docker_attestation(
        output,
        attestation,
        repository_commit=COMMIT,
        phase="verified_post_capture",
        browser_bundle_manifest_sha256=manifest_sha,
    )

    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["schema"] == runner.DOCKER_ATTESTATION_SCHEMA
    assert record["repository_commit"] == COMMIT
    assert record["browser_bundle_manifest_sha256"] == manifest_sha
    assert set(record["roles"]) == {"asr", "nmt", "tts"}
    serialized = output.read_text(encoding="utf-8").lower()
    for forbidden in (
        "ngc_api_key",
        "environment",
        "container_name",
        "compose_project",
        "secret",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:5173/#/test",
        "http://example.test:5173/#/test",
        "http://user:password@localhost:5173/#/test",
    ],
)
def test_local_url_check_rejects_nonlocal_or_credentialed_urls(url):
    with pytest.raises(runner.PreflightRunnerError):
        runner.ensure_local_http_url(url, label="dashboard URL")


def test_local_url_check_accepts_loopback():
    runner.ensure_local_http_url(
        "http://127.0.0.1:5173/#/test", label="dashboard URL"
    )
    runner.ensure_local_http_url(
        "http://localhost:5173/#/test", label="dashboard URL"
    )


def test_wait_for_uses_injected_clock_without_real_sleep():
    now = [0.0]
    attempts = [0]

    def predicate():
        attempts[0] += 1
        return "ready" if attempts[0] == 3 else None

    def sleep(seconds):
        now[0] += seconds

    assert runner.wait_for(
        predicate,
        timeout_seconds=10,
        description="test",
        interval_seconds=1,
        monotonic=lambda: now[0],
        sleep=sleep,
    ) == "ready"


def test_riva_health_urls_use_configured_host_ports():
    assert runner.riva_health_urls(
        {
            "ASR_HTTP_PORT": "19002",
            "NMT_HTTP_PORT": "19001",
            "TTS_HTTP_PORT": "19003",
        }
    ) == (
        "http://127.0.0.1:19002/v1/health/ready",
        "http://127.0.0.1:19001/v1/health/ready",
        "http://127.0.0.1:19003/v1/health/ready",
    )


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def request(self, url, *, timeout_seconds):
        self.requests.append((url, timeout_seconds))
        return self.responses[url]


def test_served_frontend_provenance_requires_production_commit():
    page_url = "http://localhost:5173/"
    script_url = "http://localhost:5173/assets/app.js"
    client = FakeHttpClient(
        {
            page_url: b'<script type="module" src="/assets/app.js"></script>',
            script_url: (
                f'const provenance={{commit:"{COMMIT}",dirty:!1}};'.encode()
            ),
        }
    )

    fingerprint = runner.validate_served_frontend_provenance(
        client, f"{page_url}#/test", COMMIT
    )

    assert len(fingerprint) == 64
    assert [request[0] for request in client.requests] == [
        page_url,
        script_url,
    ]


def test_served_frontend_provenance_rejects_vite_development_page():
    page_url = "http://localhost:5173/"
    client = FakeHttpClient(
        {
            page_url: (
                b'<script type="module" src="/@vite/client"></script>'
            ),
        }
    )

    with pytest.raises(runner.PreflightRunnerError, match="development"):
        runner.validate_served_frontend_provenance(
            client, f"{page_url}#/test", COMMIT
        )


def test_served_frontend_provenance_rejects_wrong_commit():
    page_url = "http://localhost:5173/"
    script_url = "http://localhost:5173/assets/app.js"
    client = FakeHttpClient(
        {
            page_url: b'<script type="module" src="/assets/app.js"></script>',
            script_url: (
                f'const provenance={{commit:"{"b" * 40}",dirty:!1}};'.encode()
            ),
        }
    )

    with pytest.raises(runner.PreflightRunnerError, match="disagrees"):
        runner.validate_served_frontend_provenance(
            client, f"{page_url}#/test", COMMIT
        )


def test_served_frontend_provenance_rejects_dirty_matching_commit():
    page_url = "http://localhost:5173/"
    script_url = "http://localhost:5173/assets/app.js"
    client = FakeHttpClient(
        {
            page_url: b'<script type="module" src="/assets/app.js"></script>',
            script_url: (
                f'const provenance={{commit:"{COMMIT}",dirty:!0}};'.encode()
            ),
        }
    )

    with pytest.raises(runner.PreflightRunnerError, match="dirty"):
        runner.validate_served_frontend_provenance(
            client, f"{page_url}#/test", COMMIT
        )


class FakeOwnedProcesses:
    def __init__(self):
        self.started = []

    def start(
        self,
        name,
        command,
        *,
        cwd,
        environment,
        log_path,
    ):
        self.started.append(
            {
                "name": name,
                "command": command,
                "cwd": cwd,
                "environment": environment,
                "log_path": log_path,
            }
        )


def test_owned_services_build_then_preview_without_reload_or_hmr(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repo"
    frontend = repository / "frontend"
    frontend.mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()
    npm = tmp_path / "node" / "bin" / "npm"
    npm.parent.mkdir(parents=True)
    npm.write_text("#!/bin/sh\n", encoding="utf-8")
    npm.chmod(0o700)
    owned = FakeOwnedProcesses()
    build_calls = []

    def fake_build(command, **kwargs):
        build_calls.append((command, kwargs))
        dist = frontend / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("production", encoding="utf-8")
        return completed_process()

    monkeypatch.setattr(runner, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(runner, "port_is_bound", lambda port: False)
    monkeypatch.setattr(
        runner,
        "require_clean_repository",
        lambda **kwargs: COMMIT,
    )

    runner.start_application_services(
        owned,
        environment={"PATH": "/bin"},
        output_dir=output,
        npm_path=npm,
        python_executable=Path("/test/python"),
        expected_commit=COMMIT,
        run=fake_build,
    )

    assert build_calls[0][0] == [str(npm), "run", "build"]
    assert build_calls[0][1]["cwd"] == str(frontend)
    assert [process["name"] for process in owned.started] == [
        "backend",
        "frontend",
    ]
    backend_command = owned.started[0]["command"]
    frontend_command = owned.started[1]["command"]
    assert "--reload" not in backend_command
    assert frontend_command[:3] == [str(npm), "run", "preview"]
    assert "dev" not in frontend_command
    assert (output / "frontend-build.log").is_file()


def test_scan_evidence_bundle_requires_exact_four_file_base(tmp_path):
    paths = write_bundle(tmp_path)

    bundle = runner.scan_evidence_bundle(tmp_path)

    assert bundle is not None
    assert bundle.base_name == "rendered-digital-preflight-stamp"
    assert bundle.manifest == paths[".manifest.json"]
    assert bundle.wav == paths[".stereo.wav"]
    assert bundle.blocks == paths[".blocks.csv"]
    assert bundle.timing_csv == paths[".timing.csv"]


def test_scan_evidence_bundle_waits_for_missing_or_partial_files(tmp_path):
    paths = write_bundle(tmp_path)
    paths[".wav"] = paths.pop(".stereo.wav")
    paths[".wav"].unlink()

    assert runner.scan_evidence_bundle(tmp_path) is None

    partial = (
        tmp_path
        / "rendered-digital-preflight-stamp.stereo.wav.crdownload"
    )
    partial.write_bytes(b"partial")
    assert runner.scan_evidence_bundle(tmp_path) is None

    partial.rename(tmp_path / "Unconfirmed 123.crdownload")
    assert runner.scan_evidence_bundle(tmp_path) is None


def test_scan_evidence_bundle_rejects_extra_artifact(tmp_path):
    write_bundle(tmp_path)
    (tmp_path / "rendered-digital-preflight-stamp.extra.json").write_text(
        "{}", encoding="utf-8"
    )

    with pytest.raises(runner.PreflightRunnerError, match="extra"):
        runner.scan_evidence_bundle(tmp_path)


def test_scan_evidence_bundle_rejects_unexpected_download(tmp_path):
    write_bundle(tmp_path)
    (tmp_path / "unexpected.bin").write_bytes(b"download")

    with pytest.raises(runner.PreflightRunnerError, match="unexpected"):
        runner.scan_evidence_bundle(tmp_path)


def test_scan_evidence_bundle_rejects_mixed_timestamp_bases(tmp_path):
    paths = write_bundle(tmp_path)
    paths[".timing.csv"].rename(
        tmp_path / "rendered-digital-preflight-other.timing.csv"
    )

    with pytest.raises(runner.PreflightRunnerError, match="timestamp"):
        runner.scan_evidence_bundle(tmp_path)


def test_run_validator_builds_exact_command_and_requires_pass_report(tmp_path):
    paths = write_bundle(tmp_path)
    bundle = runner.scan_evidence_bundle(tmp_path)
    assert bundle is not None
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
        return completed_process("PASS\n")

    report = runner.run_validator(
        bundle,
        repository_root=tmp_path,
        python_executable=Path("/test/python"),
        run=fake_run,
    )

    assert report.name.endswith(".report.json")
    command = calls[0][0]
    assert command[0] == "/test/python"
    assert command[1] == str(
        tmp_path / "analyze_rendered_digital_preflight.py"
    )
    assert command[command.index("--manifest") + 1] == str(bundle.manifest)
    assert command[command.index("--wav") + 1] == str(bundle.wav)
    assert command[command.index("--blocks") + 1] == str(bundle.blocks)
    assert command[command.index("--timing-csv") + 1] == str(
        bundle.timing_csv
    )
    assert calls[0][1]["capture_output"] is True


def test_run_validator_propagates_nonzero_status_without_deleting_bundle(
    tmp_path,
):
    write_bundle(tmp_path)
    bundle = runner.scan_evidence_bundle(tmp_path)
    assert bundle is not None

    with pytest.raises(runner.ValidatorFailure) as error:
        runner.run_validator(
            bundle,
            repository_root=tmp_path,
            run=lambda *args, **kwargs: completed_process(returncode=1),
        )

    assert error.value.return_code == 1
    assert bundle.wav.exists()


class FakeProcess:
    def poll(self):
        return None


class FakeCDP:
    def __init__(self):
        self.phase = "idle"
        self.calls = []

    def evaluate(self, expression, *, user_gesture=False):
        self.calls.append(("evaluate", expression, user_gesture))
        if "const root=document.querySelector" in expression:
            return {"phase": self.phase, "alert": False}
        if "const capture=document.querySelector" in expression:
            return True
        if "dispatchEvent" in expression:
            return True
        if "const start=document.querySelector" in expression:
            return True
        if "const button=document.querySelector" in expression:
            return True
        return True

    def call(self, method, params=None):
        self.calls.append(("call", method, params))
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 7}}
        if method == "DOM.querySelector":
            return {"nodeId": 9}
        return {}


def test_dashboard_driver_uses_stable_selectors_and_exact_file(tmp_path):
    audio = tmp_path / "preflight.wav"
    audio.write_bytes(b"fixture")
    cdp = FakeCDP()
    driver = runner.DashboardDriver(cdp, chrome_process=FakeProcess())

    driver.wait_until_ready(1)
    driver.configure_and_upload(audio, timeout_seconds=1)
    driver.start()
    cdp.phase = "completed"
    driver.wait_for_completed(1)
    driver.export()

    set_files = next(
        call for call in cdp.calls
        if call[0] == "call" and call[1] == "DOM.setFileInputFiles"
    )
    assert set_files[2]["files"] == [str(audio)]
    evaluated = "\n".join(
        call[1] for call in cdp.calls if call[0] == "evaluate"
    )
    assert 'data-s2s-control="rendered-digital-capture"' in evaluated
    assert 'data-s2s-control="start-test"' in evaluated
    assert 'data-s2s-control="export-evidence"' in evaluated


def test_dashboard_driver_fails_closed_on_any_alert():
    cdp = FakeCDP()
    driver = runner.DashboardDriver(cdp, chrome_process=FakeProcess())

    with pytest.raises(runner.PreflightRunnerError, match="alert"):
        driver._fail_on_alert({"phase": "completed", "alert": True})


@pytest.mark.parametrize(
    ("phase", "alert"),
    (("running", True), ("failed", False)),
)
def test_dashboard_completion_failure_reports_safe_recorder_state(
    monkeypatch,
    phase,
    alert,
):
    class FailedCaptureCDP(FakeCDP):
        def evaluate(self, expression, *, user_gesture=False):
            self.calls.append(("evaluate", expression, user_gesture))
            if "const root=document.querySelector" in expression:
                return {
                    "phase": phase,
                    "sourceChunksSent": 100,
                    "serverTerminalState": "error",
                    "recorderFatalCode": "noncontiguous_render_quantum",
                    "recorderGapExpectedContextFrame": 482_304,
                    "recorderGapObservedContextFrame": 482_560,
                    "recorderGapDeltaFrames": 256,
                    "alert": alert,
                    "devClient": False,
                    "alertText": "private alert text must not appear",
                }
            return True

    def poll_once(predicate, **_kwargs):
        return predicate()

    monkeypatch.setattr(runner, "wait_for", poll_once)
    driver = runner.DashboardDriver(
        FailedCaptureCDP(),
        chrome_process=FakeProcess(),
    )

    with pytest.raises(runner.PreflightRunnerError) as error:
        driver.wait_for_completed(600)

    message = str(error.value)
    payload = json.loads(message.split("safe_state=", 1)[1])
    assert payload == {
        "phase": phase,
        "recorder_fatal_code": "noncontiguous_render_quantum",
        "recorder_gap_delta_frames": 256,
        "recorder_gap_expected_context_frame": 482_304,
        "recorder_gap_observed_context_frame": 482_560,
        "server_terminal_state": "error",
        "source_chunks_sent": 100,
    }
    assert "private alert text" not in message
    assert "displayed an alert" not in message


def test_dashboard_completion_timeout_reports_only_safe_structured_state(
    monkeypatch,
):
    class DiagnosticCDP(FakeCDP):
        def evaluate(self, expression, *, user_gesture=False):
            self.calls.append(("evaluate", expression, user_gesture))
            if "const root=document.querySelector" in expression:
                return {
                    "phase": "running",
                    "sourceChunksSent": 100,
                    "serverTerminalState": "pending",
                    "recorderFatalCode": "noncontiguous_render_quantum",
                    "recorderGapExpectedContextFrame": 482_304,
                    "recorderGapObservedContextFrame": 482_560,
                    # The runner derives the delta from the two frame values.
                    "recorderGapDeltaFrames": 999,
                    "alert": False,
                    "devClient": False,
                    "transcript": "must-not-appear",
                    "audio": "must-not-appear",
                }
            return True

    def timeout_after_one_poll(predicate, **_kwargs):
        assert predicate() is False
        raise runner.WaitTimeoutError(
            "timed out waiting for completed rendered-digital capture "
            "and queue drain"
        )

    monkeypatch.setattr(runner, "wait_for", timeout_after_one_poll)
    driver = runner.DashboardDriver(
        DiagnosticCDP(),
        chrome_process=FakeProcess(),
    )

    with pytest.raises(runner.PreflightRunnerError) as error:
        driver.wait_for_completed(600)

    message = str(error.value)
    payload = json.loads(message.split("safe_state=", 1)[1])
    assert payload == {
        "phase": "running",
        "recorder_fatal_code": "noncontiguous_render_quantum",
        "recorder_gap_delta_frames": 256,
        "recorder_gap_expected_context_frame": 482_304,
        "recorder_gap_observed_context_frame": 482_560,
        "server_terminal_state": "pending",
        "source_chunks_sent": 100,
    }
    assert "must-not-appear" not in message
    assert "transcript" not in message
    assert "audio" not in message
    assert "alert" not in payload


def test_dashboard_completion_timeout_rejects_untrusted_diagnostic_values(
    monkeypatch,
):
    class UntrustedDiagnosticCDP(FakeCDP):
        def evaluate(self, expression, *, user_gesture=False):
            self.calls.append(("evaluate", expression, user_gesture))
            if "const root=document.querySelector" in expression:
                return {
                    "phase": ["running", "private phase text"],
                    "sourceChunksSent": True,
                    "serverTerminalState": "pending private state text",
                    "recorderFatalCode": "private_recorder_text",
                    "recorderGapExpectedContextFrame": "482304 private",
                    "recorderGapObservedContextFrame": 1 << 60,
                    "recorderGapDeltaFrames": -(1 << 60),
                    "alert": False,
                    "devClient": False,
                }
            return True

    def timeout_after_one_poll(predicate, **_kwargs):
        assert predicate() is False
        raise runner.WaitTimeoutError("timed out waiting for completion")

    monkeypatch.setattr(runner, "wait_for", timeout_after_one_poll)
    driver = runner.DashboardDriver(
        UntrustedDiagnosticCDP(),
        chrome_process=FakeProcess(),
    )

    with pytest.raises(runner.PreflightRunnerError) as error:
        driver.wait_for_completed(600)

    message = str(error.value)
    payload = json.loads(message.split("safe_state=", 1)[1])
    assert payload == {
        "phase": "unknown",
        "server_terminal_state": "unknown",
        "source_chunks_sent": None,
    }
    assert "private" not in message


def test_dashboard_state_reads_only_explicit_safe_diagnostic_attributes():
    cdp = FakeCDP()
    driver = runner.DashboardDriver(cdp, chrome_process=FakeProcess())

    driver.state()

    expression = next(
        call[1]
        for call in cdp.calls
        if (
            call[0] == "evaluate"
            and "const root=document.querySelector" in call[1]
        )
    )
    for attribute in (
        "data-s2s-phase",
        "data-s2s-source-chunks-sent",
        "data-s2s-server-terminal-state",
        "data-s2s-recorder-fatal-code",
        "data-s2s-recorder-gap-expected-context-frame",
        "data-s2s-recorder-gap-observed-context-frame",
        "data-s2s-recorder-gap-delta-frames",
    ):
        assert attribute in expression
    assert "innerText" not in expression
    assert "textContent" not in expression


def test_output_directory_must_be_new_and_outside_repository(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", repository)

    with pytest.raises(runner.PreflightRunnerError, match="outside"):
        runner._new_output_directory(repository / "private")

    outside = tmp_path / "evidence"
    assert runner._new_output_directory(outside) == outside
    assert outside.stat().st_mode & 0o777 == 0o700
    with pytest.raises(runner.PreflightRunnerError, match="already exists"):
        runner._new_output_directory(outside)


def test_parse_args_tracks_equals_form_of_explicit_env_file(tmp_path):
    args = runner.parse_args([f"--env-file={tmp_path / '.env'}"])

    assert args.env_file_was_explicit is True


def test_find_npm_checks_sibling_node_before_resolving_symlink(
    tmp_path, monkeypatch
):
    bin_dir = tmp_path / "node-v22" / "bin"
    lib_dir = tmp_path / "node-v22" / "lib"
    bin_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    npm_target = lib_dir / "npm-cli.js"
    npm_target.write_text("#!/bin/sh\n", encoding="utf-8")
    npm_target.chmod(0o700)
    npm = bin_dir / "npm"
    npm.symlink_to(npm_target)
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\n", encoding="utf-8")
    node.chmod(0o700)
    monkeypatch.setattr(runner, "_node_major", lambda path: 22)

    assert runner.find_npm(npm) == npm.absolute()
