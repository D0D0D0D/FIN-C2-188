"""Repo-level regression tests for refreshed shared-part contracts."""

from __future__ import annotations

import json

import pytest
from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from langgraph.graph import END

from src.entry_adapter import Operation
from src.graph.domain_workflow_graph import EvidenceReviewWorkflowGraph
from src.nodes.inner.stages import (
    ProvenanceVerifyNode,
    RequirementSpecNode,
    SufficiencyNarrativeNode,
)
from src.payload_store import PayloadStore


class _RaisingLlm:
    def __init__(self, marker: str) -> None:
        self._marker = marker

    def complete(self, *_args, **_kwargs):
        raise PermissionError(f"provider rejected credential fragment: {self._marker}")


class _ResponseLlm:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def complete(self, *_args, **_kwargs):
        return {"content": json.dumps(self._payload, ensure_ascii=False)}


def _context_state(**values):
    return {
        "caller_trust_level": "VERIFIED_EXTERNAL",
        "session_id": "session-1",
        "thread_id": "thread-1",
        "correlation_id": "correlation-1",
        "trace_id": "trace-1",
        **values,
    }


def _provenance_state(observation: object) -> dict:
    return _context_state(
        case_id="CASE-1",
        evidence_index=[{"evidence_id": "EV-1"}],
        requirement_segments=[
            {
                "segment_id": "SEG-1",
                "summary": "統制記録を確認する",
                "required_evidence_types": ["control_action"],
            }
        ],
        requirement_specs=[
            {
                "segment_id": "SEG-1",
                "extracted": {
                    "period": False,
                    "population": False,
                    "control_action": False,
                    "approval_authority": False,
                },
            }
        ],
        deterministic_observations=[observation],
        llm_observations=[],
        unresolved_questions=[],
        suggested_additional_evidence=[],
    )


def _valid_observation() -> dict:
    return {
        "observation_id": "OBS-1",
        "segment_id": "SEG-1",
        "observation_kind": "control_action_gap",
        "origin": "deterministic",
        "evidence_ids": ["EV-1"],
        "observation": "統制行為を確認できません。",
        "uncertainty": "medium",
        "review_priority": "high",
        "observation_params": {"required_control_action": "review"},
        "source_spans": [],
    }


def _narrative_state(store: PayloadStore, ref: str) -> dict:
    return _context_state(
        case_id="CASE-1",
        evidence_index=[{"evidence_id": "EV-1", "content_payload_ref": ref}],
        requirement_segments=[
            {
                "segment_id": "SEG-1",
                "summary": "統制記録を確認する",
                "required_evidence_types": ["control_action"],
            }
        ],
        deterministic_observations=[_valid_observation()],
    )


def _valid_narrative() -> dict:
    return {
        "observations": [
            {
                "segment_id": "SEG-1",
                "observation_kind": "control_action_gap",
                "evidence_ids": ["EV-1"],
                "observation": "統制行為を確認できません。",
                "uncertainty": "medium",
                "observation_params": {"required_control_action": "review"},
                "source_spans": [],
            }
        ],
        "suggested_additional_evidence": [],
    }


def test_entry_adapter_exposes_only_operation_aware_authentication() -> None:
    import src.entry_adapter as entry_adapter

    assert Operation.READ_INVOKE.value == "read_invoke"
    assert not hasattr(entry_adapter, "authenticate_scope")


def test_inner_graph_route_uses_domain_state_status() -> None:
    graph = EvidenceReviewWorkflowGraph(config={"payload_store": object()})

    assert graph.route({"status": AgentStatus.SUCCESS.value}) == "provenance"
    assert graph.route({"status": AgentStatus.ERROR.value}) == END


def test_provenance_contract_has_provisioned_control_then_rejects_malformed_claim() -> None:
    node = ProvenanceVerifyNode(config={"llm": None})

    control = node.execute(_provenance_state(_valid_observation()))
    assert control["status"] == AgentStatus.SUCCESS.value
    assert control["observations"][0]["evidence_ids"] == ["EV-1"]

    malformed = {**_valid_observation(), "evidence_ids": "EV-1"}
    with pytest.raises(SecurityViolationError, match=r"evidence_ids.*list\[str\]"):
        node.execute(_provenance_state(malformed))


def test_requirement_provider_exception_is_error_without_fallback_or_raw_message() -> None:
    marker = "SENSITIVE-PROVIDER-MARKER"
    node = RequirementSpecNode(config={"llm": _RaisingLlm(marker)})

    result = node.execute(
        _context_state(
            requirement_segments=[
                {
                    "segment_id": "SEG-1",
                    "summary": "要求",
                    "required_evidence_types": ["period"],
                }
            ],
            evidence_normalized=[],
        )
    )

    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_code"] == "llm_call_failed"
    assert "requirement_specs" not in result
    assert marker not in json.dumps(result, ensure_ascii=False)


def test_typed_narrative_projection_has_control_then_rejects_nested_raw_value() -> None:
    store = PayloadStore(scope_keys=("case_id",))
    ref = store.put("abcdefghijklm統制記録を確認した。", scope={"case_id": "CASE-1"}, session_id="session-1")
    state = _narrative_state(store, ref)

    control = SufficiencyNarrativeNode(
        config={"payload_store": store, "llm": _ResponseLlm(_valid_narrative())}
    ).execute(state)
    assert control["status"] == AgentStatus.SUCCESS.value
    assert isinstance(control["llm_observations"][0]["observation"], str)

    marker = "RAW-SOURCE-MARKER"
    malformed = _valid_narrative()
    malformed["observations"][0]["observation"] = {"raw": marker}
    with pytest.raises(SecurityViolationError) as exc_info:
        SufficiencyNarrativeNode(
            config={"payload_store": store, "llm": _ResponseLlm(malformed)}
        ).execute(state)

    assert marker not in str(exc_info.value)
