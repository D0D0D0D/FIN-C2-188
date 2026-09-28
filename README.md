# FIN-C2-188 — ExamEvidenceSufficiencyReviewAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: FIN

## Overview

ExamEvidenceSufficiencyReviewAgent reviews a regulatory-examination evidence package against the
request it is meant to satisfy, and returns what does not line up — with the evidence IDs and
verbatim quotations behind each observation.

It is for a bank's regulatory-response or compliance team at one specific moment: the information
request letter has arrived, the evidence package has been assembled internally, and somebody now
has to work out whether it actually answers what was asked. The agent segments the free-text
request into requirements, matches the assembled evidence against them, and reports period gaps,
population mismatches, approval-authority mismatches, version conflicts, and the questions that
remain open. A deterministic `review_priority` says what to look at first.

**What it deliberately does not do is the point of the design.** It does not interpret
regulation, draft a substantive response, decide whether to submit, or modify a record. Those are
excluded *structurally* — the output schema has no field they could be written into, and an
output carrying one is rejected — and they stay with the accountable human.

The division of labour between the model and the deterministic layer is explicit, in five stages:

| Stage | Kind | Does |
|---|---|---|
| 1 | deterministic | Segment the request; check the evidence register's internal consistency |
| 2 | LLM | Extract a typed requirement specification |
| 3 | deterministic | Compute coverage against the evidence, and the review priority |
| 4 | LLM | Write the semantic observations and suggested additional evidence, with citations |
| 5 | deterministic | Verify every claim against its cited source; enforce the quotation allowlist |

Stage 5 is what makes the citations mean something. A cited `evidence_id` existing is not the
same as the observation being true of it, so the check is bidirectional: the values an
observation asserts must appear in the deterministic Stage 3 result, and each quoted span must
appear verbatim in the source it names — after NFKC normalisation, and with kanji and full-width
numerals normalised, so a reworded numeral cannot slip through. Quoted spans are bounded at both
ends (8–120 characters by default); a one-character span would make `text in source` vacuously
true.

Sufficiency is judged against a **versioned rubric the customer's own compliance function
approves**, not against the model's opinion. The shipped rubric is a sample: it runs, but the
output carries `rubric_approved: false` and a `degradation_reason` until the approval fields are
replaced. That flag is a single machine-checkable value, so an audit can tell an approved run
from a sample one without reading prose.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- authentication middleware in front of the entry adapter;
- optionally a `BaseLLM` client, injected through the graph configuration.

Without an injected LLM client the workflow still runs: Stages 2 and 4 are skipped and the
response comes back with `evaluation_state: "deterministic_only"` — the deterministic coverage
result, no semantic observations. The same state is reported when no evidence entry carries
`content_text`. `not_performed` means there was nothing to evaluate. A degraded run is visible in
the response rather than looking like a complete one.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`) / `timeout_seconds` (`60`) | Backbone retry and timeout budget |
| `memory_enabled` (`false`) | No checkpointer — this agent holds no durable state |
| `max_evidence_entries` (`200`) | Ceiling on register size |
| `max_requirement_segments` (`50`) | Ceiling on request segmentation |
| `max_text_chars` (`200000`) | Per-field text ceiling |
| `payload_ttl_seconds` (`300`) | TTL of the short-lived scoped store holding request and evidence text |
| `min_quotable_span_chars` (`8`) / `max_quotable_span_chars` (`120`) | Quotation length bounds enforced in Stage 5 |
| `sufficiency_rubric` | The versioned rubric: `rubric_id`, `version`, `approved_by`, `approved_on`, `reviewer_role`, `escalation_path`, and the `criteria` list. **Replace the shipped sample before production use** |

The default criteria are period coverage, population scope, control action and approval
authority. They are meant to be replaced with the customer's own.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR. Without the `agenticstar-agentcore`
distribution installed, `src/api/server.py` cannot be imported at all — the graph, the state
schema and the security gates all come from it, and there is no vendored substitute in this
repository.

With the framework installed but no platform around it, the standalone server starts: the graph
is built and compiled during application start-up and `/health` answers
`{"status": "ok", "agent": "FIN-C2-188"}`. There are no start-up secrets to resolve — the
manifest declares none. `/invoke` still requires an authenticated caller: with neither upstream
middleware nor a matching `INVOKE_AUTH_TOKEN` bearer token, the request is rejected. With no LLM
injected, an authenticated request succeeds and returns the `deterministic_only` form. The test
suite runs without a platform connection.

## Input

`POST /invoke` takes a strict JSON body — **unknown fields are rejected**, not ignored, which is
what keeps a downstream response draft from being smuggled back in as input:

| Field | Meaning |
|---|---|
| `input` | Free-text instruction |
| `session_id` | Required; scopes the short-lived payload store |
| `case_id` | Required; partitions the payload store for this examination case |
| `request_text` | **Required** — the full text of the information request |
| `evidence_register` | The assembled evidence. Each entry carries `evidence_id`, `title`, `doc_type`, `version`, `version_status`, and optionally `effective_from` / `effective_to` / `coverage` / `owner_id` / `content_text` |
| `response_checklist` | Optional; carried through, never used to judge sufficiency |
| `owner_directory` | Optional; used to attribute accountable owners. An unregistered owner is rejected |
| `rubric_version` | Optional; selects a rubric version |

`content_text` is optional. Structured metadata alone still yields the deterministic coverage
result.

`doc_type` must be one of `policy`, `procedure`, `control_record`, `approval_record`,
`system_log`, `audit_report`, `other`.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "証拠パッケージの十分性をレビューしてください",
    "session_id": "demo-001",
    "case_id": "exam-2026-001",
    "request_text": "2025年4月から2026年3月までの与信審査に係る統制の実施記録を提出すること。",
    "evidence_register": [
      {
        "evidence_id": "EV-001",
        "title": "与信審査統制 実施記録",
        "doc_type": "control_record",
        "version": "1.2",
        "version_status": "effective",
        "effective_from": "2025-04-01",
        "effective_to": "2026-03-31"
      }
    ]
  }'
```

