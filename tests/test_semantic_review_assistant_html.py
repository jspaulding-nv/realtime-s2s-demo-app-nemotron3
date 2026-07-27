from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "semantic_review_assistant.html"

ASSIGNMENT_KEYS = {
    "schema_version",
    "assignment_type",
    "schedule_ledger_sha256",
    "source_pcm_sha256",
    "source_pcm_sample_count",
    "source_sample_rate_hz",
    "translated_pcm_sha256",
    "translated_pcm_sample_count",
    "translated_sample_rate_hz",
    "minimum_independent_reviewers",
    "minimum_boundary_confidence",
    "landmark_rule",
    "events",
    "privacy",
}
ASSIGNMENT_EVENT_KEYS = {
    "event_id",
    "source_window_start_sample",
    "source_window_end_sample_exclusive",
}
ASSIGNMENT_PRIVACY_KEYS = {
    "contains_audio",
    "contains_transcript_or_translation_text",
    "contains_reviewer_identity",
    "contains_file_path_or_uri",
    "contains_wall_clock_timestamp",
    "private_review_artifact",
}
OBSERVATION_KEYS = {
    "schema_version",
    "observation_type",
    "assignment_sha256",
    "schedule_ledger_sha256",
    "source_pcm_sha256",
    "source_pcm_sample_count",
    "translated_pcm_sha256",
    "translated_pcm_sample_count",
    "reviewer_session_id",
    "independence_attestation",
    "events",
    "privacy",
}
OBSERVATION_EVENT_KEYS = {
    "event_id",
    "source_sample_index",
    "translated_sample_index",
    "semantic_equivalence",
    "source_boundary_confidence",
    "translated_boundary_confidence",
    "translation_quality_rating",
    "intelligibility_rating",
    "naturalness_rating",
    "issue_flags",
}
OBSERVATION_PRIVACY_KEYS = ASSIGNMENT_PRIVACY_KEYS | {"contains_free_text"}
ISSUE_FLAGS = {
    "meaning_mismatch",
    "omission",
    "addition",
    "pronunciation",
    "unnatural_prosody",
    "too_fast",
    "too_slow",
    "boundary_ambiguous",
}


class _MarkupInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[dict[str, str | None]] = []
        self.links: list[dict[str, str | None]] = []
        self.textareas = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "script":
            self.scripts.append(attributes)
        elif tag == "link":
            self.links.append(attributes)
        elif tag == "textarea":
            self.textareas += 1


@pytest.fixture(scope="module")
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def inventory(html: str) -> _MarkupInventory:
    parser = _MarkupInventory()
    parser.feed(html)
    return parser


def _javascript_array(html: str, constant: str) -> list[str]:
    match = re.search(
        rf"\bconst\s+{re.escape(constant)}\s*=\s*(\[[\s\S]*?\]);",
        html,
    )
    assert match is not None, f"missing JavaScript constant {constant}"
    return json.loads(match.group(1))


def _inline_script(html: str) -> str:
    matches = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", html)
    assert len(matches) == 1
    return matches[0]


