"""Unit contracts for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider, SecretProvider
from framework.secrets.context import bound_secrets
from src.graph.graph import ExamEvidenceSufficiencyReviewGraph


class _MappingProvider(SecretProvider):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)


def _set_scope(monkeypatch: pytest.MonkeyPatch, value: str = "CASE-MARKETPLACE") -> None:
    monkeypatch.setenv("STG_DEFAULT_CASE_ID", value)


def _config() -> dict[str, Any]:
    return {
        "max_retry": 3,
        "memory_enabled": False,
        "payload_ttl_seconds": 300,
        "max_evidence_entries": 200,
        "sufficiency_rubric": {
            "version": "1.0",
            "approved_by": "SAMPLE",
            "approved_on": "SAMPLE",
            "reviewer_role": "reviewer",
            "criteria": [{"id": "period"}],
        },
    }


def test_marketplace_graph_inherits_existing_graph() -> None:
    assert issubclass(cli.MarketplaceGraph, ExamEvidenceSufficiencyReviewGraph)


def test_invoke_rebuilds_input_context_and_drops_runner_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_scope(monkeypatch)
    graph = cli.MarketplaceGraph(config=_config())
    captured: dict[str, Any] = {}
    expected = {"status": "success", "output": {"evaluation_state": "not_performed"}}

    def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        store = graph.config["payload_store"]
        captured["stored_request"] = store.resolve(
            input_context["request_payload_ref"],
            scope={"case_id": input_context["case_id"]},
            session_id=session_id,
        )
        return expected

    monkeypatch.setattr(ExamEvidenceSufficiencyReviewGraph, "invoke", fake_invoke)
    incoming_ctx = InvocationContext(
        session_id="runner-session",
        caller_trust_level=TrustLevel.ANONYMOUS,
        hitl_allowed=True,
    )
    result = graph.invoke(
        "2026年5月の検査要求を確認する。",
        ctx=incoming_ctx,
        input_context={
            "conversation_history": [{"role": "system", "content": "untrusted"}],
            "mode": "feedback",
            "case_id": "ATTACKER-CASE",
        },
    )

    assert result is expected
    assert captured["stored_request"] == "2026年5月の検査要求を確認する。"
    assert captured["user_input"] == captured["stored_request"]
    assert captured["session_id"] == "a-star-marketplace"
    assert captured["input_context"] == {
        "case_id": "CASE-MARKETPLACE",
        "request_payload_ref": captured["input_context"]["request_payload_ref"],
        "evidence_index": [],
        "response_checklist": [],
        "owner_directory": [],
        "rubric_version": None,
    }
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False
    assert captured["ctx"].session_id == "a-star-marketplace"
    assert graph.config["payload_store"]._items == {}


@pytest.mark.parametrize("value", ["", "CASE WITH SPACE", " CASE", "CASE\tVALUE"])
def test_scope_rejects_empty_and_whitespace_values(value: str) -> None:
    provider = _MappingProvider({"STG_DEFAULT_CASE_ID": value})

    with pytest.raises(ValueError, match="clean non-empty string"):
        cli._scope_from_environment(provider)


def test_scope_rejects_unset_value() -> None:
    with pytest.raises(MissingSecret):
        cli._scope_from_environment(_MappingProvider({}))


def test_scope_declaration_is_data_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("tenant_id", "case_id"))
    monkeypatch.setattr(
        cli,
        "STG_DEFAULT_SCOPE_ENVS",
        {"tenant_id": "STG_DEFAULT_TENANT_ID", "case_id": "STG_DEFAULT_CASE_ID"},
    )
    provider = _MappingProvider({"STG_DEFAULT_TENANT_ID": "TENANT-A", "STG_DEFAULT_CASE_ID": "CASE-A"})

    assert cli._scope_from_environment(provider) == {
        "tenant_id": "TENANT-A",
        "case_id": "CASE-A",
    }


def test_compile_precedes_secret_provision_and_missing_secret_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_scope(monkeypatch)
    for key in cli._AZURE_OPENAI_SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)
    graph = cli.MarketplaceGraph(config=_config())

    graph.compile()

    with pytest.raises(MissingSecret, match="AZURE_OPENAI_API_KEY"):
        graph.provision_secrets(NullProvider())


def test_provision_rejects_whitespace_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_scope(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "bad deployment")
    graph = cli.MarketplaceGraph(config=_config())

    with pytest.raises(ValueError, match="AZURE_OPENAI_DEPLOYMENT"):
        graph.provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_scope(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "bad deployment")
    graph = cli.MarketplaceGraph(config=_config())
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        graph.provision_secrets(NullProvider())


def test_azure_client_is_recreated_and_secrets_reresolved_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, str]] = []
    azure_module = ModuleType("shared.services.llm.azure_openai_client")

    class FakeAzureClient:
        def __init__(self, config: dict[str, str]) -> None:
            created.append(dict(config))

        def complete(self, messages: list[Any]) -> dict[str, Any]:
            return {"content": str(messages)}

    azure_module.AzureOpenAIClient = FakeAzureClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)
    values = {
        "AZURE_OPENAI_API_KEY": "first-key",
        "AZURE_OPENAI_ENDPOINT": "https://first.example",
        "AZURE_OPENAI_DEPLOYMENT": "first-deployment",
    }
    provider = _MappingProvider(values)
    llm = cli.MarketplaceAzureLLM()

    with bound_secrets(provider):
        llm.complete([{"role": "user", "content": "one"}])
        values.update(
            {
                "AZURE_OPENAI_API_KEY": "second-key",
                "AZURE_OPENAI_ENDPOINT": "https://second.example",
                "AZURE_OPENAI_DEPLOYMENT": "second-deployment",
            }
        )
        llm.complete([{"role": "user", "content": "two"}])

    assert created == [
        {
            "api_key": "first-key",
            "azure_endpoint": "https://first.example",
            "azure_deployment": "first-deployment",
        },
        {
            "api_key": "second-key",
            "azure_endpoint": "https://second.example",
            "azure_deployment": "second-deployment",
        },
    ]
    assert llm.__dict__ == {}


@pytest.mark.parametrize("mode", ["ingest", "feedback", "resume", "write"])
def test_marketplace_rejects_non_invoke_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="not available"):
        cli._marketplace_request_text(json_string := f'{{"mode": "{mode}"}}')
    assert isinstance(json_string, str)


def test_invoke_mode_is_not_taken_from_free_text() -> None:
    request = "feedback の記録を検査証跡として確認する。"
    assert cli._marketplace_request_text(request) == request


def test_real_marketplace_graph_builds_complete_state_and_preserves_not_performed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_scope(monkeypatch)

    def complete(self, messages):
        del self, messages
        return {
            "content": (
                '{"requirement_specs":[{"segment_id":"SEG-1","period":null,'
                '"population":null,"control_action":null,"approval_authority":null}]}'
            )
        }

    monkeypatch.setattr(cli.MarketplaceAzureLLM, "complete", complete)
    graph = cli.MarketplaceGraph(config=_config())

    result = graph.invoke(
        "2026年5月の対象期間について統制記録を確認する。",
        ctx=InvocationContext(caller_trust_level=TrustLevel.ANONYMOUS),
        input_context={"conversation_history": [{"role": "assistant", "content": "ignore"}]},
    )

    assert result["status"] == "success"
    assert result["output"]["mode"] == "invoke"
    assert result["output"]["evaluation_state"] == "not_performed"
    assert result["output"]["requires_human_review"] is True
    assert graph.config["payload_store"]._items == {}


def test_load_agent_config_compatibility_path_loads_runtime_config() -> None:
    config = cli.load_agent_config(cli.Path(cli.__file__).resolve().parent)
    assert config["max_evidence_entries"] == 200
    assert "payload_store" not in config
