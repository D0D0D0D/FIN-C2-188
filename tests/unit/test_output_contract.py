"""Closed review-packet and Stage-5 regression tests."""

from __future__ import annotations

import pytest
from framework.errors import SecurityViolationError

from src.nodes.post_process_node import _FREE_TEXT_PATHS
from src.services.validation import assert_closed_review_packet


def _packet():
    return {
        "contract_version": "review_packet_v1", "mode": "invoke", "case_id": "CASE1",
        "rubric_version": "1.0-SAMPLE", "rubric_approved": False, "evaluation_state": "not_performed",
        "requirement_segments": [{"segment_id": "SEG-1", "summary": "対象期間を確認する", "required_evidence_types": ["period"]}],
        "observations": [], "version_conflict_observations": [], "suggested_additional_evidence": [],
        "accountable_owners": [], "evidence_sources": [], "unresolved_questions": [],
        "requires_human_review": True, "degradation_reason": None,
    }


def test_free_text_paths_enumerate_every_designated_field():
    assert ("observations", "*", "observation") in _FREE_TEXT_PATHS
    assert ("suggested_additional_evidence", "*", "rationale") in _FREE_TEXT_PATHS
    assert ("requirement_segments", "*", "summary") in _FREE_TEXT_PATHS
    assert ("observations", "*", "source_spans", "*", "text") in _FREE_TEXT_PATHS


@pytest.mark.parametrize("field", ["draft_response", "response_text", "submission_payload"])
def test_handoff_response_fields_are_rejected_recursively(field):
    packet = _packet()
    packet["observations"] = [{field: "x"}]
    with pytest.raises(SecurityViolationError):
        assert_closed_review_packet(packet, max_span=120, min_span=8)


@pytest.mark.parametrize("version", [None, "review_packet_v2"])
def test_contract_version_missing_or_changed_is_rejected(version):
    packet = _packet()
    if version is None:
        packet.pop("contract_version")
    else:
        packet["contract_version"] = version
    with pytest.raises(SecurityViolationError):
        assert_closed_review_packet(packet, max_span=120, min_span=8)


@pytest.mark.parametrize("span_text", ["", "x"])
def test_short_span_is_rejected(span_text):
    packet = _packet()
    packet["evidence_sources"] = [{"evidence_id": "EV1", "title": "t", "version": "1", "version_status": "current"}]
    packet["observations"] = [{
        "observation_id": "OBS-1", "segment_id": "SEG-1", "observation_kind": "period_gap", "origin": "llm",
        "evidence_ids": ["EV1"], "observation": "期間の確認が必要", "uncertainty": "medium", "review_priority": "high",
        "observation_params": {"missing_periods": ["2026-05"]},
        "source_spans": [{"evidence_id": "EV1", "span_kind": "date", "start": 0, "end": len(span_text), "text": span_text}],
    }]
    with pytest.raises(SecurityViolationError):
        assert_closed_review_packet(packet, max_span=120, min_span=8)