def _function_source(script: str, function_name: str) -> str:
    marker = f"function {function_name}("
    start = script.find(marker)
    assert start >= 0, f"missing function {function_name}"
    brace = script.find("{", start)
    assert brace >= 0
    depth = 0
    quote: str | None = None
    escaped = False
    template_depth = 0
    index = brace
    while index < len(script):
        character = script[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif quote == "`" and character == "$" and script[index + 1:index + 2] == "{":
                template_depth += 1
                index += 1
            elif character == quote and template_depth == 0:
                quote = None
            elif quote == "`" and character == "}" and template_depth:
                template_depth -= 1
        elif character in {'"', "'", "`"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return script[start:index + 1]
        index += 1
    raise AssertionError(f"unterminated function {function_name}")


def test_is_one_self_contained_offline_file(
    html: str,
    inventory: _MarkupInventory,
) -> None:
    assert HTML_PATH.is_file()
    assert "<!doctype html>" in html.lower()
    assert len(inventory.scripts) == 1
    assert "src" not in inventory.scripts[0]
    assert inventory.links == []

    forbidden_resource_tags = {"iframe", "object", "embed", "img", "video"}
    assert not [
        tag for tag, _attrs in inventory.elements
        if tag in forbidden_resource_tags
    ]
    for tag, attrs in inventory.elements:
        if tag in {"audio", "source"}:
            assert "src" not in attrs

    csp_match = re.search(
        r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"',
        html,
    )
    assert csp_match is not None
    csp = csp_match.group(1)
    assert "default-src 'none'" in csp
    assert "connect-src 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "media-src blob:" in csp


def test_has_no_network_or_persistent_browser_apis(html: str) -> None:
    forbidden_patterns = {
        "HTTP request API": r"\bfetch\s*\(",
        "legacy request API": r"\bXMLHttpRequest\b",
        "socket API": r"\bWebSocket\b",
        "event stream API": r"\bEventSource\b",
        "beacon API": r"\bsendBeacon\b",
        "browser local persistence": r"\blocalStorage\b",
        "browser session persistence": r"\bsessionStorage\b",
        "browser database": r"\bindexedDB\b",
        "browser cookie access": r"\bdocument\s*\.\s*cookie\b",
        "cookie store": r"\bcookieStore\b",
        "service worker": r"\bserviceWorker\b",
        "cache storage": r"\bcaches\s*\.",
    }
    for description, pattern in forbidden_patterns.items():
        assert re.search(pattern, html, re.IGNORECASE) is None, description


def test_collects_no_identity_and_has_no_free_text_control(
    html: str,
    inventory: _MarkupInventory,
) -> None:
    assert inventory.textareas == 0
    forbidden_input_types = {
        "text",
        "email",
        "tel",
        "url",
        "search",
        "password",
        "hidden",
    }
    inputs = [
        attrs for tag, attrs in inventory.elements
        if tag == "input"
    ]
    assert inputs
    assert not [
        attrs for attrs in inputs
        if (attrs.get("type") or "text").lower() in forbidden_input_types
    ]
    assert not [
        attrs for _tag, attrs in inventory.elements
        if "contenteditable" in attrs
    ]
    assert "contains_reviewer_identity: false" in html
    assert "contains_free_text: false" in html


def test_loads_the_exact_four_review_inputs(
    inventory: _MarkupInventory,
) -> None:
    input_types = {
        attrs.get("id"): attrs.get("type")
        for tag, attrs in inventory.elements
        if tag == "input"
    }
    assert input_types["assignment-file"] == "file"
    assert input_types["ledger-file"] == "file"
    assert input_types["source-file"] == "file"
    assert input_types["translated-file"] == "file"


def test_assignment_and_observation_key_contracts_are_exact(html: str) -> None:
    assert set(_javascript_array(html, "ASSIGNMENT_KEYS")) == ASSIGNMENT_KEYS
    assert (
        set(_javascript_array(html, "ASSIGNMENT_EVENT_KEYS"))
        == ASSIGNMENT_EVENT_KEYS
    )
    assert (
        set(_javascript_array(html, "ASSIGNMENT_PRIVACY_KEYS"))
        == ASSIGNMENT_PRIVACY_KEYS
    )
    assert set(_javascript_array(html, "OBSERVATION_KEYS")) == OBSERVATION_KEYS
    assert (
        set(_javascript_array(html, "OBSERVATION_EVENT_KEYS"))
        == OBSERVATION_EVENT_KEYS
    )
    assert (
        set(_javascript_array(html, "OBSERVATION_PRIVACY_KEYS"))
        == OBSERVATION_PRIVACY_KEYS
    )

    required_literals = {
        "private_semantic_review_assignment",
        "private_semantic_reviewer_observation",
        "source_idea_completion_word_onset_to_corresponding_"
        "translated_word_onset_v1",
    }
    for literal in required_literals:
        assert literal in html
    assert "minimum_independent_reviewers,\n        2" in html
    assert "minimum_boundary_confidence,\n        3" in html


def test_json_loader_rejects_duplicate_keys_including_escaped_forms(
    html: str,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _inline_script(html)
    start = script.index("function rejectDuplicateJsonKeys(")
    end = script.index("\n\n    function decodeJson(", start)
    duplicate_guard = script[start:end]
    fixture = f"""
"use strict";
{duplicate_guard}

rejectDuplicateJsonKeys(
  '{{"outer":{{"first":1,"second":[true,false,null]}}}}',
  "fixture"
);
for (const payload of [
  '{{"same":1,"same":2}}',
  '{{"same":1,"\\\\u0073ame":2}}',
  '{{"outer":{{"same":1,"same":2}}}}'
]) {{
  let rejected = false;
  try {{
    rejectDuplicateJsonKeys(payload, "fixture");
  }} catch (error) {{
    rejected = error.message.includes("duplicate object key");
  }}
  if (!rejected) {{
    throw new Error("duplicate JSON key was not rejected");
  }}
}}
"""
    completed = subprocess.run(
        [node, "-e", fixture],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "rejectDuplicateJsonKeys(decoded, context)" in html


def test_assignment_requires_three_ordered_non_overlapping_events(
    html: str,
) -> None:
    assert "value.events.length < 3" in html
    assert "event.event_id <= previousEventId" in html
    assert "start < previousWindowEnd" in html
    assert "previousEventId = event.event_id" in html
    assert "previousWindowEnd = end" in html


def test_hashes_raw_documents_full_wavs_and_pcm_payloads(html: str) -> None:
    assert 'crypto.subtle.digest("SHA-256", bytes)' in html
    assert "sha256Hex(assignmentBytes)" in html
    assert "sha256Hex(ledgerBytes)" in html
    assert "assignment.schedule_ledger_sha256" in html
    assert "ledgerHash" in html
    assert "sha256Hex(buffer)" in html
    assert (
        "parsed.dataOffset + parsed.dataByteLength"
        in html
    )
    assert "contract.review_wav_sha256" in html
    assert "contract.pcm_sha256" in html


def test_wav_parser_enforces_riff_pcm16_mono_contract(html: str) -> None:
    tokens = {
        '"RIFF"',
        '"WAVE"',
        '"fmt "',
        '"data"',
        "format.audioFormat !== 1",
        "format.channels !== 1",
        "format.bitsPerSample !== 16",
        "format.blockAlign !== 2",
        "format.byteRate !== format.sampleRateHz * 2",
        "parsed.sampleCount !== contract.sample_count",
        "parsed.sampleRateHz !== contract.sample_rate_hz",
    }
    for token in tokens:
        assert token in html


def test_review_unlock_and_export_are_fail_closed(html: str) -> None:
    assert 'id="review-area" class="hidden"' in html
    assert 'id="download-observation" type="button" disabled' in html
    assert "All four files passed local binding and PCM validation" in html
    assert "downloadButton.disabled = true" in html
    assert "await loadAndValidate(selected, generation)" in html
    assert "sameValidatedBinding(originalBundle, freshBundle)" in html
    assert "makeObservation(freshBundle)" in html


def test_reviewer_ui_has_required_audio_marker_and_rating_controls(
    html: str,
) -> None:
    required_tokens = {
        'id="source-audio" controls',
        'id="translated-audio" controls',
        'id="source-waveform"',
        'id="translated-waveform"',
        "seekFromPointer",
        "source_sample_index",
        "translated_sample_index",
        "source_window_start_sample",
        "source_window_end_sample_exclusive",
        "Nudge samples",
        "semantic_equivalence",
        "source_boundary_confidence",
        "translated_boundary_confidence",
        "translation_quality_rating",
        "intelligibility_rating",
        "naturalness_rating",
        "accepted",
        "uncertain",
        "rejected",
    }
    for token in required_tokens:
        assert token in html
    normalized = " ".join(html.split())
    assert "1 is lowest and 5 is highest" in normalized


def test_rating_scales_have_shared_anchored_rubrics(html: str) -> None:
    normalized = " ".join(html.split())
    assert "Shared 1/3/5 rating anchors" in normalized
    assert "1 = guess" in normalized
    assert "5 = precise, repeatable onset" in normalized
    assert "meaning is wrong or substantially missing" in normalized
    assert "5 = meaning is fully preserved" in normalized
    assert "5 = immediately clear" in normalized
    assert "5 = natural and appropriate" in normalized
    assert "Use 2 or 4 when the judgment falls between anchors" in normalized


def test_omission_has_a_non_reconciling_diagnostic_convention(
    html: str,
) -> None:
    normalized = " ".join(html.split())
    assert "If the translated speech omits the idea" in normalized
    assert "nearest audible candidate or the surrounding phrase" in normalized
    assert "translated-boundary confidence to 1" in normalized
    assert "diagnostic only and cannot support reconciliation" in normalized


def test_omission_contract_is_enforced_in_form_and_export_source(
    html: str,
) -> None:
    script = _inline_script(html)
    form_contract = _function_source(
        script,
        "enforceOmissionFormContract",
    )
    change_handler = _function_source(script, "handleEventFormChange")
    export_contract = _function_source(script, "requireOmissionContract")
    collector = _function_source(script, "collectEvents")

    assert '[data-issue-flag="omission"]' in form_contract
    assert 'equivalence.value = "rejected"' in form_contract
    assert 'translatedConfidence.value = "1"' in form_contract
    assert "enforceOmissionFormContract(card)" in change_handler
    assert (
        'select.addEventListener("change", handleEventFormChange)'
        in html
    )
    assert (
        'checkbox.addEventListener("change", handleEventFormChange)'
        in html
    )
    assert "requireOmissionContract(" in collector
    assert 'issueFlags.includes("omission")' in export_contract
    assert 'equivalence !== "rejected"' in export_contract
    assert "translatedBoundaryConfidence !== 1" in export_contract

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    fixture = f"""
"use strict";
{export_contract}

requireOmissionContract(["omission"], "rejected", 1, "fixture");
requireOmissionContract([], "accepted", 5, "fixture");
for (const invalid of [
  [["omission"], "accepted", 1],
  [["omission"], "uncertain", 1],
  [["omission"], "rejected", 2]
]) {{
  let rejected = false;
  try {{
    requireOmissionContract(...invalid, "fixture");
  }} catch (error) {{
    rejected = error.message.includes("omission must be rejected");
  }}
  if (!rejected) {{
    throw new Error("invalid omission contract was not rejected");
  }}
}}
"""
    completed = subprocess.run(
        [node, "-e", fixture],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_issue_flags_are_exact_unique_and_sorted(html: str) -> None:
    assert set(_javascript_array(html, "ISSUE_FLAGS")) == ISSUE_FLAGS
    assert ".sort();" in html
    assert "new Set(issueFlags).size !== issueFlags.length" in html


def test_independence_privacy_and_scope_are_prominent(html: str) -> None:
    normalized = " ".join(html.split())
    assert "Work independently and keep this first pass blind" in normalized
    assert "did not view, discuss, copy, average, or reconcile" in normalized
    assert 'id="independence-check" type="checkbox"' in html
    assert "independence_attestation: true" in html
    assert "files never leave this device" in normalized
    assert "review tooling only; it is not a deployed" in normalized


def test_anonymous_lowercase_uuidv4_and_anonymous_download(html: str) -> None:
    assert "crypto.getRandomValues(bytes)" in html
    assert "bytes[6] = (bytes[6] & 0x0f) | 0x40" in html
    assert "bytes[8] = (bytes[8] & 0x3f) | 0x80" in html
    assert "UUID_V4.test(value)" in html
    assert "reviewer_session_id: reviewerSessionId" in html
    assert "anonymous-review-${reviewerSessionId}.json" in html
    assert "JSON.stringify(observation, null, 2)" in html


def test_inline_javascript_parses_when_node_is_available(
    html: str,
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script_path = tmp_path / "review-assistant.js"
    script_path.write_text(_inline_script(html), encoding="utf-8")
    completed = subprocess.run(
        [node, "--check", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_dom_independent_wav_parser_accepts_pcm16_and_rejects_stereo(
    html: str,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _inline_script(html)
    ascii_source = _function_source(script, "ascii")
    parser_source = _function_source(script, "parsePcm16MonoWav")
    fixture = f"""
"use strict";
{ascii_source}
{parser_source}

function wav(channels) {{
  const sampleCount = 4;
  const blockAlign = channels * 2;
  const buffer = new ArrayBuffer(44 + sampleCount * blockAlign);
  const view = new DataView(buffer);
  function text(offset, value) {{
    for (let index = 0; index < value.length; index += 1) {{
      view.setUint8(offset + index, value.charCodeAt(index));
    }}
  }}
  text(0, "RIFF");
  view.setUint32(4, buffer.byteLength - 8, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 16000 * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, sampleCount * blockAlign, true);
  return buffer;
}}

const parsed = parsePcm16MonoWav(wav(1), "fixture");
if (parsed.sampleCount !== 4 || parsed.sampleRateHz !== 16000) {{
  throw new Error("valid mono fixture parsed incorrectly");
}}
let rejected = false;
try {{
  parsePcm16MonoWav(wav(2), "fixture");
}} catch (_error) {{
  rejected = true;
}}
if (!rejected) {{
  throw new Error("stereo fixture was not rejected");
}}
"""
    completed = subprocess.run(
        [node, "-e", fixture],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
