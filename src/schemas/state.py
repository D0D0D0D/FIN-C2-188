"""State contract for FIN-C2-188.

Only JSON-compatible values belong here.  Raw request and evidence bodies are
kept in the short-lived payload store and never appear in this schema.
"""

from typing import Any, NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    """Flat state shared by the outer graph and its domain workflow."""

    case_id: str
    mode: str
    _preprocess_validated: bool
    request_payload_ref: str
    evidence_index: list[dict[str, Any]]
    response_checklist: list[dict[str, Any]]
    owner_directory: list[dict[str, Any]]
    rubric_version: str
    rubric_approved: bool
    evaluation_state: str
    requirement_segments: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    version_conflict_observations: list[dict[str, Any]]
    suggested_additional_evidence: list[dict[str, Any]]
    accountable_owners: list[dict[str, Any]]
    evidence_sources: list[dict[str, Any]]
    unresolved_questions: list[dict[str, Any]]
    requires_human_review: bool
    degradation_reason: str | None
    error_code: NotRequired[str]
    error_type: NotRequired[str]
    evidence_normalized: NotRequired[list[dict[str, Any]]]
    requirement_specs: NotRequired[list[dict[str, Any]]]
    deterministic_observations: NotRequired[list[dict[str, Any]]]
    coverage_matrix: NotRequired[list[dict[str, Any]]]
    version_conflicts: NotRequired[list[dict[str, Any]]]
    llm_observations: NotRequired[list[dict[str, Any]]]
    register_integrity_findings: NotRequired[list[dict[str, Any]]]
