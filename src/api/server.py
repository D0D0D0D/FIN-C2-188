"""HTTP entry adapter for FIN-C2-188.

The endpoint accepts bodies only here, stores them under the verified case
scope, and passes opaque references into the graph.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from framework.schemas.invocation_context import InvocationContext
from src.entry_adapter import Operation, authenticate
from src.graph.graph import ExamEvidenceSufficiencyReviewGraph
from src.payload_store import PayloadStore
from src.services.validation import DOC_TYPES


class EvidenceInput(BaseModel):
    """External evidence DTO.  A payload ref is intentionally not accepted."""

    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    title: str
    doc_type: str
    version: str
    version_status: str
    effective_from: str | None = None
    effective_to: str | None = None
    coverage: dict[str, Any] | None = None
    owner_id: str | None = None
    content_text: str | None = None


class InvokeRequest(BaseModel):
    """The public request shape described in docs/02 §3-1."""

    model_config = ConfigDict(extra="forbid")
    input: str
    session_id: str
    case_id: str
    request_text: str
    evidence_register: list[EvidenceInput]
    response_checklist: list[dict[str, Any]] = Field(default_factory=list)
    owner_directory: list[dict[str, Any]] = Field(default_factory=list)
    rubric_version: str | None = None


def _runtime_config() -> dict[str, Any]:
    data = yaml.safe_load(
        (Path(__file__).parents[2] / "config" / "config.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(data, dict):
        raise RuntimeError("config/config.yaml must contain a mapping")
    return dict(data)


def _quality_reasons(text: object, *, max_chars: int) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return ["empty_text"]
    signatures = ("%PDF-", "PK\x03\x04", "\xd0\xcf\x11\xe0", "\x89PNG", "\xff\xd8\xff")
    if text.startswith(signatures):
        return ["binary_magic_bytes"]
    if len(text) > max_chars:
        return ["max_text_chars_exceeded"]
    if text.count("\ufffd") / len(text) > 0.01:
        return ["replacement_character_ratio_exceeded"]
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\t\n\r")
    if controls / len(text) > 0.005:
        return ["control_character_ratio_exceeded"]
    readable = sum(1 for char in text if char.isascii() and (char.isalnum() or char.isspace() or char in ".,:;!?()[]{}-_/'\"@#%&+=") or "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff")
    if readable / len(text) < 0.9:
        return ["readable_character_ratio_too_low"]
    return []


def _bad_request(rejected: list[dict[str, str]]) -> HTTPException:
    return HTTPException(status_code=400, detail={"rejected": rejected})


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = _runtime_config()
    store = PayloadStore(scope_keys=("case_id",), ttl_seconds=int(config["payload_ttl_seconds"]))
    config["payload_store"] = store
    graph = ExamEvidenceSufficiencyReviewGraph(config=config)
    graph.compile()
    app.state.config = config
    app.state.payload_store = store
    app.state.agent = graph
    app.state.verify_scope = lambda request: request.state.requested_case_scope
    yield


app = FastAPI(title="Exam Evidence Sufficiency Review", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """The public DTO contract uses 400, never FastAPI's default 422."""

    rejected = [{"field": ".".join(str(part) for part in error["loc"] if part != "body"), "reason_code": "invalid_request"} for error in exc.errors()]
    return JSONResponse(status_code=400, content={"detail": {"rejected": rejected}})


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    """Run the read-only review operation under a request-isolation scope.

    ``case_id`` partitions only the short-lived payload store; this endpoint
    neither creates nor updates durable business state.  The request-local seam
    avoids presenting a client-selected case id as middleware-stamped identity.
    """

    if not req.case_id.strip():
        raise _bad_request([{"field": "case_id", "reason_code": "case_id_required"}])
    request.state.requested_case_scope = {"case_id": req.case_id}
    auth = authenticate(request, operation=Operation.READ_INVOKE)
    scope = auth.scope
    from shared.utils.audit_logger import emit_trace_event

    emit_trace_event(
        "entry_authenticated",
        {"auth_source": auth.auth_source.value},
        {"case_id": req.case_id},
    )
    config: dict[str, Any] = request.app.state.config
    rejected: list[dict[str, str]] = []
    for field, text in [("request_text", req.request_text), *[(f"evidence_register[{index}].content_text", item.content_text) for index, item in enumerate(req.evidence_register) if item.content_text is not None]]:
        for reason in _quality_reasons(text, max_chars=int(config["max_text_chars"])):
            rejected.append({"field": field, "reason_code": reason})
    if len(req.evidence_register) > int(config["max_evidence_entries"]):
        rejected.append({"field": "evidence_register", "reason_code": "max_evidence_entries_exceeded"})
    if rejected:
        emit_trace_event("input_quality_rejected", {"reason_code": rejected[0]["reason_code"]}, {"case_id": req.case_id})
        raise _bad_request(rejected)
    if len({item.evidence_id for item in req.evidence_register}) != len(req.evidence_register):
        raise _bad_request([{"field": "evidence_register", "reason_code": "duplicate_evidence_id"}])
    if any(item.doc_type not in DOC_TYPES for item in req.evidence_register):
        raise _bad_request([{"field": "evidence_register", "reason_code": "invalid_doc_type"}])
    session_id = req.session_id.strip()
    if not session_id:
        raise _bad_request([{"field": "session_id", "reason_code": "session_id_required"}])
    store: PayloadStore = request.app.state.payload_store
    request_ref = store.put(req.request_text, scope=scope, session_id=session_id)
    refs = [request_ref]
    evidence_index: list[dict[str, Any]] = []
    for item in req.evidence_register:
        record = item.model_dump(exclude={"content_text"}, exclude_none=True)
        if item.content_text is not None:
            ref = store.put(item.content_text, scope=scope, session_id=session_id)
            record["content_payload_ref"] = ref
            refs.append(ref)
        evidence_index.append(record)
    context = {
        "case_id": req.case_id,
        "request_payload_ref": request_ref,
        "evidence_index": evidence_index,
        "response_checklist": req.response_checklist,
        "owner_directory": req.owner_directory,
        "rubric_version": req.rubric_version,
    }
    ctx = InvocationContext(
        session_id=session_id,
        caller_trust_level=auth.trust,
        caller_id=auth.principal["caller_id"],
        hitl_allowed=False,
    )
    try:
        return request.app.state.agent.invoke(req.input, session_id=session_id, ctx=ctx, input_context=context)
    finally:
        for ref in refs:
            store.delete(ref, scope=scope, session_id=session_id)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "FIN-C2-188"}
