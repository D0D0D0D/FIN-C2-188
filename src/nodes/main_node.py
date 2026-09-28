"""Cat2 main slot for the evidence review domain workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import EvidenceReviewWorkflowGraph
from src.payload_store import PayloadStore


class EvidenceReviewWorkflowGraphNode(DomainWorkflowGraphNode):
    """Serialize the parent allowlist into the isolated inner workflow."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    propagate_hitl: ClassVar[bool] = False
    error_strategy: ClassVar[str] = "propagate"
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec(
            gate_kind="review",
            fields={
                "evaluation_state": "evaluation_state",
                "requirement_segments": "requirement_segments",
                "observations": "observations",
                "version_conflict_observations": "version_conflict_observations",
                "suggested_additional_evidence": "suggested_additional_evidence",
                "accountable_owners": "accountable_owners",
                "evidence_sources": "evidence_sources",
                "unresolved_questions": "unresolved_questions",
                "degradation_reason": "degradation_reason",
                "error_log": "error_log",
            },
        )
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        values = dict(config or {})
        values.setdefault("payload_store", PayloadStore(scope_keys=("case_id",)))
        super().__init__(config=values, subgraph_factory=EvidenceReviewWorkflowGraph, use_inner_checkpointer=False)

    def _extra_security_gate_input(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value or not state.get("_preprocess_validated"):
            emit_trace_event("upstream_error_short_circuited", {"stage": "main"}, state)
            return {**state, "status": AgentStatus.ERROR.value}
        required = ("case_id", "request_payload_ref", "evidence_index", "rubric_version")
        if any(key not in state for key in required):
            emit_trace_event("upstream_error_short_circuited", {"stage": "main", "reason": "missing_parent_field"}, state)
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["main input was not provisioned"]}
        return state

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        emit_trace_event("domain_workflow_invoked", {"case_id": state.get("case_id", "")}, state)
        return super().execute(state)

    def _parent_config(self) -> dict[str, Any]:
        forwarded = super()._parent_config()
        forwarded["memory_enabled"] = False
        forwarded["hitl"] = {"enabled": False}
        return forwarded

    def extract_input(self, state: dict) -> str:
        envelope = {
            key: state.get(key)
            for key in ("case_id", "request_payload_ref", "evidence_index", "response_checklist", "owner_directory", "rubric_version", "rubric_approved", "degradation_reason")
        }
        return json.dumps(envelope, ensure_ascii=False, default=str, sort_keys=True)

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        delta = super().merge_output({**state, "mode": "invoke"}, sub_result)
        delta["requires_human_review"] = True
        return delta


# Compatibility name retained for scaffold consumers; the main slot is GraphNode.
MainNode = EvidenceReviewWorkflowGraphNode
