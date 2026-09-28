"""Public DTO, startup, and isolation integration tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from src.entry_adapter import AuthSource, Operation
from src.api.server import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-jwt-for-testing")
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _payload(**overrides):
    value = {
        "input": "検査要求",
        "session_id": "test-session",
        "case_id": "CASE-TEST",
        "request_text": "2026年5月の対象期間について統制記録を確認する。",
        "evidence_register": [],
    }
    value.update(overrides)
    return value


def _headers():
    return {"authorization": "Bearer mock-jwt-for-testing"}


def test_startup_loads_manifest_and_invokes_graph(client):
    response = client.post("/invoke", json=_payload(), headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["output"]["contract_version"] == "review_packet_v1"
    assert app.state.config["payload_ttl_seconds"] == 300
    assert app.state.payload_store._ttl_seconds == 300


def test_invoke_uses_read_operation_and_all_auth_context_fields(client, monkeypatch):
    captured = {}
    real_authenticate = server.authenticate

    def capture_auth(request, *, operation):
        context = real_authenticate(request, operation=operation)
        captured["operation"] = operation
        captured["context"] = context
        return context

    monkeypatch.setattr(server, "authenticate", capture_auth)

    response = client.post("/invoke", json=_payload(), headers=_headers())

    assert response.status_code == 200
    assert captured["operation"] is Operation.READ_INVOKE
    context = captured["context"]
    assert context.scope == {"case_id": "CASE-TEST"}
    assert context.principal["caller_id"] == "invoke-token:standalone"
    assert context.auth_source is AuthSource.TOKEN
    assert context.trust.value == "VERIFIED_EXTERNAL"


def test_auth_put_invoke_delete_order_uses_transient_payload_only(client, monkeypatch):
    events = []
    store = app.state.payload_store
    real_authenticate = server.authenticate
    real_put = store.put
    real_delete = store.delete

    def authenticate(request, *, operation):
        events.append("authenticate")
        return real_authenticate(request, operation=operation)

    def put(*args, **kwargs):
        events.append("put")
        return real_put(*args, **kwargs)

    def invoke(*args, **kwargs):
        events.append("invoke")
        return {"status": "success"}

    def delete(*args, **kwargs):
        events.append("delete")
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(server, "authenticate", authenticate)
    monkeypatch.setattr(store, "put", put)
    monkeypatch.setattr(store, "delete", delete)
    monkeypatch.setattr(app.state.agent, "invoke", invoke)

    response = client.post("/invoke", json=_payload(), headers=_headers())

    assert response.status_code == 200
    assert events == ["authenticate", "put", "invoke", "delete"]
    assert store._items == {}


def test_anonymous_is_rejected_at_entry(client, monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    response = client.post("/invoke", json=_payload())
    assert response.status_code == 403


def test_missing_request_text_is_400(client):
    value = _payload()
    value.pop("request_text")
    response = client.post("/invoke", json=value, headers=_headers())
    assert response.status_code == 400


def test_external_response_draft_key_is_rejected(client):
    response = client.post("/invoke", json=_payload(response_draft="not allowed"), headers=_headers())
    assert response.status_code == 400


def test_content_text_is_optional_and_uses_deterministic_only(client):
    evidence = {
        "evidence_id": "EV-001",
        "title": "統制記録",
        "doc_type": "control_record",
        "version": "1.0",
        "version_status": "current",
        "coverage": {"period_from": "2026-05-01", "period_to": "2026-05-31"},
    }
    response = client.post("/invoke", json=_payload(evidence_register=[evidence]), headers=_headers())
    assert response.status_code == 200
    output = response.json()["output"]
    assert output["evaluation_state"] == "deterministic_only"
    assert "evidence_content_unavailable" not in (output["degradation_reason"] or "")


def test_unextracted_requirements_do_not_create_period_gap_and_are_coded(client):
    evidence = {
        "evidence_id": "EV-001", "title": "与信審査統制 実施記録", "doc_type": "control_record",
        "version": "2.1", "version_status": "current",
        "coverage": {
            "period_from": "2026-04-01", "period_to": "2026-05-31",
            "population_scope": "retail_loans", "approval_authority": "branch_manager",
        },
        "owner_id": "OWN-12",
    }
    response = client.post(
        "/invoke",
        json=_payload(
            request_text="2026年4月1日から2026年6月30日までの与信審査統制の実施状況を、承認記録と実施証跡を含めて示せ。",
            evidence_register=[evidence],
            owner_directory=[{"owner_id": "OWN-12", "role_label": "与信管理課"}],
        ),
        headers=_headers(),
    )
    output = response.json()["output"]
    assert output["evaluation_state"] == "deterministic_only"
    assert not [item for item in output["observations"] if item["observation_kind"] == "period_gap"]
    assert {item["question_code"] for item in output["unresolved_questions"]} == {
        "requirement_period_not_extractable",
        "requirement_population_not_extractable",
        "requirement_approval_authority_not_extractable",
    }
    assert "与信審査統制" in output["requirement_segments"][0]["summary"]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("%PDF-pretend", "binary_magic_bytes"),
        ("正常" + "\ufffd" * 100, "replacement_character_ratio_exceeded"),
        ("正常" + "\x01" * 100, "control_character_ratio_exceeded"),
    ],
)
def test_input_quality_gate_returns_reason_code(client, text, reason):
    response = client.post("/invoke", json=_payload(request_text=text), headers=_headers())
    assert response.status_code == 400
    assert response.json()["detail"]["rejected"][0]["reason_code"] == reason
