"""The five stages of the FIN-C2-188 evidence review workflow.

The stages deliberately return small deltas.  Raw bodies are resolved only
while needed and are never copied into state or a node result.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.evidence_gate import (
    EvidenceGateNode,
    FindingField,
    ProvenanceContract,
    project_typed_findings,
    validate_provenance,
)
from src.llm_draft_stage import parse_json_object
from src.services.payload_service import resolve_text
from src.services.span_classifier import classify_span
from src.services.validation import (
    DOC_TYPES,
    OBSERVATION_KINDS,
    append_reason,
    normalized_fact_tokens,
)
from src.payload_store import PayloadStore


_LLM_OBSERVATION_FIELDS = (
    FindingField("segment_id", str, required=True),
    FindingField(
        "observation_kind",
        str,
        required=True,
        allowed_values=tuple(sorted(OBSERVATION_KINDS)),
    ),
    FindingField("evidence_ids", list, required=True, item_type=str),
    FindingField("observation", str, required=True),
    FindingField(
        "uncertainty",
        str,
        required=True,
        allowed_values=("low", "medium", "high"),
    ),
    FindingField("observation_params", dict, required=True),
    FindingField("source_spans", list, required=True),
)

_LLM_SUGGESTION_FIELDS = (
    FindingField("segment_id", str, required=True),
    FindingField("evidence_type", str, required=True),
    FindingField("rationale", str, required=True),
    FindingField("source_spans", list, required=True),
)


def _default_config(config: dict[str, Any] | None) -> dict[str, Any]:
    values = dict(config or {})
    values.setdefault("payload_store", PayloadStore(scope_keys=("case_id",)))
    return values


def _error_short_circuit(state: dict, stage: str) -> dict:
    if state.get("status") == AgentStatus.ERROR.value:
        emit_trace_event("upstream_error_short_circuited", {"stage": stage}, state)
    return state


def _llm_error_result(state: dict, *, stage: str, error_code: str, exc: Exception) -> dict:
    """Return stable failure metadata without persisting provider-controlled text."""

    error_type = type(exc).__name__
    emit_trace_event(
        "llm_stage_error",
        {"stage": stage, "error_code": error_code, "error_type": error_type},
        state,
    )
    return {
        "status": AgentStatus.ERROR.value,
        "error_code": error_code,
        "error_type": error_type,
        "error_log": [f"{stage}: {error_code} ({error_type})"],
    }


class _WorkflowStage(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: dict) -> dict:
        return _error_short_circuit(state, self.__class__.__name__)


class EvidenceNormalizeNode(_WorkflowStage):
    """Resolve and segment the request, while normalizing evidence metadata."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = _default_config(config)

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        ctx = InvocationContext.from_state(state)
        try:
            envelope = json.loads(state.get("user_input", ""))
            if not isinstance(envelope, dict):
                raise ValueError("inner envelope is not an object")
            store = self._config["payload_store"]
            request_text = resolve_text(store, envelope["request_payload_ref"], case_id=envelope["case_id"], session_id=ctx.session_id)
        except Exception as exc:  # fail closed: request text is required
            emit_trace_event("evaluation_degraded", {"degradation_reason": "request_payload_unavailable"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"Stage 1 request payload unavailable: {exc}"]}
        emit_trace_event("evidence_payload_resolved", {"evidence_id": "request", "stage": "stage1"}, state)
        chunks = [part.strip() for part in re.split(r"(?:\n+|(?=^\s*\d+[.)])|(?=^\s*[-*]) )", request_text, flags=re.MULTILINE) if part.strip()]
        if not chunks:
            chunks = [request_text.strip()] if request_text.strip() else []
        max_segments = int(self._config.get("max_requirement_segments", 50))
        chunks = chunks[:max_segments]
        segments = [
            {
                "segment_id": f"SEG-{index}",
                "summary": _segment_summary(index, text),
                "required_evidence_types": _required_types(text),
            }
            for index, text in enumerate(chunks, 1)
        ]
        evidence = [dict(item) for item in envelope.get("evidence_index", []) if isinstance(item, dict)]
        findings = _register_findings(evidence)
        sources = [
            {key: item.get(key, "") for key in ("evidence_id", "title", "version", "version_status")}
            for item in evidence
        ]
        emit_trace_event("evidence_register_normalized", {"segments": len(segments), "evidence": len(evidence)}, state)
        return {
            "case_id": envelope["case_id"],
            "request_payload_ref": envelope["request_payload_ref"],
            "evidence_index": evidence,
            "response_checklist": envelope.get("response_checklist", []),
            "owner_directory": envelope.get("owner_directory", []),
            "rubric_version": envelope["rubric_version"],
            "rubric_approved": bool(envelope.get("rubric_approved")),
            "degradation_reason": envelope.get("degradation_reason"),
            "requirement_segments": segments,
            "evidence_normalized": evidence,
            "evidence_sources": sources,
            "register_integrity_findings": findings,
            "status": AgentStatus.SUCCESS.value,
        }


