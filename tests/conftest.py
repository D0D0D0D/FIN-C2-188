"""Fixtures for `work/scripts/mrb_live_llm_check.py`."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.payload_store import PayloadStore


@dataclass(frozen=True)
class _LiveCase:
    store: PayloadStore
    case_id: str
    request_ref: str
    content_ref: str


@pytest.fixture
def _live_case() -> _LiveCase:
    store = PayloadStore(scope_keys=("case_id",))
    case_id = "EXAM-2026-001"
    session_id = "live-1"
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
    return _LiveCase(store=store, case_id=case_id, request_ref=request_ref, content_ref=content_ref)


@pytest.fixture
def agent_config(_live_case: _LiveCase) -> dict:
    return {
        "max_retry": 3,
        "memory_enabled": False,
        "max_evidence_entries": 200,
        "max_requirement_segments": 50,
        "payload_ttl_seconds": 300,
        "max_quotable_span_chars": 120,
        "min_quotable_span_chars": 8,
        "max_text_chars": 200000,
        "timeout_seconds": 60,
        "payload_store": _live_case.store,
        "sufficiency_rubric": {
            "version": "1.0",
            "approved_by": "Risk Committee",
            "approved_on": "2026-08-18",
            "reviewer_role": "reviewer",
            "criteria": [{"id": "period"}],
        },
    }


@pytest.fixture
def case_payload() -> str:
    return "検査要求 R-2"


@pytest.fixture
def live_input_context(_live_case: _LiveCase) -> dict:
    return {
        "case_id": _live_case.case_id,
        "request_payload_ref": _live_case.request_ref,
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
                "content_payload_ref": _live_case.content_ref,
            }
        ],
        "response_checklist": [],
        "owner_directory": [{"owner_id": "OWN-12", "role_label": "与信管理課"}],
        "rubric_version": None,
    }
