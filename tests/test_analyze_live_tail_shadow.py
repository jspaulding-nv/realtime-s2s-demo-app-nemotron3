from __future__ import annotations

import copy
import json

import pytest

from analyze_live_tail_shadow import ShadowEvidenceError, analyze_evidence


def evidence() -> dict:
    decisions = [
        {
            "streamGeneration": 1,
            "parentSequenceId": 0,
            "audioFrameId": index,
            "schedulePerformanceMs": index * 100.0,
            "audioContextTimeAtScheduleSeconds": index * 0.1,
            "audioBytes": 16_000,
            "sourceDurationSeconds": 0.5,
            "queueBeforeTruncationSeconds": before,
            "queueAfterTruncationSeconds": after,
            "peakQueueAfterTruncationSeconds": peak,
            "playbackRate": 1.0,
            "playbackMode": "normal",
            "truncationTriggered": index == 2,
            "suppressedArrival": index == 3,
            "droppedFrameCountThisEvent": 1 if index >= 2 else 0,
            "droppedSourceDurationThisEventSeconds": (
                0.5 if index >= 2 else 0.0
            ),
            "residualOverCapSeconds": 0.0,
        }
        for index, (before, after, peak) in enumerate(
            [
                (0.5, 0.5, 0.5),
                (0.9, 0.9, 0.9),
                (1.3, 0.8, 0.9),
                (1.2, 0.7, 0.9),
            ]
        )
    ]
    return {
        "schema": "tail-freshness-shadow/v1",
        "strategy": "truncate_parent_tail",
        "status": "complete",
        "evidenceValid": True,
        "observationOnly": True,
        "liveAudioChanged": False,
        "containsPcm": False,
        "containsTranscriptOrTranslationText": False,
        "hardCapSeconds": 1.2,
        "cancellationGuardSeconds": 0.0,
        "adaptivePlayback": False,
        "playbackPolicy": {
            "targetQueueSeconds": 0.5,
            "urgentQueueSeconds": 0.8,
            "limitQueueSeconds": 1.2,
            "catchUpReleaseSeconds": 0.4,
            "urgentReleaseSeconds": 0.7,
            "normalRate": 1.0,
            "catchUpRate": 1.05,
            "urgentRate": 1.1,
        },
        "summary": {
            "framesReceived": 4,
            "framesRetained": 2,
            "framesDropped": 2,
            "parentsReceived": 1,
            "parentsTruncated": 1,
            "parentsFullyDropped": 0,
            "totalSourceDurationSeconds": 2.0,
            "retainedSourceDurationSeconds": 1.0,
            "droppedSourceDurationSeconds": 1.0,
            "retainedSourcePercent": 50.0,
            "lastDecisionQueueSeconds": 0.7,
            "peakQueueBeforeTruncationSeconds": 1.3,
            "peakQueueAfterTruncationSeconds": 0.9,
            "truncationTriggerCount": 1,
            "suppressedArrivalCount": 1,
            "residualBreachEvents": 0,
            "peakResidualOverCapSeconds": 0.0,
            "maxDroppedParentSuffixSeconds": 1.0,
            "hardCapAchieved": True,
            "singleTailContractHolds": True,
        },
        "parents": [
            {
                "parentSequenceId": 0,
                "frameCount": 4,
                "retainedFrameCount": 2,
                "droppedFrameCount": 2,
                "sourceDurationSeconds": 2.0,
                "retainedSourceDurationSeconds": 1.0,
                "droppedSourceDurationSeconds": 1.0,
                "firstDroppedFrameId": 2,
                "shape": "partial_suffix",
            }
        ],
        "decisions": decisions,
    }


def write_evidence(tmp_path, value: dict):
    path = tmp_path / "tail-shadow.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_analyzer_independently_replays_valid_shadow(tmp_path):
    report = analyze_evidence(write_evidence(tmp_path, evidence()))

    assert report["valid"] is True
    assert report["validation"]["independent_python_replay_matches"] is True
    assert report["result"]["hard_cap_achieved"] is True
    assert report["result"]["retained_source_percent"] == 50.0
    assert report["result"]["dropped_source_duration_seconds"] == 1.0
    assert report["result"]["max_dropped_parent_suffix_seconds"] == 1.0
    assert report["result"]["residual_breach_events"] == 0
    assert "tail-shadow.json" not in json.dumps(report)


def test_analyzer_rejects_tampered_queue_decision(tmp_path):
    value = copy.deepcopy(evidence())
    value["decisions"][2]["queueAfterTruncationSeconds"] = 0.9

    with pytest.raises(ShadowEvidenceError, match="queue after truncation"):
        analyze_evidence(write_evidence(tmp_path, value))


def test_analyzer_rejects_non_suffix_parent_claim(tmp_path):
    value = copy.deepcopy(evidence())
    value["parents"][0]["firstDroppedFrameId"] = 1

    with pytest.raises(ShadowEvidenceError, match="not one suffix"):
        analyze_evidence(write_evidence(tmp_path, value))


def test_analyzer_rejects_privacy_contract_change(tmp_path):
    value = copy.deepcopy(evidence())
    value["containsPcm"] = True

    with pytest.raises(ShadowEvidenceError, match="containsPcm"):
        analyze_evidence(write_evidence(tmp_path, value))
