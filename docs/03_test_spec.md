# Test Specification

## Scope

Tests run with injected fakes only. No LLM, database, or external API connection is made.

| ID | Verification | Result |
|---|---|---|
| TC-01 | `State` is a flat `TypedDict` with JSON-compatible fields | PASS |
| TC-02 | Invalid closed output raises `SecurityViolationError` | PASS |
| TC-03 | State has no credential-like fields or body text | PASS |
| TC-04 | Nodes reconstruct context with `InvocationContext.from_state()` | PASS |
| TC-05 | Domain event bindings are observed separately from framework lifecycle events | PASS |
| TC-06 | S-2 final gate cannot be overridden | PASS |
| TC-07 | S-3 final gate cannot be overridden | PASS |
| TC-08 | `VERIFIED_EXTERNAL` trust is required。加えて PB-6 の常設フィクスチャで **`execute()` が呼ばれなかったこと**を証明する（`test_pb_invoke_order.py` の `test_s1_denial_refuses_execution_before_execute` — `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / `execute` 未呼び出し を assert。正本 docs/03 の TC-08 要求） | PASS |
| TC-09 | Internal context/rubric structural validation fails closed | PASS |
| TC-10 | Closed recursive output schema rejects unknown keys and bad contract versions | PASS |
| TC-11 | Every domain node has a domain audit event | PASS |

## Boundary Proof

| ID | Verification | Result |
|---|---|---|
| PB-1 | Patch each node module's `emit_trace_event` binding and observe a domain event, including reject/degradation paths | PASS |
| PB-2 | Invoke-state/result serialization contains primitives only | PASS |
| PB-3 | LLM and payload store use injection seams; no external service is called | PASS |
| PB-4 | AST import-isolation scan rejects Level 0 imports | PASS |
| PB-5 | Deserialize post-invoke state/result and assert no body fields; body-in-State mutation fails | PASS |
| PB-6 | Provisioned input verifies S-1 → S-2 → execute → S-3 ordering | PASS |
| PB-7 | HITL interrupt propagation | Auto-skip — non-HITL |

## Domain Acceptance

- Missing `request_text`, unknown DTO keys including `response_draft`, empty case id, and malformed evidence are 400.
- Optional `content_text` produces `deterministic_only` without falsely adding `evidence_content_unavailable`.
- A present-but-unresolvable content ref adds `evidence_content_unavailable` and a coded unresolved question.
- `%PDF-`, U+FFFD-ratio, and control-character-ratio mutation inputs return their rejection `reason_code`.
- Handoff tests reject response narrative keys and contract-version mutation; static source scan finds no remote handoff implementation.
- NFKC/full-width/kanji numerical tokens are checked against `observation_params`; empty and one-character spans are rejected.
- 実 LLM 検証で捕捉した不正 `segment_id` / `span_kind` と、軸ごとに一部だけ抽出された RequirementSpec を固定 FakeLLM で回帰する。
- LLM observation の引用なし・決定論結果に無い params は、origin 付与後に `nonquotable_source_requires_human_review` へ降格することを検証する。

## 共通部品リフレッシュ接合部（2026-08-14）

`parts/TESTING.md` の対象行を FIN-C2-188 の実経路へ具体化する。

| ID | 正本の項目 | FIN-C2-188 での具体的な検証 |
|---|---|---|
| G-1 | `mode_specs` 上書き後の `merge_output` | `tests/unit/test_main_node.py` と同梱テストで、単一 `invoke` モードの宣言フィールドだけを返し、`None` と `node_history` を混入させない。 |
| G-2 | inner graph の route 分岐 | `EvidenceReviewWorkflowGraph.route(State)` が success だけ `provenance`、error は `END` へ進むことを固定する。 |
| G-3 | E2E 5ノード到達 | `tests/integration/test_invoke_api.py::test_startup_loads_manifest_and_invokes_graph` で実 compiled graph を HTTP から通し、`review_packet_v1` を確認する。 |
| V-1 | PASS/BLOCK 対（本番の domain evidence 形） | `tests/unit/test_part_refresh_contracts.py::test_provenance_contract_has_provisioned_control_then_rejects_malformed_claim` で、正常 observation の PASS を先に示した後、`evidence_ids` が `list[str]` でない claim を `SecurityViolationError` にする。 |
| V-4 | 所見本文の型付き allowlist 射影 | `test_typed_narrative_projection_has_control_then_rejects_nested_raw_value` で正常な `str` 所見を通し、`observation={"raw": ...}` を拒否し、例外文へ LLM 値を出さない。 |
| L-2 | parse 失敗 fail-closed | `tests/parts/llm-draft-stage/test_error_boundary.py::test_broken_json_remains_fail_closed_at_exported_parser_and_node` で壊れた JSON を `status=error` にする。 |
| L-4 | LLM 障害境界の対 | 同梱テストで `llm=None` だけを success + degradation とし、repo 接合部では `test_requirement_provider_exception_is_error_without_fallback_or_raw_message` で provider 例外を `error_code=llm_call_failed` にする。 |
| L-5 | LLM 例外文の非露出 | provider marker が result / 統合 state / audit trace に無いことを同梱テストで、FIN-C2-188 の `RequirementSpecNode` result に無いことを repo 接合部テストで確認する。 |
| N-1〜N-4 | scope 供給・token・fallback・stale seam | `tests/parts/entry-adapter/test_entry_auth.py` を `SCOPE_KEYS=("case_id",)` に具体化し、header 非採用、401/403 境界、STG fallback、stale tuple seam を確認する。 |
| N-5 | 配線順序 E2E | 実 HTTP 経路で `authenticate(READ_INVOKE)` 後に request-scoped `PayloadStore.put()`、graph invoke、`finally` delete が行われ、生本文を envelope に入れないことを確認する。 |
| N-6 | operation policy | `tests/parts/entry-adapter/test_operation_policy.py` と `test_invoke_uses_read_operation_and_all_auth_context_fields` で、単一 `/invoke` が `READ_INVOKE` を宣言し、token / middleware / STG-default の source と principal 制約を固定する。 |

## Execution Result

2026-07-30 local execution: `pytest tests/ -q --tb=line` — 172 passed, 2 skipped.
`pytest tests/proof_of_boundary/ -q` and `ruff check src tests` are recorded in the MR-B handoff after the final run.

2026-08-14 component refresh:

- repo ルートの全収集（移行前）: `304 passed, 2 skipped, 11 errors`。11 errors は sandbox が `scripts/test_stg_invoke_evidence.py` の loopback socket bind を拒否した `PermissionError`。
- repo ルートの全収集（移行後）: `563 passed, 6 skipped, 18 errors`。18 errors は更新後の `scripts/test_stg_invoke_evidence.py` が追加した同一 socket fixture 配下で、すべて sandbox の loopback bind `PermissionError`。
- 同じ socket 利用テストだけを除外した全収集（移行後）: `562 passed, 6 skipped`。
- `ruff check src tests`: PASS。
- `check_manifest_schema.py config/agent.yaml`: PASS。

## 実 LLM 1 回転（§14-1-bis・push 前必須）

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-07-30 |
| 使用 LLM | ローカル CLI（注入シーム経由。テンプレの `src/` は不変） |
| 結果 | **PASS** — `observations` 2 件（`origin="deterministic"` 1 / `origin="llm"` 1）・`evaluation_state="completed"`・出力は非空 |
| 検出した欠陥 | 3 巡で 6 件（下記）。いずれも **モックでは露呈しなかった** |

実 LLM でのみ露呈した欠陥と対処:

| # | 欠陥 | 対処 |
|---|---|---|
| 1 | Stage 2 プロンプトに**出力 JSON スキーマの実体が無く**、`segment_id` 複製指示も無いため `_coerce_spec` が全件 fallback に落ちた | スキーマ・例・`segment_id` 複製指示をプロンプトに明記 |
| 2 | `population`/`approval_authority` は証拠側 `coverage` と**完全一致照合**なのに候補語彙を渡しておらず、自然文回答で**誤った gap** を検出しうる | 候補語彙をプロンプトへ注入し「候補に無ければ null」を明示 |
| 3 | Stage 2 が要求本文を読めない（§6 の解決許可主体に Stage 2 が無い）ため当てはめが成立しない | Stage 2 を解決許可主体に追加（`execute()` 内で一時解決・戻り値に含めない）。§6 更新 |
| 4 | **Stage 4 が LLM 所見に `origin` を付けず、§8-1（引用なし降格）と §7-3-1（params 双方向照合）が一度も発火していなかった** | Stage 4 で `origin="llm"` を付与。**変異検査で 2 テストが落ちることを確認済み** |
| 5 | Stage 5 が検証通過後の所見を出力契約の形に**正規化しない**ため、LLM が完璧な応答を返しても S-3 で必ず error | Stage 5 に正規化責務（`observation_id` 採番・`origin`・`review_priority` 補完・契約外キー除去） |
| 6 | LLM の `evidence_type` 自由文・捏造 `segment_id` で **AG 全体が error** | Stage 5 で `other` へ正規化 / `segment_id` を `null` へ正規化して降格 |

### 2 回目（2026-08-14・共通部品リフレッシュ後）

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-14 |
| 使用 LLM | ローカル CLI（注入シーム経由・**repo 外 cwd**。テンプレの `src/` は不変） |
| 結果 | **PASS** — LLM 呼び出し 2 回（Stage 2 要求抽出 / Stage 3 所見生成）・`status: success`・出力は非空 |
| 検出した欠陥 | 1 件（下記）。**全テスト緑・CI 緑の状態で潜在していた** |

| # | 欠陥 | 対処 |
|---|---|---|
| 7 | **`llm.complete(messages, timeout=...)` が `BaseLLM` 契約違反**。ABC は `complete(self, messages: list) -> dict` で `timeout` を取らないため、**契約に準拠した LLM クライアントを注入すると Stage 2 / Stage 3 とも必ず `TypeError` → `llm_call_failed` で落ちる**（`llm-draft-stage` v0.2.0 以降は縮退 success ではなく `status=error`）。`origin/develop` に元からあり、リフレッシュ起因ではない | 呼び出し 2 箇所（`src/nodes/inner/stages.py` Stage 2 / Stage 3）から `timeout=` を削除。per-call timeout はクライアント側の構築で持つべきもので、この呼び出し地点の責務ではない |

**なぜ全テスト緑ですり抜けたか** — テストの fake が `def complete(self, messages, timeout)` と
**本番契約と違う形**で定義されていた（`tests/integration/test_real_llm_regression.py` ×2、
`tests/proof_of_boundary/test_audit_and_checkpoint.py` ×1）。fake 自身が契約違反を受け入れる形だったため、
「実 LLM を模した回帰テスト」という名前のファイルが、まさにその契約違反を隠していた。
3 箇所を `complete(self, messages)` に揃えた結果、**production に `timeout=` を戻すと 2 件が赤くなる**
（PB-3 を含む）ことを変異検査で確認済み。同型は Travel Supplier Sustainability Claim Evidence Validator のレビューで出た指摘
（`llm.invoke(dict)` を呼んでいたが `BaseLLM` に `invoke` は無い）と同じ類型で、2 件目。

回帰テスト（`tests/integration/test_real_llm_regression.py`）は **実 LLM が実際に返す形**
（`observation_id`/`origin`/`review_priority` を含まない）を fake に固定している。
**完成品を返す fake では上記 4・5 が素通りする**ため、この形で書くことが再発防止の本体である。

## llm-draft-stage 0.3.0 の回帰テスト（LLM 出力の複数 JSON）

AGENTIC STAR の登録モデルが draft JSON を改行区切りで 2 回連結して返す、または JSON の後ろに `{` `}` を含む散文を付ける応答で、
Stage の parse が `Extra data` になり fail-closed していた。部品 `llm-draft-stage` を 0.3.0 に更新し、複数候補を走査して
一意な非空 object だけを受理する（相異なる複数は `AmbiguousJsonOutput` で fail-closed・`{}` / NaN / 配列は不受理）。
対応テスト: `tests/parts/llm-draft-stage/test_draft_node.py`（実物 fixture `fixtures/gpt54mini_duplicated_decision.txt` を含む）。