class RequirementSpecNode(_WorkflowStage):
    """LLM seam for typed requirement extraction, with deterministic fallback."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = _default_config(config)

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        ctx = InvocationContext.from_state(state)
        fallback = [_fallback_spec(segment) for segment in state.get("requirement_segments", [])]
        llm = self._config.get("llm")
        if llm is None:
            emit_trace_event("evaluation_degraded", {"degradation_reason": "llm_not_configured"}, state)
            emit_trace_event("requirement_spec_extracted", {"source": "fallback"}, state)
            return {
                "requirement_specs": fallback,
                "degradation_reason": append_reason(state.get("degradation_reason"), "llm_not_configured"),
                "status": AgentStatus.SUCCESS.value,
            }
        # Stage 2 の責務（自由記述の要求を証拠種別へ当てはめる）は本文なしには成立しない。
        # 他ステージと同じ規律で execute() 内だけで一時解決し、戻り値には含めない。
        request_text = ""
        ref = state.get("request_payload_ref")
        if ref:
            try:
                request_text = resolve_text(
                    self._config["payload_store"], ref,
                    case_id=state.get("case_id", ""), session_id=ctx.session_id,
                )
                emit_trace_event("evidence_payload_resolved", {"evidence_id": "request", "stage": "stage2"}, state)
            except Exception:  # noqa: BLE001 - 本文が無くてもセグメント要約だけで続行する
                emit_trace_event("evaluation_degraded", {"degradation_reason": "request_payload_unavailable_stage2"}, state)
        prompt = _requirement_prompt(
            state.get("requirement_segments", []),
            request_text=request_text,
            evidence=state.get("evidence_normalized", []),
        )
        try:
            # BaseLLM.complete(messages) — the ABC takes no timeout parameter, so
            # passing one raises TypeError against any conforming client and the
            # stage degrades to llm_call_failed on every call.  Per-call timeouts
            # belong to the client's own construction, not to this call site.
            response = llm.complete([{"role": "user", "content": prompt}])
        except Exception as exc:  # noqa: BLE001
            return _llm_error_result(
                state,
                stage="RequirementSpecNode",
                error_code="llm_call_failed",
                exc=exc,
            )
        try:
            draft = parse_json_object(response.get("content", "") if isinstance(response, dict) else str(response))
            specs = draft.get("requirement_specs")
            if not isinstance(specs, list) or len(specs) != len(fallback):
                raise ValueError("typed requirement_specs are missing or incomplete")
            result = [_coerce_spec(spec, fallback[index]) for index, spec in enumerate(specs)]
        except Exception as exc:  # noqa: BLE001
            return _llm_error_result(
                state,
                stage="RequirementSpecNode",
                error_code="llm_output_parse_failed",
                exc=exc,
            )
        emit_trace_event("requirement_spec_extracted", {"count": len(result)}, state)
        return {
            "requirement_specs": result,
            "degradation_reason": state.get("degradation_reason"),
            "status": AgentStatus.SUCCESS.value,
        }


class CoverageMatchNode(_WorkflowStage):
    """Deterministic coverage and version matching."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        InvocationContext.from_state(state)
        observations: list[dict[str, Any]] = []
        matrix: list[dict[str, Any]] = []
        evidence = state.get("evidence_normalized", [])
        for segment, spec in zip(state.get("requirement_segments", []), state.get("requirement_specs", [])):
            segment_id = segment["segment_id"]
            matched = [record for record in evidence if _record_matches(segment, record)]
            matrix.append({"segment_id": segment_id, "evidence_ids": [record["evidence_id"] for record in matched]})
            if not matched:
                observations.append(_observation(len(observations) + 1, segment_id, "document_absent", [], "要求に対応する証拠種別が証拠登録簿にありません。", {"required_evidence_type": "other"}, "high"))
                continue
            required_months = _required_months(spec.get("period")) if _axis_extracted(spec, "period") else []
            missing_months = [
                month for month in required_months
                if not any(_covers_period(record.get("coverage", {}), month) for record in matched)
            ]
            if missing_months:
                observations.append(_observation(len(observations) + 1, segment_id, "period_gap", [record["evidence_id"] for record in matched], "登録済み証拠は抽出済み要求期間をカバーしていません。", {"missing_periods": missing_months}, "high"))
            required_population = spec.get("population")
            if _axis_extracted(spec, "population") and isinstance(required_population, str) and required_population and not any(record.get("coverage", {}).get("population_scope") == required_population for record in matched):
                covered = next((record.get("coverage", {}).get("population_scope") for record in matched if record.get("coverage", {}).get("population_scope")), "unverified")
                observations.append(_observation(len(observations) + 1, segment_id, "population_gap", [record["evidence_id"] for record in matched], "登録済み証拠の母集団が抽出済み要求と一致しません。", {"required_population": required_population, "covered_population": covered}, "medium"))
            required_authority = spec.get("approval_authority")
            if _axis_extracted(spec, "approval_authority") and isinstance(required_authority, str) and required_authority and not any(record.get("coverage", {}).get("approval_authority") == required_authority for record in matched):
                authority = next((record.get("coverage", {}).get("approval_authority") for record in matched if record.get("coverage", {}).get("approval_authority")), "unverified")
                observations.append(_observation(len(observations) + 1, segment_id, "approval_authority_gap", [record["evidence_id"] for record in matched], "登録済み証拠の承認権限が抽出済み要求と一致しません。", {"required_authority": required_authority, "evidence_authority": authority}, "medium"))
        versions = _version_conflicts(evidence, start=len(observations) + 1)
        observations.extend(versions)
        owners = _owners_for_observations(observations, evidence)
        unresolved = _checklist_questions(state.get("response_checklist", []), state.get("requirement_segments", []))
        emit_trace_event("coverage_matched", {"observations": len(observations)}, state)
        return {
            "deterministic_observations": observations,
            "version_conflicts": versions,
            "coverage_matrix": matrix,
            "accountable_owners": owners,
            "unresolved_questions": unresolved,
            "status": AgentStatus.SUCCESS.value,
        }


