"""PB-1/PB-2/PB-3/PB-5 evidence for domain-specific boundaries."""

from __future__ import annotations

import json

from framework.schemas.trust_level import TrustLevel

from src.graph.graph import ExamEvidenceSufficiencyReviewGraph
from src.payload_store import PayloadStore


def _config(store):
    return {
        "max_retry": 3, "memory_enabled": False, "max_evidence_entries": 200,
        "max_requirement_segments": 50, "payload_ttl_seconds": 300,
        "max_quotable_span_chars": 120, "min_quotable_span_chars": 8, "max_text_chars": 200000,
        "timeout_seconds": 60, "payload_store": store,
        "sufficiency_rubric": {
            "version": "1.0", "approved_by": "SAMPLE", "approved_on": "SAMPLE",
            "reviewer_role": "reviewer", "criteria": [{"id": "period"}],
        },
    }


def _invoke_graph(request_text="2026年5月の対象期間を確認する", extra_config=None):
    store = PayloadStore(scope_keys=("case_id",))
    ref = store.put(request_text, scope={"case_id": "CASEPB"}, session_id="pb-session")
    config = _config(store)
    config.update(extra_config or {})
    graph = ExamEvidenceSufficiencyReviewGraph(config=config)
    graph.compile()
    from framework.schemas.invocation_context import InvocationContext

    output = graph.invoke(
        "heading",
        ctx=InvocationContext(session_id="pb-session", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=False),
        input_context={
            "case_id": "CASEPB", "request_payload_ref": ref, "evidence_index": [],
            "response_checklist": [], "owner_directory": [], "rubric_version": None,
        },
    )
    return output, request_text


def test_pb5_serialized_result_has_no_raw_body_and_mutation_is_detected():
    output, body = _invoke_graph()
    serialized = json.loads(json.dumps(output))
    assert body not in json.dumps(serialized, ensure_ascii=False)
    mutated = {**serialized, "output": {**serialized["output"], "request_text": body}}
    assert body in json.dumps(mutated, ensure_ascii=False)


def test_pb3_llm_is_an_injected_fake_only():
    class FakeLlm:
        def complete(self, messages):  # BaseLLM contract: messages only（timeout を足すと契約違反を隠す）
            assert messages
            return {"content": '{"requirement_specs": [{"segment_id": "SEG-1", "extracted": false}]}' }

    output, _ = _invoke_graph(extra_config={"llm": FakeLlm()})
    assert output["status"] == "success"


def test_pb1_domain_audit_events_are_emitted(monkeypatch):
    events: list[str] = []
    import shared.utils.audit_logger as audit_logger

    monkeypatch.setattr(audit_logger, "emit_trace_event", lambda event_type, _payload, _state: events.append(event_type))
    _invoke_graph()
    assert {"exam_case_scope_resolved", "evidence_register_normalized", "requirement_spec_extracted", "coverage_matched", "sufficiency_narrative_drafted", "evidence_provenance_verified", "review_packet_emitted"} <= set(events)