Input failures are **400 with a `rejected` list**, each entry naming the field and a stable
`reason_code` — never FastAPI's default 422, and never an unhandled error. Codes include
`case_id_required`, `session_id_required`, `duplicate_evidence_id`, `invalid_doc_type`,
`max_evidence_entries_exceeded`, `empty_text`, `binary_magic_bytes`,
`max_text_chars_exceeded`, `replacement_character_ratio_exceeded`,
`control_character_ratio_exceeded` and `readable_character_ratio_too_low`. The agent parses no
document formats: a file posted as text is detected by its magic bytes and rejected rather than
half-read.

## Output

`output` is a closed packet stamped `contract_version: "review_packet_v1"`:

| Field | Meaning |
|---|---|
| `case_id`, `rubric_version`, `rubric_approved` | Which case, which rubric, and whether that rubric is customer-approved |
| `evaluation_state` | `completed` / `deterministic_only` / `not_performed` |
| `requirement_segments` | The request, segmented |
| `observations` | Findings, each with an enumerated `observation_kind`, cited evidence IDs, and verbatim source spans |
| `version_conflict_observations` | Version inconsistencies within the register |
| `suggested_additional_evidence` | What might close a gap |
| `accountable_owners` | Owners joined from `owner_directory` |
| `evidence_sources` | The evidence actually referenced |
| `unresolved_questions` | Coded questions, e.g. `requirement_period_not_extractable`, `evidence_content_unavailable`, `owner_unassigned` |
| `requires_human_review` | Always `true` |
| `degradation_reason` | Set when the run was degraded |

`observation_kind` is an enumeration (`period_gap`, `population_gap`, `control_action_gap`,
`approval_authority_gap`, `document_absent`, `version_conflict`, `coverage_unverifiable`) and
questions are codes rather than free prose, so a conclusion cannot be smuggled out through an
open text field. `requires_human_review` is not computed — it is always true, because this agent
has no state in which a finding could be marked resolved.

## Security and Limitations

Examination requests and evidence text are sensitive financial material and are **never written
into state or a checkpoint in plaintext**. Both go into a short-lived, `case_id`- and
`session_id`-scoped payload store, are referenced indirectly, and are deleted when the invocation
finishes — including when it fails. A client cannot supply a reference itself; the external
request DTO has no field for one, so one caller cannot name another's payload.

The output gate **rejects rather than masks**. Every declared free-text path is scanned for
personal data and credentials, every string in the packet is scanned for credentials, and a hit
raises a security violation instead of producing a redacted packet. Beyond that, the packet is
checked as a *closed* structure: an undeclared field, an out-of-vocabulary observation kind, or
an out-of-bounds quotation is a rejection.

The template holds no durable state, has no persistence layer, and declares no start-up secrets.
The evidence register of record stays with the customer and is only read.

Known limits, stated rather than implied:

- Coverage is judged against the register supplied. Evidence that exists but was not registered
  cannot be assessed.
- Stage 5 proves that a quoted span occurs verbatim in the source it names — not that the source
  is the *right* one for the requirement. That judgement stays human.
- Non-quotable sources (structured metadata with no text) route to
  `nonquotable_source_requires_human_review` rather than being asserted about.
- Identifiers are best written without dash-separated UUIDs in free-text fields: measurement on
  20,000 samples put the personal-data detector's false-positive rate on dash-separated UUIDs at
  0.535%, and at 0% for the hex form.

Output is machine-generated and assistive. It is not a regulatory interpretation, not a response
draft, and not a submission decision. A reviewer who disagrees with an observation escalates it
per the rubric's own `escalation_path` and does not adopt the output.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), outer backbone wrapping an inner five-stage workflow graph
- Generation mode: `llm`, degrading to a declared deterministic-only result when no client is injected
- HITL: not enabled (`hitl_allowed` is set false at the entry adapter)
- Persistence: none — `memory_enabled: false`, no checkpointer; sensitive text lives only in a TTL-bounded scoped store
- External API clients: deployment-injected; none bundled

Handoff to the adjacent response-drafting template (`Bank Regulatory Examination Request Response Drafting Agent`) is **performed by a person**.
This agent never calls it, never accepts its output as input, and deliberately offers no way to
mark a finding "confirmed" — an automatic bridge is the thing being prevented.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using a fake LLM. Running the agent itself starts the
standalone server:

```bash
uvicorn src.api.server:app --reload
```

Set `INVOKE_AUTH_TOKEN` before calling `/invoke`, or use real authentication middleware.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the five-stage division of labour,
the payload-isolation design and the security design, together with the design decision record.
`docs/03_test_spec.md` records the test specification and the verification results, including a
live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
