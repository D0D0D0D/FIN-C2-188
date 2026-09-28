"""Regression fixture captured from the mandatory real-LLM validation run."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import ExamEvidenceSufficiencyReviewGraph
from src.nodes.inner.stages import ProvenanceVerifyNode, SufficiencyNarrativeNode
from src.payload_store import PayloadStore


class CapturedResponseLlm:
    """The two malformed-but-plausible responses observed from the real LLM."""

    def __init__(self) -> None:
        self._responses = [
            {
                "requirement_specs": [
                    {
                        "segment_id": "SEG-1",
                        "extracted": False,
                        "period": ["2026-04", "2026-05", "2026-06"],
                        "population": "retail_loans",
                        "control_action": "与信審査統制の実施",
                        "approval_authority": None,
                    }
                ]
            },
            {
                "observations": [
                    {
                        "segment_id": "SEG-1",
                        "observation_kind": "period_gap",
                        "evidence_ids": ["EV-001"],
                        "observation": "登録済み証拠の対象期間は2026年5月までであり、2026年6月の記録は確認できません。",
                        "uncertainty": "medium",
                        "observation_params": {"missing_periods": ["2026-06"]},
                        "source_spans": [
                            {
                                "evidence_id": "EV-001",
                                "span_kind": "date",
                                "start": 13,
                                "end": 36,
                                "text": "2026年4月1日から2026年5月31日まで",
                            }
                        ],
                    }
                ],
                "suggested_additional_evidence": [{
                    "segment_id": "SEG-1",
                    "evidence_type": "2026年6月の与信審査対象一覧およびレビュー実施記録",
                    "rationale": "6月分の対象一覧と実施記録を確認する。",
                    "source_spans": [],
                }],
            },
        ]

    def complete(self, messages):  # BaseLLM contract: messages only（timeout を足すと契約違反を隠す）
        assert messages
        return {"content": json.dumps(self._responses.pop(0), ensure_ascii=False)}


def test_captured_real_llm_response_is_normalized_into_closed_output_schema():
    store = PayloadStore(scope_keys=("case_id",))
    case_id, session_id = "EXAM-2026-001", "captured-llm"
    request_ref = store.put(
        "2026年4月1日から2026年6月30日までの与信審査統制の実施状況を、承認記録と実施証跡を含めて示せ。",
        scope={"case_id": case_id},
        session_id=session_id,
    )
    content_ref = store.put(
        "abcdefghijklm2026年4月1日から2026年5月31日まで",
        scope={"case_id": case_id},
        session_id=session_id,
    )
    config = {
        "max_retry": 3,
        "memory_enabled": False,
        "max_evidence_entries": 200,
        "max_requirement_segments": 50,
        "payload_ttl_seconds": 300,
        "max_quotable_span_chars": 120,
        "min_quotable_span_chars": 8,
        "max_text_chars": 200000,
        "timeout_seconds": 60,
        "payload_store": store,
        "llm": CapturedResponseLlm(),
        "sufficiency_rubric": {
            "version": "1.0",
            "approved_by": "SAMPLE",
            "approved_on": "SAMPLE",
            "reviewer_role": "reviewer",
            "criteria": [{"id": "period"}],
        },
    }
    graph = ExamEvidenceSufficiencyReviewGraph(config=config)
    result = graph.invoke(
        "検査要求 R-2",
        ctx=InvocationContext(
            session_id=session_id,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        ),
        input_context={
            "case_id": case_id,
            "request_payload_ref": request_ref,
            "evidence_index": [
                {
                    "evidence_id": "EV-001",
                    "title": "与信審査統制 実施記録",
                    "doc_type": "control_record",
                    "version": "2.1",
                    "version_status": "current",
                    "coverage": {
                        "period_from": "2026-04-01",
                        "period_to": "2026-05-31",
                        "population_scope": "retail_loans",
                        "approval_authority": "branch_manager",
                    },
                    "owner_id": "OWN-12",
                    "content_payload_ref": content_ref,
                }
            ],
            "response_checklist": [],
            "owner_directory": [{"owner_id": "OWN-12", "role_label": "与信管理課"}],
            "rubric_version": None,
        },
    )

    assert result["status"] == "success"
    output = result["output"]
    period = next(item for item in output["observations"] if item["observation_kind"] == "period_gap")
    assert period["observation_params"] == {"missing_periods": ["2026-06"]}
    llm_period = next(item for item in output["observations"] if item["origin"] == "llm")
    assert set(llm_period) == {
        "observation_id", "segment_id", "observation_kind", "origin", "evidence_ids",
        "observation", "uncertainty", "review_priority", "observation_params", "source_spans",
    }
    assert llm_period["observation_id"] == "OBS-2"
    assert output["suggested_additional_evidence"][0]["evidence_type"] == "other"


def _run_stage4_then_stage5(narrative_observation):
    class OneResponseLlm:
        def complete(self, messages):  # BaseLLM contract: messages only（timeout を足すと契約違反を隠す）
            assert messages
            return {"content": json.dumps({"observations": [narrative_observation], "suggested_additional_evidence": []}, ensure_ascii=False)}

    store = PayloadStore(scope_keys=("case_id",))
    text = "abcdefghijklm2026年4月1日から2026年5月31日まで"
    ref = store.put(text, scope={"case_id": "CASE"}, session_id="session")
    config = {"payload_store": store, "llm": OneResponseLlm(), "timeout_seconds": 60, "min_quotable_span_chars": 8, "max_quotable_span_chars": 120}
    deterministic = {
        "observation_id": "OBS-1", "segment_id": "SEG-1", "observation_kind": "period_gap",
        "origin": "deterministic", "evidence_ids": ["EV-001"], "observation": "登録済み証拠は抽出済み要求期間をカバーしていません。",
        "uncertainty": "medium", "review_priority": "high", "observation_params": {"missing_periods": ["2026-06"]}, "source_spans": [],
    }
    state = {
        "caller_trust_level": "VERIFIED_EXTERNAL", "correlation_id": "c", "trace_id": "t", "thread_id": "th", "session_id": "session",
        "case_id": "CASE", "evidence_index": [{"evidence_id": "EV-001", "content_payload_ref": ref}],
        "requirement_segments": [{"segment_id": "SEG-1", "summary": "要求", "required_evidence_types": ["period"]}],
        "deterministic_observations": [deterministic], "requirement_specs": [{"segment_id": "SEG-1", "extracted": {"period": True, "population": False, "control_action": False, "approval_authority": False}}],
    }
    drafted = SufficiencyNarrativeNode(config=config).execute(state)
    return ProvenanceVerifyNode(config=config).execute({**state, **drafted})


def test_llm_observation_without_span_is_demoted():
    result = _run_stage4_then_stage5({
        "segment_id": "SEG-1", "observation_kind": "period_gap", "evidence_ids": ["EV-001"], "observation": "2026年6月の記録は確認できません。",
        "uncertainty": "medium", "observation_params": {"missing_periods": ["2026-06"]}, "source_spans": [],
    })
    assert [item for item in result["observations"] if item.get("origin") == "llm"] == []
    assert any(item["question_code"] == "nonquotable_source_requires_human_review" for item in result["unresolved_questions"])


def test_llm_observation_with_non_deterministic_params_is_demoted():
    result = _run_stage4_then_stage5({
        "segment_id": "SEG-1", "observation_kind": "period_gap", "evidence_ids": ["EV-001"], "observation": "2099年1月の記録は確認できません。",
        "uncertainty": "medium", "observation_params": {"missing_periods": ["2099-01"]},
        "source_spans": [{"evidence_id": "EV-001", "span_kind": "date", "start": 13, "end": 36, "text": "2026年4月1日から2026年5月31日まで"}],
    })
    assert [item for item in result["observations"] if item.get("origin") == "llm"] == []
    assert any(item["question_code"] == "nonquotable_source_requires_human_review" for item in result["unresolved_questions"])


class _UnverifiedSpanLlm:
    """suggestion の source_spans に原文を入れ、offset だけ外して返す実 LLM 相当の形。

    span 検証は `invalid_source_span` を返すが、以前はその理由でも suggestion を採用して
    いたため、**検証を通っていない原文が顧客出力へ素通り**していた。
    """

    def __init__(self, leaked_text: str) -> None:
        self._leaked = leaked_text
        self._responses = [
            {"requirement_specs": [{
                "segment_id": "SEG-1", "period_from": "2026-04-01", "period_to": "2026-06-30",
                "population_scope": "retail_loans", "control_action": None,
                "approval_authority": "branch_manager",
            }]},
            {"observations": [], "suggested_additional_evidence": [{
                "segment_id": "SEG-1",
                "evidence_type": "2026年6月の与信審査対象一覧",
                "rationale": "6月分を確認する。",
                # text は証拠本文の一部だが start/end が実位置と一致しない
                "source_spans": [{"evidence_id": "EV-001", "span_kind": "date",
                                  "start": 0, "end": 5, "text": leaked_text}],
            }]},
        ]

    def complete(self, messages):
        assert messages
        return {"content": json.dumps(self._responses.pop(0), ensure_ascii=False)}

    def stream(self, messages):
        yield self.complete(messages)["content"]

    def bind_tools(self, tools):
        return self


def test_suggestion_with_unverified_span_does_not_leak_raw_text_into_output():
    """offset が合わない span の原文が顧客出力へ到達しないこと（レビュー R1 Medium-1）。"""
    leaked = "abcdefghijklm2026年4月1日から2026年5月31日まで"
    store = PayloadStore(scope_keys=("case_id",))
    case_id, session_id = "EXAM-2026-002", "unverified-span"
    request_ref = store.put("2026年4月1日から2026年6月30日までの与信審査統制の実施状況を示せ。",
                            scope={"case_id": case_id}, session_id=session_id)
    content_ref = store.put(leaked, scope={"case_id": case_id}, session_id=session_id)
    config = {
        "max_retry": 3, "memory_enabled": False, "max_evidence_entries": 200,
        "max_requirement_segments": 50, "payload_ttl_seconds": 300,
        "max_quotable_span_chars": 120, "min_quotable_span_chars": 8,
        "max_text_chars": 200000, "timeout_seconds": 60,
        "payload_store": store, "llm": _UnverifiedSpanLlm(leaked),
        "sufficiency_rubric": {"version": "1.0", "approved_by": "SAMPLE", "approved_on": "SAMPLE",
                               "reviewer_role": "reviewer", "criteria": [{"id": "period"}]},
    }
    result = ExamEvidenceSufficiencyReviewGraph(config=config).invoke(
        "検査要求 R-3",
        ctx=InvocationContext(session_id=session_id,
                              caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=False),
        input_context={
            "case_id": case_id, "request_payload_ref": request_ref,
            "evidence_index": [{
                "evidence_id": "EV-001", "title": "与信審査統制 実施記録",
                "doc_type": "control_record", "version": "2.1", "version_status": "current",
                "coverage": {"period_from": "2026-04-01", "period_to": "2026-05-31",
                             "population_scope": "retail_loans", "approval_authority": "branch_manager"},
                "owner_id": "OWN-12", "content_payload_ref": content_ref,
            }],
            "response_checklist": [],
            "owner_directory": [{"owner_id": "OWN-12", "role_label": "与信管理課"}],
            "rubric_version": None,
        },
    )

    assert result["status"] == "success"
    serialized = json.dumps(result["output"], ensure_ascii=False)
    assert leaked not in serialized, (
        "検証を通っていない span の原文が顧客出力へ到達している: " + serialized[:400]
    )
