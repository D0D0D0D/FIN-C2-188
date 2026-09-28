"""S-3 closed review-packet envelope."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii

from src.output_envelope import OutputEnvelopeNode
from src.services.validation import assert_closed_review_packet


# Every domain free-text field is enumerated.  ``*`` means list expansion and
# keeps later nested additions from silently escaping the S-3 scan.
_FREE_TEXT_PATHS: tuple[tuple[str, ...], ...] = (
    ("observations", "*", "observation"),
    ("suggested_additional_evidence", "*", "rationale"),
    ("requirement_segments", "*", "summary"),
    ("observations", "*", "source_spans", "*", "text"),
    ("version_conflict_observations", "*", "observation"),
    ("version_conflict_observations", "*", "source_spans", "*", "text"),
    ("suggested_additional_evidence", "*", "source_spans", "*", "text"),
)


class PostProcessNode(OutputEnvelopeNode):
    """Fold only the allowlisted review fields into the framework output slot."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    allowed_modes: ClassVar[tuple[str, ...]] = ("invoke",)
    gate_kind_by_mode: ClassVar[dict[str, str]] = {"invoke": "review"}
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": (
            "mode", "gate_kind", "contract_version", "case_id", "rubric_version", "rubric_approved",
            "evaluation_state", "requires_human_review",
        )
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        result = super().execute(state)
        emit_trace_event("review_packet_emitted", {"case_id": state.get("case_id", "")}, state)
        return result

    def format_invoke(self, state: dict) -> dict[str, Any]:
        return {
            "contract_version": "review_packet_v1",
            "case_id": state.get("case_id", ""),
            "rubric_version": state.get("rubric_version", ""),
            "rubric_approved": bool(state.get("rubric_approved")),
            "evaluation_state": state.get("evaluation_state", "not_performed"),
            "requirement_segments": state.get("requirement_segments", []),
            "observations": state.get("observations", []),
            "version_conflict_observations": state.get("version_conflict_observations", []),
            "suggested_additional_evidence": state.get("suggested_additional_evidence", []),
            "accountable_owners": state.get("accountable_owners", []),
            "evidence_sources": state.get("evidence_sources", []),
            "unresolved_questions": state.get("unresolved_questions", []),
            "requires_human_review": True,
            "degradation_reason": state.get("degradation_reason"),
        }

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        """Reject, rather than mask, PII/credentials in every declared text path."""

        sanitized = super()._sanitize_client_echoes(formatted)
        for path in _FREE_TEXT_PATHS:
            for text in _texts_at_path(sanitized, path):
                if detect_pii(text) or detect_credentials(text):
                    raise SecurityViolationError("S-3 free-text PII or credential detected")
        # title is not a free-text output field but is still client-visible.
        for text in _all_strings(sanitized):
            if detect_credentials(text):
                raise SecurityViolationError("S-3 credential detected")
        return sanitized

    def _extra_security_gate_output(self, result: dict) -> dict:
        result = super()._extra_security_gate_output(result)
        formatted = result.get("formatted_output")
        if not isinstance(formatted, dict):
            raise SecurityViolationError("S-3 formatted output is missing")
        # gate_kind is an internal OutputEnvelope routing declaration.  It was
        # verified by the part above and is intentionally not in review_packet_v1.
        formatted.pop("gate_kind", None)
        assert_closed_review_packet(
            formatted,
            max_span=int(self._config.get("max_quotable_span_chars", 120)),
            min_span=int(self._config.get("min_quotable_span_chars", 8)),
        )
        result["formatted_output"] = formatted
        return result


def _texts_at_path(value: Any, path: tuple[str, ...]):
    if not path:
        if isinstance(value, str):
            yield value
        return
    head, *tail = path
    if head == "*" and isinstance(value, list):
        for item in value:
            yield from _texts_at_path(item, tuple(tail))
    elif isinstance(value, dict) and head in value:
        yield from _texts_at_path(value[head], tuple(tail))


def _all_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)
