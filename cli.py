"""AGENTIC STAR Marketplace entry adapter for FIN-C2-188."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 has no Marketplace extra
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-1.0.2 wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import ExamEvidenceSufficiencyReviewGraph
from src.payload_store import PayloadStore

_NAMESPACE = "fin"
_AGENT_NAME = "FIN-C2-188"
_PROVIDER_AGENT_NAME = "fin_c2_188"
_MARKETPLACE_SESSION_ID = "a-star-marketplace"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)


def _validate_scope_declaration() -> None:
    """Reject a malformed reusable-part adaptation before graph construction."""

    if not isinstance(SCOPE_KEYS, tuple):
        raise RuntimeError("Marketplace SCOPE_KEYS must be a tuple")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must not contain duplicates")
    if any(not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier() for key in SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must contain clean identifier strings")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS must exactly match SCOPE_KEYS")
    if any(
        not isinstance(env_name, str) or not env_name or any(character.isspace() for character in env_name)
        for env_name in STG_DEFAULT_SCOPE_ENVS.values()
    ):
        raise RuntimeError("Marketplace scope environment names must be clean non-empty strings")


def _allowed_environment_keys() -> frozenset[str]:
    _validate_scope_declaration()
    return frozenset((*_AZURE_OPENAI_SECRET_KEYS, *(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS)))


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _allowed_environment_keys():
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build exactly the scope declared by ``src.entry_adapter``."""

    _validate_scope_declaration()
    return {scope_key: _require_clean(provider, STG_DEFAULT_SCOPE_ENVS[scope_key]) for scope_key in SCOPE_KEYS}


class _AzureOpenAIClientCompat(BaseLLM):
    """Azure chat client used only when the 1.0.1 wheel lacks the optional client."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions" "?api-version=2024-10-21"
        request = Request(
            url,
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={
                "api-key": self._config["api_key"],
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class MarketplaceAzureLLM(BaseLLM):
    """Resolve Azure values and construct a fresh client for every operation."""

    @staticmethod
    def _new_client() -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure is an optional SDK extra and therefore the sole invocation-time
        # import. Framework, graph, security, and state imports remain eager.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        return cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return self._new_client().bind_tools(tools)


def _marketplace_request_text(user_input: str) -> str:
    """Accept one plain string and reject any JSON-declared non-invoke mode."""

    if not isinstance(user_input, str):
        raise TypeError("Marketplace input must be one plain string")
    if not user_input.strip():
        raise ValueError("Marketplace input must be a non-empty string")
    try:
        decoded = json.loads(user_input)
    except json.JSONDecodeError:
        return user_input
    if isinstance(decoded, dict) and "mode" in decoded and decoded["mode"] != "invoke":
        raise ValueError("Marketplace write and resume modes are not available")
    return user_input


class MarketplaceGraph(ExamEvidenceSufficiencyReviewGraph):
    """Marketplace-only scope, payload isolation, trust, and Azure boundary."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The Marketplace runner compiles before provision_secrets(). Resolve
        # scope and compile-time dependency seams before the runner compiles.
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        source = dict(config or {})
        ttl_seconds = source.get("payload_ttl_seconds", 300)
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("payload_ttl_seconds must be a positive integer")
        marketplace_config = {
            **source,
            "llm": MarketplaceAzureLLM(),
            "payload_store": PayloadStore(scope_keys=SCOPE_KEYS, ttl_seconds=ttl_seconds),
            "trusted_scope": scope,
            "memory_enabled": False,
            "hitl": {"enabled": False},
        }
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation_history is untrusted free text outside S-2. Build
        # the complete internal context here; no runner key is merged into it.
        del input_context, session_id
        request_text = _marketplace_request_text(user_input)
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        payload_store = cast(PayloadStore, self.config["payload_store"])
        request_ref = payload_store.put(
            request_text,
            scope=scope,
            session_id=_MARKETPLACE_SESSION_ID,
        )
        base_ctx = ctx if ctx is not None else InvocationContext()
        marketplace_ctx = replace(
            base_ctx,
            session_id=_MARKETPLACE_SESSION_ID,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        marketplace_input_context = {
            **scope,
            "request_payload_ref": request_ref,
            "evidence_index": [],
            "response_checklist": [],
            "owner_directory": [],
            "rubric_version": None,
        }
        try:
            return cast(
                dict[str, Any],
                super().invoke(
                    request_text,
                    session_id=_MARKETPLACE_SESSION_ID,
                    ctx=marketplace_ctx,
                    input_context=marketplace_input_context,
                ),
            )
        finally:
            payload_store.delete(
                request_ref,
                scope=scope,
                session_id=_MARKETPLACE_SESSION_ID,
            )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore any broader or dotenv-capable runner provider. Startup must
        # validate every Azure value and every data-driven scope value.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
