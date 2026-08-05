#!/usr/bin/env python3
"""Run one non-formal 60-second or five-minute tail-freshness shadow probe.

The runner builds the current frontend tree, starts only its own FastAPI and
Vite preview children, drives the Test Dashboard through local headless Chrome,
and validates the exported numeric-only shadow artifact with the independent
Python replay.  It never starts, stops, or mutates Riva containers and it never
enables rendered-digital PCM capture.

This is deliberately an engineering probe rather than a qualification runner:
it records dirty Git state instead of requiring a committed clean checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from run_rendered_digital_preflight import (
    CDP,
    DEFAULT_API_CONFIG_URL,
    DEFAULT_AUDIO,
    DEFAULT_DASHBOARD_URL,
    DashboardDriver,
    LocalHttpClient,
    OwnedProcessSet,
    PREFLIGHT_FILE_SHA256,
    PreflightRunnerError,
    _chrome_debugging_port,
    _json_target,
    attest_docker_runtime,
    build_child_environment,
    find_chrome,
    find_npm,
    inspect_repository,
    parse_env_file,
    port_is_bound,
    riva_health_urls,
    sha256_file,
    start_chrome,
    stop_owned_process,
    wait_for,
    wait_for_http_ok,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent
SHADOW_ARTIFACT_PREFIX = "tail-freshness-shadow-"
TIMING_ARTIFACT_PREFIX = "timing-export-"
FIVE_MINUTE_SOURCE = REPOSITORY_ROOT / "test_audio" / "long-form-01.mp3"
FIVE_MINUTE_SOURCE_SHA256 = (
    "3824d3a7997213d787407e51a7594af2c94052435d7709d1cc659049188f8bdf"
)
FIVE_MINUTE_WAV_SHA256 = (
    "78e04698bf76502bc1ae23c5dac8391de0f60a51dc13aae9f042532a24df44d4"
)

EXACT_RUNTIME_OVERRIDES = {
    "S2S_PIPELINE_MODE": "staged",
    "RIVA_EOU_MS": "800",
    "RIVA_ASR_WORD_TIMES": "1",
    "STAGED_SEGMENT_MAX_CHARS": "240",
    "STAGED_SEGMENT_MAX_AGE_MS": "2000",
    "STAGED_SEGMENT_PUNCTUATION_MIN_CHARS": "0",
    "STAGED_ASR_EVENT_QUEUE_MAXSIZE": "32",
    "STAGED_NMT_QUEUE_MAXSIZE": "4",
    "STAGED_TTS_QUEUE_MAXSIZE": "4",
    "STAGED_OUTPUT_QUEUE_MAXSIZE": "4",
    "STAGED_NMT_RPC_TIMEOUT_SECONDS": "15",
    "STAGED_TTS_RPC_TIMEOUT_SECONDS": "60",
    "STAGED_TTS_MAX_SEGMENT_AUDIO_SECONDS": "60",
    "STAGED_TTS_MAX_RETRIES": "1",
    "STAGED_TTS_RESPONSE_CHUNK_TELEMETRY": "0",
    "STAGED_TTS_INCREMENTAL_PUBLISH": "1",
    "STAGED_TTS_INCREMENTAL_FRAME_MS": "500",
    "STAGED_TTS_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS": "4",
    "STAGED_TTS_SUBSEGMENT_MAX_CHARS": "0",
    "STAGED_TTS_SUBSEGMENT_MIN_CHARS": "12",
    "STAGED_CLOSE_TIMEOUT_SECONDS": "10",
}


class TailShadowDriver(DashboardDriver):
    """Browser automation for the default-off, observation-only shadow."""

    SHADOW_SELECTOR = '[data-s2s-control="tail-freshness-shadow"]'

    def shadow_state(self) -> Mapping[str, Any]:
        state = dict(self.state())
        shadow = self.cdp.evaluate(
            "(() => {"
            f"const root=document.querySelector('{self.PHASE_SELECTOR}');"
            "if(!root)return null;"
            "return {"
            "enabled:root.getAttribute('data-s2s-tail-shadow-enabled'),"
            "status:root.getAttribute('data-s2s-tail-shadow-status'),"
            "hardCap:root.getAttribute("
            "'data-s2s-tail-shadow-hard-cap-achieved'),"
            "singleTail:root.getAttribute("
            "'data-s2s-tail-shadow-single-tail-contract')"
            "};"
            "})()"
        )
        if isinstance(shadow, Mapping):
            state["shadow"] = dict(shadow)
        return state

    def configure_and_upload_shadow(
        self,
        audio_path: Path,
        *,
        timeout_seconds: float,
    ) -> None:
        configured = self.cdp.evaluate(
            "(() => {"
            f"const shadow=document.querySelector('{self.SHADOW_SELECTOR}');"
            f"const capture=document.querySelector('{self.CAPTURE_SELECTOR}');"
            "const adaptive=[...document.querySelectorAll("
            "'input[type=\"checkbox\"]')].find(input=>"
            "input.closest('label')?.innerText.includes("
            "'Adaptive Spanish playback'));"
            "if(!shadow||!capture||!adaptive)return false;"
            "if(!adaptive.checked)adaptive.click();"
            "if(!shadow.checked)shadow.click();"
            "if(capture.checked)capture.click();"
            "return adaptive.checked===true&&shadow.checked===true&&"
            "capture.checked===false;"
            "})()",
            user_gesture=True,
        )
        if configured is not True:
            raise PreflightRunnerError(
                "could not enable adaptive playback and the shadow while "
                "keeping rendered PCM capture disabled"
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
            {"nodeId": root["nodeId"], "selector": self.FILE_SELECTOR},
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
            state = self.shadow_state()
            self._fail_on_alert(state)
            return self.cdp.evaluate(
                "(() => {"
                f"const start=document.querySelector('{self.START_SELECTOR}');"
                f"const shadow=document.querySelector('{self.SHADOW_SELECTOR}');"
                f"const capture=document.querySelector('{self.CAPTURE_SELECTOR}');"
                "return Boolean(start&&!start.disabled&&shadow?.checked&&"
                "!capture?.checked);"
                "})()"
            ) is True

        wait_for(
            decoded,
            timeout_seconds=timeout_seconds,
            description="exact preflight audio decode and shadow setup",
            interval_seconds=0.25,
        )

    def require_valid_shadow_completion(self) -> None:
        state = self.shadow_state()
        self._fail_on_completion_failure(state)
        shadow = state.get("shadow")
        if state.get("phase") != "completed" or not isinstance(
            shadow, Mapping
        ):
            raise PreflightRunnerError(
                "dashboard did not expose completed shadow state"
            )
        expected = {
            "enabled": "true",
            "status": "complete",
            "hardCap": "true",
            "singleTail": "true",
        }
        if dict(shadow) != expected:
            safe_shadow = json.dumps(
                dict(shadow), sort_keys=True, separators=(",", ":")
            )
            raise PreflightRunnerError(
                "shadow completion contract failed; "
                f"safe_shadow_state={safe_shadow}"
            )


def _new_output_directory(
    requested: Optional[Path], *, profile: str, incremental_frame_ms: int
) -> Path:
    if requested is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        label = "5min" if profile == "five-minute" else "60s"
        requested = (
            REPOSITORY_ROOT
            / "experiment_results"
            / (
                f"live-tail-shadow-{label}-{incremental_frame_ms}ms-probe-"
                f"{timestamp}"
            )
        )
    output = requested.expanduser().resolve()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.mkdir(mode=0o700, exist_ok=False)
    return output


def _find_ffmpeg() -> Path:
    discovered = shutil.which("ffmpeg")
    if discovered:
        return Path(discovered).resolve()
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise PreflightRunnerError(
            "FFmpeg is required to create the registered five-minute fixture"
        ) from exc
    candidate = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise PreflightRunnerError(
            "the bundled FFmpeg executable is unavailable"
        )
    return candidate


def _prepare_audio(
    args: argparse.Namespace, output_dir: Path
) -> tuple[Path, int, str]:
    if not args.five_minute:
        audio_path = (args.audio or DEFAULT_AUDIO).expanduser().resolve(
            strict=True
        )
        if sha256_file(audio_path) != PREFLIGHT_FILE_SHA256:
            raise PreflightRunnerError(
                "audio input is not the registered exact 60-second fixture"
            )
        return audio_path, 60, PREFLIGHT_FILE_SHA256

    if args.audio is not None:
        raise PreflightRunnerError(
            "--audio cannot be combined with --five-minute"
        )
    source = FIVE_MINUTE_SOURCE.resolve(strict=True)
    if sha256_file(source) != FIVE_MINUTE_SOURCE_SHA256:
        raise PreflightRunnerError(
            "tracked five-minute source does not match its registered hash"
        )
    audio_path = output_dir / "five-minute-source.wav"
    completed = subprocess.run(
        [
            str(_find_ffmpeg()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-t",
            "300",
            "-i",
            str(source),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ],
        cwd=str(REPOSITORY_ROOT),
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise PreflightRunnerError(
            "could not create the registered five-minute WAV fixture"
        )
    if sha256_file(audio_path) != FIVE_MINUTE_WAV_SHA256:
        raise PreflightRunnerError(
            "generated five-minute WAV does not match retained-trace input"
        )
    return audio_path, 300, FIVE_MINUTE_WAV_SHA256


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _start_application_services(
    owned: OwnedProcessSet,
    *,
    environment: Mapping[str, str],
    output_dir: Path,
    npm_path: Path,
) -> None:
    occupied = [str(port) for port in (8000, 5173) if port_is_bound(port)]
    if occupied:
        raise PreflightRunnerError(
            "application port(s) already in use: " + ", ".join(occupied)
        )
    frontend_environment = dict(environment)
    frontend_environment["PATH"] = (
        str(npm_path.parent)
        + os.pathsep
        + frontend_environment.get("PATH", "")
    )
    build_log_path = output_dir / "frontend-build.log"
    with build_log_path.open("w", encoding="utf-8") as build_log:
        build = subprocess.run(
            [str(npm_path), "run", "build"],
            cwd=str(REPOSITORY_ROOT / "frontend"),
            env=frontend_environment,
            stdin=subprocess.DEVNULL,
            stdout=build_log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=300,
        )
    if build.returncode != 0:
        raise PreflightRunnerError(
            "frontend production build failed; inspect frontend-build.log"
        )
    owned.start(
        "backend",
        [
            sys.executable,
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


def _validate_probe_config(
    config: Mapping[str, Any], *, incremental_frame_ms: int
) -> None:
    try:
        exact = {
            "pipelineMode": config["pipelineMode"],
            "audioMetadataProtocolVersions": config[
                "audioMetadataProtocolVersions"
            ],
            "asrEouMs": config["modelConfig"]["asr"]["eouMs"],
            "asrWordTimes": config["modelConfig"]["asr"][
                "wordTimeOffsets"
            ],
            "telemetrySchemaVersion": config["stagedConfig"][
                "telemetrySchemaVersion"
            ],
            "segmentPunctuationMinChars": config["stagedConfig"][
                "segmentPunctuationMinChars"
            ],
            "incrementalPublish": config["stagedConfig"][
                "ttsIncrementalPublishEnabled"
            ],
            "incrementalFrameMs": config["stagedConfig"][
                "ttsIncrementalFrameMs"
            ],
            "ttsSubsegmentMaxChars": config["stagedConfig"][
                "ttsSubsegmentMaxChars"
            ],
        }
    except (KeyError, TypeError) as exc:
        raise PreflightRunnerError(
            "/api/config did not expose the shadow probe contract"
        ) from exc
    expected = {
        "pipelineMode": "staged",
        "audioMetadataProtocolVersions": [1],
        "asrEouMs": 800,
        "asrWordTimes": True,
        "telemetrySchemaVersion": 3,
        "segmentPunctuationMinChars": 0,
        "incrementalPublish": True,
        "incrementalFrameMs": incremental_frame_ms,
        "ttsSubsegmentMaxChars": 0,
    }
    if exact != expected:
        raise PreflightRunnerError(
            "/api/config does not match the exact live shadow probe contract"
        )


def _wait_for_downloads(
    output_dir: Path,
    driver: TailShadowDriver,
    *,
    timeout_seconds: float,
) -> tuple[Path, Path]:
    stable: Optional[tuple[tuple[str, int], ...]] = None

    def complete() -> Optional[tuple[Path, Path]]:
        nonlocal stable
        driver.require_valid_shadow_completion()
        if any(output_dir.glob("*.crdownload")):
            stable = None
            return None
        timing = list(output_dir.glob(f"{TIMING_ARTIFACT_PREFIX}*.csv"))
        shadows = list(output_dir.glob(f"{SHADOW_ARTIFACT_PREFIX}*.json"))
        if len(timing) != 1 or len(shadows) != 1:
            stable = None
            return None
        signature = tuple(
            sorted((path.name, path.stat().st_size) for path in (*timing, *shadows))
        )
        if any(size <= 0 for _, size in signature):
            stable = None
            return None
        if signature != stable:
            stable = signature
            return None
        return timing[0], shadows[0]

    return wait_for(
        complete,
        timeout_seconds=timeout_seconds,
        description="timing CSV and tail shadow JSON downloads",
        interval_seconds=0.5,
    )


def _run_validator(evidence: Path, output_dir: Path) -> tuple[Path, Path]:
    json_report = output_dir / "tail-shadow.analysis.json"
    markdown_report = output_dir / "tail-shadow.analysis.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "analyze_live_tail_shadow.py"),
            "--evidence-json",
            str(evidence),
            "--json-output",
            str(json_report),
            "--markdown-output",
            str(markdown_report),
        ],
        cwd=str(REPOSITORY_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    (output_dir / "validator.log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise PreflightRunnerError(
            "independent tail shadow validation failed; inspect validator.log"
        )
    if not json_report.is_file() or not markdown_report.is_file():
        raise PreflightRunnerError(
            "independent validator returned no complete report"
        )
    return json_report, markdown_report


def _safe_model_record(config: Mapping[str, Any]) -> Mapping[str, Any]:
    model_config = config.get("modelConfig")
    if not isinstance(model_config, Mapping):
        return {}
    result: dict[str, Any] = {}
    for role in ("asr", "nmt", "tts"):
        value = model_config.get(role)
        if isinstance(value, Mapping):
            result[role] = {
                "image": value.get("image"),
                "imageDigest": value.get("imageDigest"),
                "profile": value.get("profile"),
            }
    return result


def _run(args: argparse.Namespace) -> Path:
    state = inspect_repository(REPOSITORY_ROOT)
    profile = "five-minute" if args.five_minute else "60-second"
    output_dir = _new_output_directory(
        args.output_dir,
        profile=profile,
        incremental_frame_ms=args.incremental_frame_ms,
    )
    audio_path, audio_duration_seconds, audio_sha256 = _prepare_audio(
        args, output_dir
    )
    loaded = parse_env_file(args.env_file.expanduser())
    environment = build_child_environment(
        os.environ, loaded, repository_root=REPOSITORY_ROOT
    )
    runtime_overrides = dict(EXACT_RUNTIME_OVERRIDES)
    runtime_overrides["STAGED_TTS_INCREMENTAL_FRAME_MS"] = str(
        args.incremental_frame_ms
    )
    environment.update(runtime_overrides)
    http = LocalHttpClient()
    owned = OwnedProcessSet()
    chrome: Optional[subprocess.Popen[Any]] = None
    chrome_log: Any = None
    cdp: Optional[CDP] = None
    profile_dir: Optional[Path] = None
    try:
        for index, url in enumerate(riva_health_urls(environment), start=1):
            wait_for_http_ok(
                http,
                url,
                timeout_seconds=args.service_timeout_seconds,
                description=f"Riva service {index} readiness",
            )
        npm_path = find_npm(args.npm)
        _start_application_services(
            owned,
            environment=environment,
            output_dir=output_dir,
            npm_path=npm_path,
        )
        wait_for_http_ok(
            http,
            DEFAULT_API_CONFIG_URL,
            timeout_seconds=args.service_timeout_seconds,
            description="FastAPI configuration endpoint",
            owned=owned,
        )
        wait_for_http_ok(
            http,
            urllib.parse.urldefrag(DEFAULT_DASHBOARD_URL).url,
            timeout_seconds=args.service_timeout_seconds,
            description="production Test Dashboard",
            owned=owned,
        )
        config = http.json(DEFAULT_API_CONFIG_URL, timeout_seconds=5)
        _validate_probe_config(
            config, incremental_frame_ms=args.incremental_frame_ms
        )
        docker_attestation = attest_docker_runtime(environment, config)
        _write_private_json(
            output_dir / "nonformal-probe.json",
            {
                "schema": "live-tail-shadow-nonformal-probe/v1",
                "formalQualification": False,
                "reason": "working_tree_may_be_dirty",
                "repositoryCommit": state.commit,
                "repositoryDirtyAtStart": state.dirty,
                "profile": profile,
                "audioDurationSeconds": audio_duration_seconds,
                "audioSha256": audio_sha256,
                "runtimeOverrides": runtime_overrides,
                "models": _safe_model_record(config),
                "dockerRuntimeAttested": True,
                "dockerRuntimeIdentitySha256": (
                    docker_attestation.identity_sha256()
                ),
                "renderedDigitalPcmCapture": False,
                "observationOnly": True,
                "liveAudioChanged": False,
            },
        )

        profile_dir = Path(tempfile.mkdtemp(prefix="s2s-tail-shadow-chrome-"))
        profile_dir.chmod(0o700)
        chrome_path = find_chrome(args.chrome)
        chrome, chrome_log = start_chrome(
            chrome_path=chrome_path,
            profile_dir=profile_dir,
            output_dir=output_dir,
        )
        debugging_port = _chrome_debugging_port(chrome, profile_dir)
        debugging_base = f"http://127.0.0.1:{debugging_port}"
        wait_for_http_ok(
            http,
            f"{debugging_base}/json/version",
            timeout_seconds=20,
            description="Chrome DevTools endpoint",
        )
        target = _json_target(http, debugging_base)
        cdp = CDP(str(target["webSocketDebuggerUrl"]))
        for domain in ("Page", "Runtime", "DOM"):
            cdp.call(f"{domain}.enable")
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
                {"behavior": "allow", "downloadPath": str(output_dir)},
            )
        cdp.call("Page.navigate", {"url": DEFAULT_DASHBOARD_URL})
        driver = TailShadowDriver(cdp, chrome_process=chrome)
        driver.wait_until_ready(args.service_timeout_seconds)
        driver.configure_and_upload_shadow(
            audio_path, timeout_seconds=args.decode_timeout_seconds
        )
        driver.start()
        print("Shadow probe started; live audio remains unchanged.")
        driver.wait_for_completed(args.timeout_seconds)
        driver.require_valid_shadow_completion()
        driver.export()
        _, evidence = _wait_for_downloads(
            output_dir,
            driver,
            timeout_seconds=args.download_timeout_seconds,
        )
        report, _ = _run_validator(evidence, output_dir)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one non-formal, observation-only 60-second or five-minute "
            "live tail freshness shadow probe"
        )
    )
    parser.add_argument(
        "--audio",
        type=Path,
        help="registered exact 60-second fixture (default: test_audio/preflight.wav)",
    )
    parser.add_argument(
        "--five-minute",
        action="store_true",
        help=(
            "generate and run the hash-bound 300-second prefix of the tracked "
            "long-form-01 fixture"
        ),
    )
    parser.add_argument("--env-file", type=Path, default=REPOSITORY_ROOT / ".env")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--incremental-frame-ms",
        type=int,
        choices=(100, 500),
        default=500,
        help="schema-3 TTS publication frame target (default: 500)",
    )
    parser.add_argument("--npm", type=Path)
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--service-timeout-seconds", type=float, default=180)
    parser.add_argument("--decode-timeout-seconds", type=float, default=60)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--download-timeout-seconds", type=float, default=60)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    previous_umask = os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        for name in (
            "service_timeout_seconds",
            "decode_timeout_seconds",
            "timeout_seconds",
            "download_timeout_seconds",
        ):
            if getattr(args, name) <= 0:
                raise PreflightRunnerError(
                    f"--{name.replace('_', '-')} must be positive"
                )
        _run(args)
        return 0
    except (OSError, subprocess.SubprocessError, PreflightRunnerError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