class SufficiencyNarrativeNode(_WorkflowStage):
    """LLM seam for semantic observations; never returns raw source text."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = _default_config(config)

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        ctx = InvocationContext.from_state(state)
        usable: list[dict[str, Any]] = []
        unavailable = False
        store = self._config["payload_store"]
        for record in state.get("evidence_index", []):
            ref = record.get("content_payload_ref")
            if ref is None:
                continue
            try:
                text = resolve_text(store, ref, case_id=state["case_id"], session_id=ctx.session_id)
            except Exception:
                unavailable = True
                continue
            emit_trace_event("evidence_payload_resolved", {"evidence_id": record["evidence_id"], "stage": "stage4"}, state)
            usable.append({"evidence_id": record["evidence_id"], "excerpt": text[: self._config.get("max_quotable_span_chars", 120)]})
        llm = self._config.get("llm")
        reason = state.get("degradation_reason")
        if unavailable:
            reason = append_reason(reason, "evidence_content_unavailable")
        if llm is None or not usable:
            if llm is None:
                reason = append_reason(reason, "llm_not_configured")
            emit_trace_event("evaluation_degraded", {"degradation_reason": reason or "no_evidence_content"}, state)
            emit_trace_event("sufficiency_narrative_drafted", {"count": 0}, state)
            return {"llm_observations": [], "suggested_additional_evidence": [], "degradation_reason": reason, "status": AgentStatus.SUCCESS.value}
        try:
            # See RequirementSpecNode — BaseLLM.complete() takes messages only.
            response = llm.complete([{"role": "user", "content": _narrative_prompt(state, usable)}])
        except Exception as exc:  # noqa: BLE001
            return _llm_error_result(
                state,
                stage="SufficiencyNarrativeNode",
                error_code="llm_call_failed",
                exc=exc,
            )
        try:
            parsed = parse_json_object(response.get("content", "") if isinstance(response, dict) else str(response))
        except Exception as exc:  # noqa: BLE001
            return _llm_error_result(
                state,
                stage="SufficiencyNarrativeNode",
                error_code="llm_output_parse_failed",
                exc=exc,
            )
        if set(parsed) != {"observations", "suggested_additional_evidence"}:
            from framework.errors import SecurityViolationError

            raise SecurityViolationError("S-3 LLM narrative contains undeclared top-level fields")
        observations = project_typed_findings(parsed["observations"], _LLM_OBSERVATION_FIELDS)
        suggestions = project_typed_findings(
            parsed["suggested_additional_evidence"],
            _LLM_SUGGESTION_FIELDS,
        )
        # Framework-owned origin is added only after the LLM DTO has passed the
        # typed allowlist, so callers cannot smuggle a conflicting value.
        observations = [{**item, "origin": "llm"} for item in observations]
        suggestions = [{**item, "origin": "llm"} for item in suggestions]
        emit_trace_event("sufficiency_narrative_drafted", {"count": len(observations)}, state)
        return {"llm_observations": observations, "suggested_additional_evidence": suggestions, "degradation_reason": reason, "status": AgentStatus.SUCCESS.value}


class ProvenanceVerifyNode(EvidenceGateNode):
    """Stage 5: source, span, and deterministic-fact verification."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        self._domain_contract = ProvenanceContract(
            gate_kind="review",
            cited_ids_key="evidence_ids",
            retrieved_records_key="evidence_index",
            evidence_map_key="observations",
            record_id_keys=("evidence_id",),
            claim_source_keys=("evidence_ids",),
            passthrough_keys=("evaluation_state", "degradation_reason"),
        )
        super().__init__(contract=self._domain_contract)
        self._config = _default_config(config)

    def _extra_security_gate_input(self, state: dict) -> dict:
        return _error_short_circuit(state, self.__class__.__name__)

    def _extra_security_gate_output(self, result: dict) -> dict:
        # The generic part's output contract targets a draft envelope.  This
        # stage returns workflow deltas; domain provenance checks run in execute.
        return result

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        ctx = InvocationContext.from_state(state)
        evidence_by_id = {item["evidence_id"]: item for item in state.get("evidence_index", [])}
        raw_by_id: dict[str, str] = {}
        unavailable: list[str] = []
        for record in evidence_by_id.values():
            if "content_payload_ref" not in record:
                continue
            try:
                raw_by_id[record["evidence_id"]] = resolve_text(self._config["payload_store"], record["content_payload_ref"], case_id=state["case_id"], session_id=ctx.session_id)
                emit_trace_event("evidence_payload_resolved", {"evidence_id": record["evidence_id"], "stage": "stage5"}, state)
            except Exception:
                unavailable.append(record["evidence_id"])
        observations: list[dict[str, Any]] = []
        questions = list(state.get("unresolved_questions", []))
        valid_segment_ids = {segment.get("segment_id") for segment in state.get("requirement_segments", [])}
        candidates = [
            *state.get("deterministic_observations", []),
            *state.get("llm_observations", []),
        ]
        cited_ids = [
            source_id
            for candidate in candidates
            if isinstance(candidate, dict)
            for source_id in (
                candidate.get("evidence_ids", [])
                if isinstance(candidate.get("evidence_ids", []), list)
                else []
            )
        ]
        validate_provenance(
            {
                "gate_kind": "review",
                "status": AgentStatus.SUCCESS.value,
                "evidence_ids": cited_ids,
                "evidence_index": list(evidence_by_id.values()),
                "observations": candidates,
            },
            self._domain_contract,
        )
        for candidate in candidates:
            candidate_data = candidate if isinstance(candidate, dict) else {}
            if candidate_data.get("segment_id") not in valid_segment_ids:
                verified, reason = None, "unknown_segment_id"
            else:
                verified, reason = _verify_observation(candidate, evidence_by_id, raw_by_id, state.get("deterministic_observations", []), self._config)
            if verified is None:
                emit_trace_event("observation_demoted", {"demotion_reason": reason}, state)
                safe_segment = candidate_data.get("segment_id") if candidate_data.get("segment_id") in valid_segment_ids else None
                requested_ids = candidate_data.get("evidence_ids", [])
                safe_ids = [evidence_id for evidence_id in requested_ids if evidence_id in evidence_by_id] if isinstance(requested_ids, list) else []
                questions.append(_question(len(questions) + 1, safe_segment, "nonquotable_source_requires_human_review", {"evidence_ids": safe_ids}))
            else:
                if verified.get("origin") == "llm":
                    observations.append(
                        _normalize_llm_observation(
                            verified,
                            number=len(observations) + 1,
                            deterministic=state.get("deterministic_observations", []),
                        )
                    )
                else:
                    observations.append(verified)
        for evidence_id in unavailable:
            questions.append(_question(len(questions) + 1, None, "evidence_content_unavailable", {"evidence_ids": [evidence_id]}))
        specs_by_segment = {spec.get("segment_id"): spec for spec in state.get("requirement_specs", []) if isinstance(spec, dict)}
        for segment in state.get("requirement_segments", []):
            spec = specs_by_segment.get(segment["segment_id"], {})
            for axis, code in (
                ("period", "requirement_period_not_extractable"),
                ("population", "requirement_population_not_extractable"),
                ("approval_authority", "requirement_approval_authority_not_extractable"),
            ):
                if not _axis_extracted(spec, axis):
                    questions.append(_question(len(questions) + 1, segment["segment_id"], code, {}))
        reason = state.get("degradation_reason")
        if unavailable:
            reason = append_reason(reason, "evidence_content_unavailable")
        all_without_content = bool(evidence_by_id) and not raw_by_id and not unavailable
        evaluation = "not_performed" if not state.get("requirement_segments") or not evidence_by_id else "deterministic_only" if not self._config.get("llm") or all_without_content else "completed"
        if evaluation != "completed":
            emit_trace_event("evaluation_degraded", {"degradation_reason": reason or evaluation}, state)
        version = [item for item in observations if item.get("observation_kind") == "version_conflict"]
        emit_trace_event("evidence_provenance_verified", {"observations": len(observations)}, state)
        return {
            "evaluation_state": evaluation,
            "observations": observations,
            "version_conflict_observations": version,
            "suggested_additional_evidence": _safe_suggestions(state.get("suggested_additional_evidence", []), state.get("requirement_segments", []), evidence_by_id, raw_by_id, self._config),
            "unresolved_questions": questions,
            "degradation_reason": reason,
            "status": AgentStatus.SUCCESS.value,
        }


