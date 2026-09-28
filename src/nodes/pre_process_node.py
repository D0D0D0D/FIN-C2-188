"""Outer S-2 validation and rubric governance."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.nodes.function_node import FunctionNode
from shared.security.pii_detector import detect_pii
from src.services.validation import rubric_state, validate_internal_context


class PreProcessNode(FunctionNode):
    """Fail closed on malformed internal envelopes before the GraphNode runs."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {
            "max_evidence_entries": 200,
            "sufficiency_rubric": {
                "version": "1.0", "approved_by": "SAMPLE", "approved_on": "SAMPLE",
                "reviewer_role": "reviewer", "criteria": [{"id": "period"}],
            },
        })

    def _extra_security_gate_input(self, state: dict) -> dict:
        context = state.get("input_context", {})
        errors = validate_internal_context(context, self._config)
        if isinstance(context, dict):
            for evidence in context.get("evidence_index", []):
                if isinstance(evidence, dict) and isinstance(evidence.get("title"), str) and detect_pii(evidence["title"]):
                    errors.append("evidence title contains PII")
        reported, approved, degradation, rubric_errors = rubric_state(self._config, context.get("rubric_version") if isinstance(context, dict) else None)
        if errors or rubric_errors:
            return {
                **state,
                "status": AgentStatus.ERROR.value,
                "error_log": [*errors, *rubric_errors],
            }
        # The values are declared here so the main-node S-2 short circuit does
        # not need to infer validity from a missing key.
        return {
            **state,
            "_preprocess_validated": True,
            "rubric_version": reported,
            "rubric_approved": approved,
            "degradation_reason": degradation,
        }

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        ctx = InvocationContext.from_state(state)
        context = state.get("input_context", {})
        if not state.get("_preprocess_validated") or not isinstance(context, dict):
            emit_trace_event("upstream_error_short_circuited", {"stage": "pre_process"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["input context validation did not complete"]}
        degradation = state.get("degradation_reason")
        if degradation:
            emit_trace_event("rubric_not_customer_approved", {"case_id": context["case_id"]}, state)
        emit_trace_event("exam_case_scope_resolved", {"case_id": context["case_id"], "session_id": ctx.session_id}, state)
        return {
            "_preprocess_validated": True,
            "mode": "invoke",
            "case_id": context["case_id"],
            "request_payload_ref": context["request_payload_ref"],
            "evidence_index": context["evidence_index"],
            "response_checklist": context.get("response_checklist", []),
            "owner_directory": context.get("owner_directory", []),
            "rubric_version": state["rubric_version"],
            "rubric_approved": state["rubric_approved"],
            "degradation_reason": degradation,
            "status": AgentStatus.SUCCESS.value,
        }
