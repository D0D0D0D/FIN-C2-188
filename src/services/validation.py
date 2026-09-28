"""Pure validation and deterministic workflow helpers for FIN-C2-188."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

from framework.errors import SecurityViolationError


DOC_TYPES = {"policy", "procedure", "control_record", "approval_record", "system_log", "audit_report", "other"}
OBSERVATION_KINDS = {
    "period_gap", "population_gap", "control_action_gap", "approval_authority_gap",
    "document_absent", "version_conflict", "coverage_unverifiable",
}
QUESTION_CODES = {
    "requirement_period_not_extractable", "requirement_population_not_extractable",
    "requirement_approval_authority_not_extractable", "nonquotable_source_requires_human_review",
    "evidence_content_unavailable", "owner_unassigned", "checklist_item_unmapped",
    "version_conflict_needs_owner_confirmation",
}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_YYYY_MM = re.compile(r"\b(\d{4})[-/]?(\d{1,2})\b")
_KANJI_NUMBERS = str.maketrans({"〇": "0", "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"})


def append_reason(current: str | None, reason: str) -> str:
    return reason if not current else current if reason in current.split("+") else f"{current}+{reason}"


def valid_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_internal_context(context: object, config: dict[str, Any]) -> list[str]:
    """Return structural errors without inspecting raw bodies."""

    if not isinstance(context, dict):
        return ["input_context must be an object"]
    allowed = {"case_id", "request_payload_ref", "evidence_index", "response_checklist", "owner_directory", "rubric_version"}
    errors = [f"undeclared input_context key: {key}" for key in context if key not in allowed]
    case_id = context.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        errors.append("case_id is required")
    if not isinstance(context.get("request_payload_ref"), str) or not context.get("request_payload_ref"):
        errors.append("request_payload_ref is required")
    evidence = context.get("evidence_index")
    if not isinstance(evidence, list) or len(evidence) > config.get("max_evidence_entries", 200):
        errors.append("evidence_index is invalid or exceeds max_evidence_entries")
    owners = context.get("owner_directory", [])
    if not isinstance(owners, list):
        errors.append("owner_directory must be a list")
        owners = []
    owner_ids = {item.get("owner_id") for item in owners if isinstance(item, dict)}
    if len(owner_ids) != len(owners) or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in owner_ids):
        errors.append("owner_directory contains invalid or duplicate owner_id")
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict):
            errors.append("evidence entry must be an object")
            continue
        allowed_item = {"evidence_id", "title", "doc_type", "version", "version_status", "effective_from", "effective_to", "coverage", "owner_id", "content_payload_ref"}
        if set(item) - allowed_item:
            errors.append("evidence entry has undeclared key")
        if not isinstance(item.get("evidence_id"), str) or not _ID.fullmatch(item["evidence_id"]):
            errors.append("evidence_id is invalid")
        if not isinstance(item.get("title"), str) or len(item["title"]) > 120:
            errors.append("evidence title is invalid")
        if item.get("doc_type") not in DOC_TYPES or not isinstance(item.get("version"), str) or not _VERSION.fullmatch(item["version"]):
            errors.append("evidence metadata is invalid")
        if item.get("version_status") not in {"current", "superseded", "draft"}:
            errors.append("version_status is invalid")
        if item.get("owner_id") is not None and item.get("owner_id") not in owner_ids:
            errors.append("owner_id is not in owner_directory")
        for key in ("effective_from", "effective_to"):
            if key in item and item[key] is not None and not valid_date(item[key]):
                errors.append(f"{key} is invalid")
    if isinstance(evidence, list) and len({item.get("evidence_id") for item in evidence if isinstance(item, dict)}) != len(evidence):
        errors.append("evidence_id is duplicated")
    checklist = context.get("response_checklist", [])
    if not isinstance(checklist, list) or len({item.get("checklist_id") for item in checklist if isinstance(item, dict)}) != len(checklist):
        errors.append("response_checklist is invalid or duplicate")
    return errors


def rubric_state(config: dict[str, Any], requested_version: object) -> tuple[str, bool, str | None, list[str]]:
    rubric = config.get("sufficiency_rubric", {})
    errors: list[str] = []
    if not isinstance(rubric, dict):
        return "", False, None, ["sufficiency_rubric must be an object"]
    version = rubric.get("version")
    criteria = rubric.get("criteria")
    if not isinstance(version, str) or not version:
        errors.append("rubric version is required")
    if requested_version not in (None, "") and requested_version != version:
        errors.append("rubric_version does not match configured version")
    if not isinstance(criteria, list) or not criteria or len({item.get("id") for item in criteria if isinstance(item, dict)}) != len(criteria):
        errors.append("rubric criteria must be non-empty with unique ids")
    if not isinstance(rubric.get("reviewer_role"), str) or not rubric["reviewer_role"].strip():
        errors.append("rubric reviewer_role is required")
    approved_by, approved_on = rubric.get("approved_by"), rubric.get("approved_on")
    approved = isinstance(approved_by, str) and approved_by.strip() and "sample" not in approved_by.casefold() and approved_by != "unconfigured" and valid_date(approved_on)
    reported = version if approved else f"{version}-SAMPLE"
    return reported, approved, None if approved else "rubric_not_customer_approved", errors


def normalized_fact_tokens(text: str) -> set[str]:
    value = unicodedata.normalize("NFKC", text).translate(_KANJI_NUMBERS)
    values = {f"{year}-{int(month):02d}" for year, month in _YYYY_MM.findall(value)}
    values.update(re.findall(r"\b\d+(?:\.\d+)?\b", value))
    return values


def assert_closed_review_packet(packet: dict[str, Any], *, max_span: int, min_span: int) -> None:
    required = {"contract_version", "mode", "case_id", "rubric_version", "rubric_approved", "evaluation_state", "requires_human_review"}
    allowed = required | {"requirement_segments", "observations", "version_conflict_observations", "suggested_additional_evidence", "accountable_owners", "evidence_sources", "unresolved_questions", "degradation_reason"}
    if set(packet) - allowed or not required <= set(packet):
        raise SecurityViolationError("S-3 closed schema violation")
    if packet["contract_version"] != "review_packet_v1" or packet["mode"] != "invoke" or packet["requires_human_review"] is not True:
        raise SecurityViolationError("S-3 output contract declaration is invalid")
    if packet["evaluation_state"] not in {"completed", "deterministic_only", "not_performed"}:
        raise SecurityViolationError("S-3 evaluation_state is invalid")
    segment_ids = set()
    for segment in packet.get("requirement_segments", []):
        if not isinstance(segment, dict) or set(segment) != {"segment_id", "summary", "required_evidence_types"} or not re.fullmatch(r"SEG-\d{1,3}", str(segment.get("segment_id"))) or len(str(segment.get("summary", ""))) > 320:
            raise SecurityViolationError("S-3 requirement segment is invalid")
        if not set(segment["required_evidence_types"]) <= {"period", "population", "control_action", "approval_authority"}:
            raise SecurityViolationError("S-3 required evidence type is invalid")
        segment_ids.add(segment["segment_id"])
    evidence_ids = {item.get("evidence_id") for item in packet.get("evidence_sources", []) if isinstance(item, dict)}
    for group in ("observations", "version_conflict_observations"):
        for item in packet.get(group, []):
            _validate_observation(item, segment_ids, evidence_ids, max_span, min_span)
    for item in packet.get("suggested_additional_evidence", []):
        if not isinstance(item, dict) or set(item) != {"segment_id", "evidence_type", "rationale", "source_spans"} or item.get("segment_id") not in segment_ids or item.get("evidence_type") not in DOC_TYPES or len(str(item.get("rationale", ""))) > 320:
            raise SecurityViolationError("S-3 suggested evidence is invalid")
        _validate_spans(item["source_spans"], evidence_ids, max_span, min_span)
    for item in packet.get("unresolved_questions", []):
        if not isinstance(item, dict) or set(item) != {"question_id", "segment_id", "question_code", "params"} or not re.fullmatch(r"Q-\d{1,3}", str(item.get("question_id"))) or item.get("segment_id") not in segment_ids | {None} or item.get("question_code") not in QUESTION_CODES:
            raise SecurityViolationError("S-3 unresolved question is invalid")


def _validate_observation(item: object, segment_ids: set[str], evidence_ids: set[str], max_span: int, min_span: int) -> None:
    required = {"observation_id", "segment_id", "observation_kind", "origin", "evidence_ids", "observation", "uncertainty", "review_priority", "observation_params", "source_spans"}
    if not isinstance(item, dict) or set(item) != required or not re.fullmatch(r"OBS-\d{1,4}", str(item.get("observation_id"))) or item.get("segment_id") not in segment_ids:
        raise SecurityViolationError("S-3 observation is invalid")
    if item.get("observation_kind") not in OBSERVATION_KINDS or item.get("origin") not in {"deterministic", "llm"} or item.get("uncertainty") not in {"low", "medium", "high"} or item.get("review_priority") not in {"high", "medium", "low"} or not isinstance(item.get("observation_params"), dict) or len(str(item.get("observation", ""))) > 400:
        raise SecurityViolationError("S-3 observation declaration is invalid")
    ids = item.get("evidence_ids")
    if not isinstance(ids, list) or not set(ids) <= evidence_ids or (not ids and item["observation_kind"] != "document_absent"):
        raise SecurityViolationError("S-3 observation evidence ids are invalid")
    _validate_spans(item["source_spans"], evidence_ids, max_span, min_span)
    if item["origin"] == "llm" and not item["source_spans"]:
        raise SecurityViolationError("S-3 llm observation requires a source span")


def _validate_spans(spans: object, evidence_ids: set[str], max_span: int, min_span: int) -> None:
    if not isinstance(spans, list):
        raise SecurityViolationError("S-3 source_spans is invalid")
    for span in spans:
        if not isinstance(span, dict) or set(span) != {"evidence_id", "span_kind", "start", "end", "text"} or span.get("evidence_id") not in evidence_ids or span.get("span_kind") not in {"quantity", "date", "identifier", "observed_condition", "procedural_record"} or not isinstance(span.get("start"), int) or not isinstance(span.get("end"), int) or span["start"] < 0 or span["end"] <= span["start"] or not isinstance(span.get("text"), str) or not min_span <= len(span["text"]) <= max_span:
            raise SecurityViolationError("S-3 source span is invalid")