def _required_types(text: str) -> list[str]:
    lowered = text.casefold()
    values = []
    for name, markers in (("period", ("期間", "月", "年")), ("population", ("母集団", "対象")), ("control_action", ("統制", "実施", "手続")), ("approval_authority", ("承認", "権限"))):
        if any(marker in lowered for marker in markers):
            values.append(name)
    return values or ["period"]


def _segment_summary(index: int, text: str) -> str:
    """Render a Japanese, body-safe label from deterministic request features."""

    labels = [
        term for term in ("与信審査統制", "統制", "承認記録", "実施証跡", "承認", "記録", "証跡")
        if term in text
    ]
    dates = _required_months_from_text(text)
    components = []
    if dates:
        components.append(f"対象期間 {dates[0]}～{dates[-1]}")
    if labels:
        components.append("・".join(dict.fromkeys(labels)))
    if not components:
        components.append("検査要求の確認事項")
    return f"要求セグメント {index}: {'／'.join(components)}"


def _register_findings(evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    findings = []
    current: dict[str, list[str]] = defaultdict(list)
    for item in evidence:
        if item.get("version_status") == "current":
            current[str(item.get("doc_type"))].append(str(item.get("evidence_id")))
        if item.get("effective_from") and item.get("effective_to") and item["effective_from"] > item["effective_to"]:
            findings.append({"reason_code": "effective_date_invalid", "evidence_id": item["evidence_id"]})
    for doc_type, ids in current.items():
        if len(ids) > 1:
            findings.append({"reason_code": "multiple_current_versions", "doc_type": doc_type})
    return findings


def _fallback_spec(segment: dict[str, Any]) -> dict[str, Any]:
    # The fallback is deliberately non-inferential.  It must not transform a
    # date-looking token in a summary into a requirement fact.
    return {
        "segment_id": segment["segment_id"],
        "extracted": {
            "period": False,
            "population": False,
            "control_action": False,
            "approval_authority": False,
        },
        "period": None,
        "population": None,
        "control_action": None,
        "approval_authority": None,
    }


def _coerce_spec(value: object, fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("segment_id") != fallback["segment_id"]:
        return fallback
    period = value.get("period")
    if period is not None and not _required_months(period):
        return fallback
    population = value.get("population") if isinstance(value.get("population"), str) and value.get("population") else None
    control_action = value.get("control_action") if isinstance(value.get("control_action"), str) and value.get("control_action") else None
    authority = value.get("approval_authority") if isinstance(value.get("approval_authority"), str) and value.get("approval_authority") else None
    return {
        "segment_id": fallback["segment_id"],
        "extracted": {
            "period": bool(_required_months(period)),
            "population": population is not None,
            "control_action": control_action is not None,
            "approval_authority": authority is not None,
        },
        "period": period if _required_months(period) else None,
        "population": population,
        "control_action": control_action,
        "approval_authority": authority,
    }


def _axis_extracted(spec: object, axis: str) -> bool:
    """Only the explicit per-axis extraction map authorizes an axis match."""

    return isinstance(spec, dict) and isinstance(spec.get("extracted"), dict) and spec["extracted"].get(axis) is True


def _vocabulary(evidence: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Stage 3 は coverage の値と文字列完全一致で照合する。抽出側へ候補語彙を渡さないと、
    LLM が自然文で答えた瞬間に誤った gap を検出してしまう。"""

    def _values(key: str) -> list[str]:
        seen = {
            record.get("coverage", {}).get(key)
            for record in evidence
            if isinstance(record.get("coverage"), dict)
        }
        return sorted(value for value in seen if isinstance(value, str) and value)

    return {"population": _values("population_scope"), "approval_authority": _values("approval_authority")}


def _requirement_prompt(segments: list[dict[str, Any]], request_text: str = "", evidence: list[dict[str, Any]] | None = None) -> str:
    # 実 LLM 検証（§14-1-bis）で判明: 出力スキーマの実体・segment_id の返却指示・
    # 照合先の候補語彙が無いと、_coerce_spec() が全件 fallback に落ちて所見がゼロになる。
    # period の受理形式は _required_months() と、population/approval_authority の値は
    # Stage 3 の完全一致照合と、それぞれ対でなければならない。
    vocab = _vocabulary(evidence or [])
    return (
        "あなたは銀行の検査要求を読み、要求仕様を構造化する担当者です。\n"
        "次の JSON だけを返してください（説明文・コードフェンスは不要）:\n"
        '{"requirement_specs": [{"segment_id": "...", "extracted": true, '
        '"period": ["YYYY-MM", ...], "population": "...", "control_action": "...", '
        '"approval_authority": "..."}]}\n'
        "規則:\n"
        "- 入力セグメントと**同じ数・同じ順序**で返し、`segment_id` は入力の値をそのまま複製する\n"
        "- 検査要求から確かに読み取れる項目だけを埋め、1 つでも不確かなら `extracted` を false にする"
        "（推測で埋めない）\n"
        '- `period` は対象期間に含まれる月を "YYYY-MM" の文字列配列で列挙する'
        '（例: 2026年4月〜6月 → ["2026-04","2026-05","2026-06"]）。範囲オブジェクトや自由文は不可\n'
        "- `population` と `approval_authority` は**下記の候補値のいずれかと完全一致する文字列**で返す。"
        "候補に該当が無ければ null とし `extracted` は false にする\n"
        f"- `population` の候補: {json.dumps(vocab['population'], ensure_ascii=False)}\n"
        f"- `approval_authority` の候補: {json.dumps(vocab['approval_authority'], ensure_ascii=False)}\n"
        "- `control_action` は要求されている統制行為を短い語で表す（自由記述可）\n\n"
        "検査要求の本文:\n"
        f"{request_text}\n\n"
        "要求セグメント:\n"
        + json.dumps(segments, ensure_ascii=False)
    )


def _narrative_prompt(state: dict, usable: list[dict[str, Any]]) -> str:
    # 実 LLM 検証（§14-1-bis）で判明: span の形式を示さないと Stage 5 の逐語照合を通らず、
    # 所見が丸ごと nonquotable_source_requires_human_review へ降格して出力がゼロになる。
    return (
        "あなたは銀行の検査対応で、収集済み証拠が検査要求を実証できているかを点検する担当者です。\n"
        "次の JSON だけを返してください（説明文・コードフェンスは不要）:\n"
        '{"observations": [{"segment_id": "...", "observation_kind": "...", '
        '"evidence_ids": ["..."], "observation": "...", "uncertainty": "low|medium|high", '
        '"observation_params": {...}, "source_spans": [{"evidence_id": "...", '
        '"span_kind": "...", "start": 0, "end": 12, "text": "..."}]}], '
        '"suggested_additional_evidence": [{"segment_id": "...", "evidence_type": "...", '
        '"rationale": "...", "source_spans": []}]}\n'
        "規則:\n"
        "- `observation` と `rationale` は**日本語**で書く\n"
        "- `segment_id` は入力の要求セグメントにある値を**そのまま複製**する。新しい ID は作らない\n"
        "- `observation_kind` は次のいずれか: period_gap / population_gap / control_action_gap / "
        "approval_authority_gap / document_absent / version_conflict / coverage_unverifiable\n"
        "- `observation_params` は kind ごとに固定する: period_gap={missing_periods:[YYYY-MM]}, "
        "population_gap={required_population,covered_population}, control_action_gap={required_control_action}, "
        "approval_authority_gap={required_authority,evidence_authority}, "
        "document_absent={required_evidence_type}, version_conflict={conflicting_versions,conflict_attribute}, "
        "coverage_unverifiable={reason_code}\n"
        "- `source_spans` は**必須**（1 件以上）。付けられない所見は返さない\n"
        "- `text` は証拠本文の**逐語の部分文字列**とし、`start`/`end` はその本文における"
        "0 始まりの文字オフセット（end は排他）にする。8 文字以上 120 文字以内\n"
        "- `span_kind` は quantity / date / identifier / observed_condition / procedural_record の"
        "いずれかを必ず使う。fact 等の別値は使わない\n"
        "- 引用してよいのは事実の記述のみ（数量・日付・識別子・観察された状態・手続の記録）。"
        "評価や判断を述べた文（「有効である」「適合している」等）は引用しない\n"
        "- 規制の解釈・適合/不適合の判定・提出可否・検査回答の文面は**書かない**\n"
        "- 事実値は下記の決定論所見と矛盾させない\n\n"
        "決定論所見:\n"
        + json.dumps(state.get("deterministic_observations", []), ensure_ascii=False)
        + "\n\n証拠（本文つき）:\n"
        + json.dumps(usable, ensure_ascii=False)
    )


def _record_matches(segment: dict[str, Any], record: dict[str, Any]) -> bool:
    required = segment.get("required_evidence_types", [])
    if "approval_authority" in required:
        return record.get("doc_type") in {"approval_record", "control_record"}
    if "control_action" in required:
        return record.get("doc_type") in {"procedure", "control_record", "system_log"}
    return True


def _covers_period(coverage: object, period: str) -> bool:
    if not isinstance(coverage, dict):
        return False
    start, end = coverage.get("period_from"), coverage.get("period_to")
    return isinstance(start, str) and isinstance(end, str) and start[:7] <= period <= end[:7]


def _required_months(value: object) -> list[str]:
    """Accept only explicit YYYY-MM month facts supplied by Stage 2."""

    raw = value if isinstance(value, list) else [value]
    months = [item for item in raw if isinstance(item, str) and re.fullmatch(r"\d{4}-\d{2}", item)]
    return sorted(set(months))


def _required_months_from_text(text: str) -> list[str]:
    normalized = re.sub(r"[０-９]", lambda match: str(ord(match.group()) - ord("０")), text)
    dates = re.findall(r"(\d{4})年\s*(\d{1,2})月", normalized)
    return [f"{year}-{int(month):02d}" for year, month in dates]


def _observation(number: int, segment_id: str, kind: str, evidence_ids: list[str], text: str, params: dict[str, Any], priority: str) -> dict[str, Any]:
    return {"observation_id": f"OBS-{number}", "segment_id": segment_id, "observation_kind": kind, "origin": "deterministic", "evidence_ids": evidence_ids, "observation": text, "uncertainty": "medium", "review_priority": priority, "observation_params": params, "source_spans": []}


def _version_conflicts(evidence: list[dict[str, Any]], *, start: int) -> list[dict[str, Any]]:
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        by_doc[str(item.get("doc_type"))].append(item)
    out = []
    for doc_type, records in by_doc.items():
        current = [record for record in records if record.get("version_status") == "current"]
        if len(current) > 1:
            ids = [record["evidence_id"] for record in current]
            out.append(_observation(start + len(out), "SEG-1", "version_conflict", ids, "同一文書種別に current 版が複数あります。", {"conflicting_versions": [record["version"] for record in current], "conflict_attribute": "approval_authority"}, "high"))
    return out


def _owners_for_observations(observations: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_id = {item.get("evidence_id"): item.get("owner_id") for item in evidence}
    values = {(item["segment_id"], by_id[evidence_id]) for item in observations for evidence_id in item["evidence_ids"] if by_id.get(evidence_id)}
    return [{"segment_id": segment_id, "owner_id": owner_id} for segment_id, owner_id in sorted(values)]


def _checklist_questions(checklist: list[dict[str, Any]], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not checklist or segments:
        return []
    return [_question(index, None, "checklist_item_unmapped", {"checklist_id": item.get("checklist_id", "")}) for index, item in enumerate(checklist, 1)]


def _question(number: int, segment_id: str | None, code: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"question_id": f"Q-{number}", "segment_id": segment_id, "question_code": code, "params": params}


def _verify_observation(candidate: object, evidence_by_id: dict[str, dict[str, Any]], raw_by_id: dict[str, str], deterministic: list[dict[str, Any]], config: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(candidate, dict) or candidate.get("observation_kind") not in OBSERVATION_KINDS:
        return None, "invalid_observation"
    ids = candidate.get("evidence_ids", [])
    if not isinstance(ids, list) or not set(ids) <= set(evidence_by_id):
        return None, "unknown_evidence_id"
    if candidate.get("origin") == "llm" and not candidate.get("source_spans"):
        return None, "llm_observation_without_span"
    params = candidate.get("observation_params")
    if not isinstance(params, dict):
        return None, "missing_observation_params"
    if candidate.get("origin") == "llm" and not any(
        item.get("segment_id") == candidate.get("segment_id")
        and item.get("observation_kind") == candidate.get("observation_kind")
        and item.get("observation_params") == params
        for item in deterministic
    ):
        return None, "observation_params_not_in_deterministic_result"
    text_tokens = normalized_fact_tokens(str(candidate.get("observation", "")))
    param_tokens = normalized_fact_tokens(json.dumps(params, ensure_ascii=False))
    if not text_tokens <= param_tokens:
        return None, "observation_text_param_mismatch"
    for span in candidate.get("source_spans", []):
        if not isinstance(span, dict) or span.get("evidence_id") not in raw_by_id:
            return None, "source_unavailable"
        source = raw_by_id[span["evidence_id"]]
        start, end, text = span.get("start"), span.get("end"), span.get("text")
        if not isinstance(start, int) or not isinstance(end, int) or not isinstance(text, str) or source[start:end] != text or not config.get("min_quotable_span_chars", 8) <= len(text) <= config.get("max_quotable_span_chars", 120) or classify_span(text, max_chars=config.get("max_quotable_span_chars", 120)) not in {"quantity", "date", "identifier", "observed_condition", "procedural_record"}:
            return None, "invalid_source_span"
    return candidate, ""


def _normalize_llm_observation(candidate: dict[str, Any], *, number: int, deterministic: list[dict[str, Any]]) -> dict[str, Any]:
    """Project a verified LLM DTO into the closed review-packet observation schema."""

    priority = next(
        (
            item.get("review_priority")
            for item in deterministic
            if isinstance(item, dict)
            and item.get("segment_id") == candidate.get("segment_id")
            and item.get("observation_kind") == candidate.get("observation_kind")
            and item.get("observation_params") == candidate.get("observation_params")
        ),
        "medium",
    )
    spans = [
        {key: span.get(key) for key in ("evidence_id", "span_kind", "start", "end", "text")}
        for span in candidate.get("source_spans", [])
        if isinstance(span, dict)
    ]
    return {
        "observation_id": f"OBS-{number}",
        "segment_id": candidate["segment_id"],
        "observation_kind": candidate["observation_kind"],
        "origin": "llm",
        "evidence_ids": list(candidate["evidence_ids"]),
        "observation": candidate["observation"],
        "uncertainty": candidate["uncertainty"],
        "review_priority": priority,
        "observation_params": dict(candidate["observation_params"]),
        "source_spans": spans,
    }


def _safe_suggestions(items: list[dict[str, Any]], segments: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]], raw_by_id: dict[str, str], config: dict[str, Any]) -> list[dict[str, Any]]:
    valid_segments = {item["segment_id"] for item in segments}
    output = []
    for item in items:
        if not isinstance(item, dict) or item.get("segment_id") not in valid_segments or not isinstance(item.get("rationale"), str) or len(item["rationale"]) > 320:
            continue
        evidence_type = item.get("evidence_type") if item.get("evidence_type") in DOC_TYPES else "other"
        candidate = {"observation_kind": "coverage_unverifiable", "origin": "deterministic", "evidence_ids": [], "observation_params": {"reason_code": "suggestion"}, "observation": "", "source_spans": item.get("source_spans", [])}
        _, reason = _verify_observation(candidate, evidence_by_id, raw_by_id, [], config)
        # `invalid_source_span` を許容すると、LLM が span.text に原文を入れて offset を
        # 外すだけで未検証の原文が顧客出力へ到達する（span 検証をすり抜ける）。
        # 提案は「引用が検証を通った」ものだけ採用する。
        if reason == "" and isinstance(item.get("source_spans"), list):
            spans = [
                {key: span.get(key) for key in ("evidence_id", "span_kind", "start", "end", "text")}
                for span in item["source_spans"]
                if isinstance(span, dict)
            ]
            output.append({"segment_id": item["segment_id"], "evidence_type": evidence_type, "rationale": item["rationale"], "source_spans": spans})
    return output
