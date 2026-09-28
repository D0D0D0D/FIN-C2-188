# Template Design Specification — FIN-C2-188 ExamEvidenceSufficiencyReviewAgent

> 本設計書は AgentCore SDK **1.0.0** の実体（wheel introspection・2026-07-30 実施）に照合済み。
> 照合記録は §14。敵対レビュー（2026-07-30）の指摘反映記録は §16。

---

## 1. Position in AgentCore Architecture

- **Agent Class**: `ExamEvidenceSufficiencyReviewGraph`（`src/graph/graph.py`）
- **L1 Base**: `AgentBaseGraph`（L1 直継承。`AutonomousBaseGraph` ではない）

> **継承欄はここまで。** `GraphNode` と内側の `EvidenceReviewWorkflowGraph` は**継承関係ではなく
> 構成（composition）**であり、アーキテクチャ詳細として §2／§4 に記す（継承欄に構成要素を
> 混ぜる書き方を避ける）。

- **Three-Layer Separation**:
  - **State**: flat TypedDict composition（`AgentState` を継承。Pydantic 不使用 — msgpack 非互換）
  - **Node**: L1 継承（Template Method: `execute(self, state) -> dict` のみ override）
  - **Graph**: composition（`register_nodes()` でスロットを埋める。外側の `add_edges()` は override しない）

固定 5 ノード backbone は framework が配線する。テンプレートが埋めるのは
`pre_process` / `main` / `post_process` の 3 スロットのみ。

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max_retry=3)
                                          pre_process
```

`initialize` / `finalize` は `super().register_nodes()` が自動注入する。
`route()` は override しない（`status` を読む framework 実装をそのまま使う）。

### Composition Pattern

- **Pattern**: `GraphNode`（main スロットに内側 domain workflow subgraph を置く Cat 2 標準構成）
- **Composition target**: `EvidenceReviewWorkflowGraph`（同一リポジトリ内・in-process）
- **Error propagation strategy**: `propagate`（内側エラーは `SubgraphError` として親へ伝播。
  部分的な所見を「成功」として返さない）
- **`propagate_hitl`**: `False`（HITL 非採用）

---

## 2. Architecture Overview

### 2-1. Node Configuration（外側）

| Node | クラス | 継承 | 責務 | 主な入力 State | 主な出力 State |
|------|--------|------|------|---------------|---------------|
| initialize | `InitializeNode` | framework 既定 | — | — | — |
| pre_process | `PreProcessNode` | `FunctionNode` | 内部 `input_context` の schema 検証・案件スコープ解決（fail-closed）・rubric 版検証・監査イベント | `input_context` | `case_id` / `request_payload_ref` / `evidence_index` / `owner_directory` / `response_checklist` / `rubric_version` / `status` |
| main | `EvidenceReviewWorkflowGraphNode` | `GraphNode` | 上流 error のパススルー判定 → 内側 subgraph 実行 → 結果を明示マップで親 State へ昇格 | 上記すべて | §4-A-3 の `ChildWorkflowOutput` を展開したフィールド |
| post_process | `PostProcessNode` | `FunctionNode` | S-3 出力契約（closed allowlist・再帰）検証・禁止キーの再帰排除・PII 再帰走査・`formatted_output` への畳み込み | main の出力 | `formatted_output` |
| finalize | `FinalizeNode` | framework 既定 | — | — | — |

### 2-2. 内側 domain workflow subgraph（5 ステージ）

`EvidenceReviewWorkflowGraph` は **`BaseGraph` を直継承**し、`register_nodes()` /
`add_edges()` / `route()` / `get_output()` を自前で持つ（内側は固定 3 スロット制約を受けない）。

| Stage | ノード | LLM | 責務 | 主な出力 |
|-------|--------|:---:|------|---------|
| 1 | `EvidenceNormalizeNode` | — | 検査要求本文を payload store から解決し、**決定論**でセグメント化（見出し・番号項目・箇条書き単位）。証拠 register を正規化し、**register 内部の不整合**（同一 doc_type で `current` が複数、`effective_from > effective_to`、所有者未登録）を検出 | `requirement_segments`（ID + 原文 span 範囲）/ `evidence_normalized` / `register_integrity_findings` |
| 2 | `RequirementSpecNode` | ✅ | 各要求セグメントから **`RequirementSpec` を型付きで抽出**（対象期間 / 母集団 / 統制行為 / 承認権限）。抽出状態は `extracted: {period, population, control_action, approval_authority}` の**軸ごとの bool**で明示し、値が無い軸は `false` + `null` とする。推測で埋めない。**`period` は `YYYY-MM` 形式の文字列配列**（例 `["2026-04","2026-05","2026-06"]`）— プロンプトで形式を明示し、範囲オブジェクト等を受け取った場合はその軸を抽出不能として `false` に倒す（Stage 3 の月照合と対の契約） | `requirement_specs` |
| 3 | `CoverageMatchNode` | — | `requirement_specs` × `evidence_normalized` を**決定論照合**。期間カバレッジ欠落・母集団不一致・承認権限不一致・要求期間に該当する `superseded` 版を算出し、**決定論所見**と `review_priority` を生成 | `deterministic_observations` / `version_conflicts` / `coverage_matrix` |
| 4 | `SufficiencyNarrativeNode` | ✅ | Stage 3 の決定論所見と証拠本文を入力に、「存在するが要求に対しなぜ実質不十分か」の**意味的所見**と追加・代替証拠案を、`observation_kind` を宣言したうえで引用付きで構成 | `llm_observations` / `suggested_additional_evidence` |
| 5 | `ProvenanceVerifyNode` | — | claim→source 実在照合（捏造 ID 破棄）、**span_kind allowlist** による引用可否判定、**引用が付かない LLM 所見の降格**（§8）、`unresolved_questions` の確定 | 検証済み observations / `unresolved_questions` / `evaluation_state` |

**LLM は Stage 2 と Stage 4 のみ。** Stage 1 / 3 / 5 は決定論であり、LLM 不在でも単体で動作する
（縮退動作は §11）。

> **Stage 順序の根拠（レビュー High 指摘の反映）**: 「要求期間に対するカバレッジ欠落」は
> 要求期間が抽出されて初めて算出できる。したがって決定論照合（Stage 3）は LLM 抽出（Stage 2）の
> **後**に置く。Stage 1 が行う決定論処理は、要求に依存しない register 内部の整合検証に限定する。

### 2-3. Data Flow

```
[規制対応担当] ──POST /invoke（外部 DTO: 本文つき）──> entry adapter
   │  ① 認証・trust 解決（VERIFIED_EXTERNAL 未満は 403）
   │  ② case_id 必須検証（欠落は 400）
   │  ③ request_text / evidence_register[].content_text を payload_store.put()
   │     → 本文フィールドを削除し、ref のみの内部 input_context を組み立てる
   v
pre_process  … 内部 input_context の schema 検証 / rubric 版検証 / fail-closed
   v
main (GraphNode)  … 上流 status=error はパススルー（§10 S-2）
   ├─ Stage 1 決定論: 要求セグメント化 + register 内部整合
   ├─ Stage 2 LLM  : RequirementSpec 抽出（型付き・未抽出は明示）
   ├─ Stage 3 決定論: カバレッジ照合 + review_priority 算出
   ├─ Stage 4 LLM  : 意味的所見 + 追加証拠案（引用付き）
   └─ Stage 5 決定論: claim→source 照合 + span_kind allowlist + 降格
   v
post_process … closed allowlist（再帰） / 禁止キー排除 / PII 再帰走査
   v
[出力: reviewパケット] ──> 規制対応責任者のレビュー・回答起草・提出判断（人間）
```

---

## 3. 入力契約 — 外部 DTO と内部 `input_context` を分離する

**クライアントは payload_ref を一切送れない。** 本文フィールドを送り、adapter が
サーバ側で ref を発行する。これにより「偽装 ref で隔離層をバイパスする」経路が構造的に消える。

### 3-0. AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は `ExamEvidenceSufficiencyReviewGraph` を継承した
`MarketplaceGraph` を公開する。Marketplace runner の入力はプレーン文字列 1 本なので、adapter は
その文字列を検査要求本文として短命 `PayloadStore` に格納し、次の内部 context を**新規構築**する。
runner が渡す `conversation_history` その他の `input_context` はマージしない。

```json
{
  "case_id": "<STG_DEFAULT_CASE_ID>",
  "request_payload_ref": "<adapter-minted ref>",
  "evidence_index": [],
  "response_checklist": [],
  "owner_directory": [],
  "rubric_version": null
}
```

scope のキーと環境変数名は `src/entry_adapter` の `SCOPE_KEYS` / `STG_DEFAULT_SCOPE_ENVS`
だけから組み立てる。宣言不整合、未設定、空文字、文字列中の空白は graph 構築時に fail closed とする。
`InvocationContext.caller_trust_level` は adapter が `VERIFIED_EXTERNAL` に固定し、HITL と memory は無効化する。
JSON 文字列が `mode` を宣言する場合は `invoke` 以外を拒否し、adapter は書込・resume 経路を公開しない。

Marketplace の文字列契約には証拠 register を載せる別 channel がないため、Marketplace 実行の
`evidence_index` は常に空であり、結果は `evaluation_state="not_performed"` となる。これは十分性を
推測する縮退ではない。証拠つきレビューは §3-1 の HTTP DTO を使用する。

Azure LLM は `config["llm"]` の既存 injection seam にだけ接続する。接続値は invocation context の
secret provider から LLM 操作ごとに再解決し、その都度 client を生成する。したがって adapter/graph
インスタンスは API key・endpoint・deployment も client も保持しない。LLM の影響範囲は既存どおり
Stage 2（型付き要求抽出）と Stage 4（引用付き叙述）だけであり、Stage 3 の coverage 照合と Stage 5 の
provenance 検証・降格、S-3 closed output gate は変更しない。

### 3-1. 外部 DTO（`POST /invoke` の body・`src/api/server.py` のみが扱う）

| キー | 型 | 必須 | 内容 |
|------|----|:--:|------|
| `input` | `str` | ✅ | 検査要求の見出し（短文。全文は `request_text`） |
| `session_id` | `str` | ✅ | セッション ID |
| `case_id` | `str` | ✅ | 検査案件 ID。欠落・空文字・空白のみは **400 fail-closed** |
| `request_text` | `str` | ✅ | 検査要求の全文。**必須**（無いと要求セグメント化・`RequirementSpec` 抽出が成立せず、本テンプレートの価値が消えるため任意にしない）。空文字・空白のみは 400 |
| `evidence_register` | `list[dict]` | ✅ | 証拠エントリ（§3-3）。空配列は許容 |
| `response_checklist` | `list[dict]` | — | `checklist_id` / `label` |
| `owner_directory` | `list[dict]` | — | `owner_id` / `role_label`（**氏名・連絡先は受け取らない**） |
| `rubric_version` | `str` | — | 適用する十分性 rubric の版（省略時は config の版。指定時は config と完全一致が必要・§9-1） |

**未宣言キーは 400 で reject**（`payload_ref` 系を含む — クライアントは ref を送れない）。

### 3-2. 内部 `input_context`（adapter → `agent.invoke(..., input_context=...)`）

| キー | 型 | 内容 |
|------|----|------|
| `case_id` | `str` | 検証済み案件 ID |
| `request_payload_ref` | `str` | adapter が発行した ref |
| `evidence_index` | `list[dict]` | 証拠の**構造化メタ**（本文を除く。`content_payload_ref` を含む） |
| `response_checklist` | `list[dict]` | 正規化済み |
| `owner_directory` | `list[dict]` | 正規化済み（`owner_id` / `role_label`） |
| `rubric_version` | `str` | 解決済みの版 |

本文（`request_text` / `content_text`）は **この時点で削除済み**であり、State に到達しない。

### 3-3. 証拠エントリ

| キー | 型 | 必須 | 内容 |
|------|----|:--:|------|
| `evidence_id` | `str` | ✅ | 参照 ID。`^[A-Za-z0-9_-]{1,64}$`。**ダッシュ区切り UUID 形式は禁止**（§7-3） |
| `title` | `str` | ✅ | 表示ラベル。**120 文字以内・入口で `detect_pii` を明示適用し検出時は reject**（§6） |
| `doc_type` | `str` | ✅ | 列挙: `policy` / `procedure` / `control_record` / `approval_record` / `system_log` / `audit_report` / `other` |
| `version` | `str` | ✅ | 版（`^[A-Za-z0-9._-]{1,32}$`） |
| `version_status` | `str` | ✅ | 列挙: `current` / `superseded` / `draft` |
| `effective_from` / `effective_to` | `str` (ISO date) | — | 有効期間 |
| `coverage` | `dict` | — | `period_from` / `period_to`（ISO date）／`population_scope`（`^[A-Za-z0-9_-]{1,64}$`）／`approval_authority`（同左） |
| `owner_id` | `str` | — | `^[A-Za-z0-9_-]{1,64}$`。`owner_directory` に存在しない値は **reject**（§5-2） |
| `content_text` | `str` | — | 本文（外部 DTO のみ。adapter が put して削除する） |

- 件数上限 `max_evidence_entries`（既定 200）を超えたら 400
- `evidence_id` の重複は 400

> 🔴 **本文の必須／任意は 2 つに分かれる（混同しない）**:
> - **検査要求の本文 `request_text` は必須**（上表）。これが無いと Stage 1 のセグメント化と
>   Stage 2 の `RequirementSpec` 抽出が成立しない
> - **各証拠の本文 `content_text` は任意**。無い証拠は**構造化メタ（`coverage` 等）だけで
>   Stage 3 の決定論照合の対象になる**。ただし Stage 4 の意味的所見（`origin="llm"`）は
>   本文が無い証拠に対しては生成できない（引用が付けられないため §8-1 で降格される）
>
> `content_text` を持たない証拠には **`content_payload_ref` を発行しない**（adapter が put しない）。
> Stage 1 / 4 / 5 は `content_payload_ref` が**キーごと存在しない**場合、解決を試みずに読み飛ばす
> （存在しない ref を解決しにいかない）。`ref` はあるが解決できない場合（TTL 切れ等）とは
> **別事象として扱い**、§11-2 で挙動を分ける。

### 3-4. 入力形式の割り切り（正典 §2-4 既定適用）

**PDF・Excel（xlsx）・その他バイナリは受け付けない。** 受け付けるのは上記 JSON 構造と
プレーンテキストのみ。パーサを持たない形式には攻撃面が無く、OCR 崩れによる誤所見も生じない。
ファイルアップロード経路は実装しない。

ただし**経路を塞いでも、貼り付けで同じ事故は起きる**（GMP Manufacturing Deviation & CAPA Report Drafting Agent / EnvironmentalImpactAssessmentDraftingAgent で実際に指摘された型）。
`request_text` / `content_text` には adapter で**品質ゲート**をかけ、不合格は 400 で reject する:

| # | 検査 | 拒否条件 |
|---|------|---------|
| 1 | **マジックバイト（先頭シグネチャ）** | `%PDF-` / `PK\x03\x04`（Office・zip）/ `\xd0\xcf\x11\xe0`（旧 Office）/ `\x89PNG` / `\xff\xd8\xff`（JPEG）で始まる。**拡張子・Content-Type の自己申告は信用しない** |
| 2 | **置換文字比率** | U+FFFD（�）が全体の 1% 超（文字化け・誤デコード） |
| 3 | **制御文字比率** | タブ・改行を除く制御文字が 0.5% 超（バイナリの誤貼り付け・OCR 崩れ） |
| 4 | **可読文字比率** | 日本語（かな・漢字）+ ASCII 英数記号が全体の 90% 未満 |
| 5 | **長さ** | 空文字、または `max_text_chars`（既定 200,000）超 |

拒否した場合は `rejected[]` に `{"field": ..., "reason_code": ...}` を返し、**どの検査で落ちたかを
呼び出し側に伝える**（黙って握りつぶさない）。

---

## 4. 受渡し契約（内側グラフ／隣接テンプレート）

### 4-A. 親グラフ ⇄ 内側グラフ

> **SDK 実体（§14 で確認）**: `GraphNode.execute()` は
> `subgraph.invoke(user_input, session_id=ctx.session_id, ctx=ctx)` しか呼ばない。
> **`input_context` も親 State も内側へ渡らない。** したがって受渡しは
> `extract_input()` の**文字列 1 本**に閉じる必要がある。

### 4-A-1. `extract_input(state) -> str`

親 State の必要フィールドを **JSON 直列化した 1 文字列**として渡す。
**本文は含まない**（ref と構造化メタのみ）。

```json
{
  "case_id": "...",
  "request_payload_ref": "...",
  "evidence_index": [ ... ],
  "response_checklist": [ ... ],
  "owner_directory": [ ... ],
  "rubric_version": "1.0"
}
```

内側の Stage 1 が `json.loads(state["user_input"])` で復元する。復元に失敗した場合は
`status="error"`（内側 fail-closed）。

### 4-A-2. 内側 `get_output(state) -> dict`（override 必須）

標準の `AgentBaseGraph.get_output()` は `formatted_output` / `result` しか載せず、
Stage 1〜5 の個別結果を親へ渡せない。内側は `get_output()` を override して
**`ChildWorkflowOutput` を丸ごと返す**。

### 4-A-3. `ChildWorkflowOutput` schema

| キー | 型 | 内容 |
|------|----|------|
| `evaluation_state` | `str` | 列挙: `completed` / `deterministic_only` / `not_performed` |
| `requirement_segments` | `list[dict]` | `segment_id` / `summary` / `required_evidence_types[]` |
| `observations` | `list[dict]` | §7-1 の observation（決定論・LLM を `origin` で区別） |
| `version_conflict_observations` | `list[dict]` | 同上の形 |
| `suggested_additional_evidence` | `list[dict]` | `segment_id` / `evidence_type` / `rationale` / `source_spans[]` |
| `accountable_owners` | `list[dict]` | `segment_id` / `owner_id` |
| `evidence_sources` | `list[dict]` | `evidence_id` / `title` / `version` / `version_status` |
| `unresolved_questions` | `list[dict]` | `question_id` / `segment_id` / `question_code` / `params` |
| `degradation_reason` | `str \| None` | 縮退理由 |
| `status` | `str` | `AgentStatus` の値 |

### 4-A-4. `merge_output(state, sub_result) -> dict`

`sub_result` の宣言済みキーのみを**明示マップ**で親 State へ昇格する
（raw pass-through は composition consistency 違反）。`node_history` は返さない（reducer 管理）。
`error_log` は非空時のみ返す。

### 4-B. 隣接テンプレート `Bank Regulatory Examination Request Response Drafting Agent` との handoff contract

> 設計着手条件として要求された「**機械的に強制可能な** handoff contract」。
> 4-A（親 ⇄ 内側のシリアライズ契約）とは別物であり、こちらが**テンプレート間**の契約である。

#### 4-B-1. 方向と所有

| 項目 | 内容 |
|---|---|
| producer | **本テンプレートのみ**。reviewパケットを出力する |
| consumer | **人間（規制対応担当）**。本テンプレートは 171 を**呼び出さない**（`RemoteAgentNode` も下流 `GraphNode` も持たない） |
| 逆方向（171 → 本テンプレート） | **禁止**。本テンプレートは 171 の出力を入力に取らない（外部 DTO schema に該当キーが無く、未宣言キーは 400） |
| handoff の実行主体 | **人間**。本テンプレートの出力を確認し、**証拠を確定したうえで**人間が 171 を起動する。自動連携の経路を実装しない |

#### 4-B-2. versioned schema — `review_packet_v1`

`formatted_output` は **`contract_version: "review_packet_v1"`** を必須キーとして持つ（§7-1）。
下流が消費する場合、**この版文字列が一致しない payload は消費しない**（版を上げるときは
`review_packet_v2` を新設し、旧版の意味を変えない）。

搬送されるのは §7-1 の宣言済みキーのみ。**回答叙述を運ぶフィールドは schema に存在しない。**

#### 4-B-3. 人間確定の precondition

`requires_human_review` は常に `true`（§7-2）。171 への引き渡しは「人間が証拠を確定した後」が前提だが、
**本テンプレート側に「確認済み」を立てる手段は意図的に持たせない** — 立てられると自動連携の抜け道になるため。
人間確認の記録は 171 側の入力または運用手順（`docs/07_operation_guide.md`）で担保する。

#### 4-B-4. rejection conditions（機械検査可能）

| # | 条件 | 検査場所 |
|---|---|---|
| 1 | `formatted_output`（ネスト含む）に回答叙述キー — `draft_response` / `response_text` / `response_body` / `submission_package` / `submission_payload` — が現れる | post_process の closed allowlist（再帰）が reject |
| 2 | 外部 DTO に 171 由来のキー — `draft_response` / `response_draft` / `examination_response` — が含まれる | adapter が 400（未宣言キー reject） |
| 3 | `contract_version` の欠落・不一致 | post_process が `raise` |
| 4 | `src/` に 171 を呼ぶ実装（`RemoteAgentNode` / 171 のエンドポイント参照）が存在する | 静的検査（MR-B テスト #4） |

#### 4-B-5. MR-B の受入テスト（negative integration）

| # | テスト | 押さえる越境 |
|---|---|---|
| 1 | `formatted_output` に `draft_response` を注入 → post_process が reject | 本テンプレート → 下流に回答叙述が流出 |
| 2 | 外部 DTO に `response_draft` を入れて `/invoke` → 400 | 171 → 本テンプレートへの逆流入 |
| 3 | `contract_version` を欠落／改変 → reject | 版の取り違え |
| 4 | `grep -r "RemoteAgentNode\|Bank Regulatory Examination Request Response Drafting Agent" src/` が空であることを静的に検証 | 自動連携の実装混入 |

---

## 5. State Definition

`src/schemas/state.py` — `AgentState` を継承した flat TypedDict。

### 5-1. フィールド

| フィールド | 型 | 由来 | 用途 |
|-----------|----|------|------|
| `case_id` | `str` | pre_process | 検査案件 ID（スコープキー） |
| `request_payload_ref` | `str` | pre_process | 検査要求本文の不透明 ref |
| `evidence_index` | `list[dict]` | pre_process | 証拠の**構造化メタのみ**（本文なし） |
| `response_checklist` | `list[dict]` | pre_process | 応答チェックリスト（ID + ラベル） |
| `owner_directory` | `list[dict]` | pre_process | 所有者ディレクトリ（役割 ID のみ） |
| `rubric_version` | `str` | pre_process | 適用 rubric 版（未承認時は `-SAMPLE` 付き） |
| `rubric_approved` | `bool` | pre_process | 顧客承認済み rubric か（§9-1） |
| `evaluation_state` | `str` | main | `completed` / `deterministic_only` / `not_performed` |
| `requirement_segments` | `list[dict]` | main | 要求セグメントと必要証拠種別 |
| `observations` | `list[dict]` | main | 検証済み所見（決定論 + LLM） |
| `version_conflict_observations` | `list[dict]` | main | 版不整合の所見 |
| `suggested_additional_evidence` | `list[dict]` | main | 追加・代替証拠案 |
| `accountable_owners` | `list[dict]` | main | セグメント → 所有者 ID |
| `evidence_sources` | `list[dict]` | main | 出力に現れた証拠の一覧 |
| `unresolved_questions` | `list[dict]` | main | 人間が確認すべき固定論点（コード化） |
| `requires_human_review` | `bool` | post_process | 常に `true`（§7-2） |
| `degradation_reason` | `str \| None` | 各 Stage | 縮退理由（`+` 結合） |

### 5-2. `owner_directory` / `response_checklist` の join 規則

| 規則 | 内容 |
|------|------|
| owner の解決 | `evidence.owner_id` → `owner_directory[].owner_id` で join。**未登録 owner_id は入口で reject**（400） |
| `accountable_owners` の決定 | セグメントに紐づく証拠のうち、`review_priority` が最も高い証拠の `owner_id`。同順位が複数なら全件返す |
| owner 不在 | `accountable_owners` に載せず、`unresolved_questions` に `question_code="owner_unassigned"` を立てる |
| checklist の用途 | Stage 3 の決定論照合で「チェックリスト項目に対応する要求セグメントが無い」場合に `question_code="checklist_item_unmapped"` を立てる。**十分性の判定には使わない** |
| `checklist_id` 重複 | 入口で reject（400） |

### 5-3. State 制約（必須）

- flat TypedDict のみ（primitives + JSON シリアライズ可能型）
- JWT / API キー / credential を **State に置かない**（checkpoint DB へ平文で残るため）
- **`InvocationContext` を State に独自フィールドとして追加しない。** ノード内では
  `InvocationContext.from_state(state)` で framework 管理フィールド
  （`correlation_id` / `session_id` / `thread_id` / `trace_id` / `caller_id` /
  `caller_trust_level` / `hitl_allowed`）から**再構成**する。`InvocationContext(...)` を
  直接構築しない（`NullProvider` が束縛され `ctx.secrets.require()` が必ず失敗する）
- Pydantic / dataclass / 任意 Python オブジェクトを置かない（msgpack 非互換）
- **検査要求本文・証拠本文を State に置かない**（§6）

---

## 6. 機微情報の隔離（payload_ref 方式）

検査要求と証拠本文は機微な金融情報であり、**checkpoint に平文で永続化してはならない**
（提案段階で識別した主要リスクの 1 つ）。

| 層 | 実装 |
|----|------|
| 入口 | adapter が本文を `payload_store.put(body, scope={"case_id": ...}, session_id=...)` に預け、内部 `input_context` には**不透明 ref のみ**を載せる。**外部 DTO に ref フィールドが存在しない**ため、クライアントが ref を持ち込む経路が無い（§3-1） |
| グラフ内 | **Stage 1 / Stage 4 / Stage 5** が `execute()` 内で解決し、**戻り値には本文を含めない**。`user_input` / `validated_input` / `llm_response` にも載せない |
| 解決の許可主体 | adapter（put のみ）と **内側 Stage 1 / 2 / 4 / 5**（read のみ）。それ以外のノードは解決しない。**Stage 2 は要求本文が無いと当てはめ自体が成立しない**ため read を許可する（MR-B の実 LLM 検証で判明・他ステージと同じく `execute()` 内で一時解決し戻り値には含めない） |
| replay 防止 | **single-use は「invocation をまたぐ再利用の禁止」**であり、同一 invocation 内での複数回 read は許可する（Stage 4 が本文を読んだ後に Stage 5 が span を再検証する必要があるため）。invocation 終了時に adapter が `delete` する |
| TTL | `payload_ttl_seconds`（既定 300 秒）。**TTL 切れは fail-closed ではなく縮退**（§11） |
| 監査 | 解決のたびに `emit_trace_event("evidence_payload_resolved", {"evidence_id": ..., "stage": ...}, state)` |

**`evidence_index` に載せてよいのは構造化メタのみ** — ID・列挙値・日付・版・所有者 ID・`title`。
`title` は自由文であるため、**入口で 120 文字上限 + `detect_pii` を明示適用し、検出時は reject**
する（「構造化メタだから安全」とは扱わない）。新しいキーが増えたら自動的に「載せない側」になる
allowlist 方式にする（denylist は必ず漏れる）。

---

## 7. 出力契約と S-3（design-entry blocker (iii)）

### 7-1. `formatted_output` の closed allowlist（再帰・宣言外キーは全 reject）

| キー | 型 | 内容 |
|------|----|------|
| `contract_version` | `str` | `"review_packet_v1"` 固定（§4-B-2。欠落・不一致は reject） |
| `mode` | `str` | `"invoke"` 固定 |
| `case_id` | `str` | 検査案件 ID |
| `rubric_version` | `str` | 適用した十分性 rubric の版（未承認時は `-SAMPLE` 付き） |
| `rubric_approved` | `bool` | 顧客承認済み rubric で評価したか（§9-1）。監査はこの 1 値で判定できる |
| `evaluation_state` | `str` | `completed` / `deterministic_only` / `not_performed` |
| `requirement_segments` | `list[dict]` | 下表 |
| `observations` | `list[dict]` | 下表 |
| `version_conflict_observations` | `list[dict]` | observation と同形 |
| `suggested_additional_evidence` | `list[dict]` | 下表 |
| `accountable_owners` | `list[dict]` | `segment_id` / `owner_id`（**役割 ID のみ**） |
| `evidence_sources` | `list[dict]` | `evidence_id` / `title` / `version` / `version_status` |
| `unresolved_questions` | `list[dict]` | 下表 |
| `requires_human_review` | `bool` | 常に `true` |
| `degradation_reason` | `str \| None` | 縮退理由 |

**`requirement_segments[]`**

| キー | 型 | 制約 |
|---|---|---|
| `segment_id` | `str` | `^SEG-[0-9]{1,3}$` |
| `summary` | `str` | 320 文字以内 |
| `required_evidence_types` | `list[str]` | 列挙: `period` / `population` / `control_action` / `approval_authority` |

**`observations[]` / `version_conflict_observations[]`**

| キー | 型 | 制約 |
|---|---|---|
| `observation_id` | `str` | `^OBS-[0-9]{1,4}$` |
| `segment_id` | `str` | `requirement_segments` に実在すること |
| `observation_kind` | `str` | **列挙のみ**: `period_gap` / `population_gap` / `control_action_gap` / `approval_authority_gap` / `document_absent` / `version_conflict` / `coverage_unverifiable` |
| `origin` | `str` | 列挙: `deterministic` / `llm` |
| `evidence_ids` | `list[str]` | 各要素は `evidence_sources` に実在すること。`observation_kind="document_absent"`（要求された証拠種別が register に無い）の場合のみ空配列を許容し、それ以外は **1 件以上必須** |
| `observation` | `str` | 400 文字以内。`origin="deterministic"` の場合は**決定論 renderer が生成**（LLM は書かない） |
| `uncertainty` | `str` | 列挙: `low` / `medium` / `high` |
| `review_priority` | `str` | 列挙: `high` / `medium` / `low`（**決定論導出**・§7-4） |
| `observation_params` | `dict` | **必須**。`observation_kind` ごとに固定キー（§7-3-1）。値は ID・列挙値・日付・数量のみ（自由文禁止）。Stage 3 の決定論結果に実在する値だけを取れる |
| `source_spans` | `list[dict]` | 下表。`origin="llm"` では 1 件以上必須（0 件なら §8 により降格される） |

**`source_spans[]`**

| キー | 型 | 制約 |
|---|---|---|
| `evidence_id` | `str` | `evidence_sources` に実在 |
| `span_kind` | `str` | §8 の引用可 5 種のみ（引用不可分類は出力に現れない） |
| `start` / `end` | `int` | **NFKC 正規化前の原文**に対する文字オフセット（0-origin・`end` 排他）。正規化は分類のためだけに行い、オフセットは常に原文基準 |
| `text` | `str` | 原文 `[start:end]` と**完全一致**すること（Stage 5 が照合）。**長さ下限 8 文字**・上限 `max_quotable_span_chars`（既定 120）。下限が無いと空文字・1 文字が `text in source` を常に満たし、逐語照合が vacuous になる |

**`suggested_additional_evidence[]`**

| キー | 型 | 制約 |
|---|---|---|
| `segment_id` | `str` | 実在 |
| `evidence_type` | `str` | 列挙: `doc_type` の 7 値 |
| `rationale` | `str` | 320 文字以内 |
| `source_spans` | `list[dict]` | 上表と同形（0 件可） |

**`unresolved_questions[]`** — **自由文を持たない**

| キー | 型 | 制約 |
|---|---|---|
| `question_id` | `str` | `^Q-[0-9]{1,3}$` |
| `segment_id` | `str \| null` | 実在または `null` |
| `question_code` | `str` | 列挙: `requirement_period_not_extractable` / `requirement_population_not_extractable` / `requirement_approval_authority_not_extractable` / `nonquotable_source_requires_human_review` / `evidence_content_unavailable` / `owner_unassigned` / `checklist_item_unmapped` / `version_conflict_needs_owner_confirmation` |
| `params` | `dict` | 値は **ID・列挙値・日付・数値のみ**（自由文禁止）。例: `{"evidence_ids": ["EV-001"], "missing_months": ["2026-05"]}` |

**構造的に持たないフィールド**（生成不能にする＝denylist で文言を弾くのではなく、
スキーマに存在させない）:
`regulatory_interpretation` / `response_draft` / `submission_decision` /
`applicability_determination` / `record_mutation` / `compliance_verdict` /
`examination_outcome_prediction`。

post_process は top-level だけでなく**ネスト内も再帰的に**未宣言キー・未知 enum 値・
長さ超過を reject する（ネストへの密輸を塞ぐ）。

**出力言語**: `summary`、`origin="deterministic"` の `observation`、および
`suggested_additional_evidence[].rationale` は日本語で出力する。LLM には日本語出力を指示する。
技術キー・ID・enum・`source_spans[].text`（原文完全一致が必要な逐語引用）はこの限りでない。

### 7-2. 非結論性の担保 — denylist は実装しない（正典 §2-4 既定適用）

**自由文に対する禁止語・断定表現の denylist は実装しない。** 自然言語の言い換えは無限で
原理的に閉じず、「守っているように見えて守らない」false assurance になる
（Logistics Dangerous-Goods Document Consistency Agent / Financial Regulatory Consultation Response Evidence Brief Agent で独立に同じ裁定）。

代わりに**構造**で担保する:

| # | 機構 | 何を防ぐか |
|---|------|----------|
| 1 | closed allowlist（再帰） | 判定フィールドそのものが存在しない |
| 2 | `observation_kind` の**列挙必須** | 所見の型が 7 種に閉じる。「規制に適合している/していない」という型の主張を宣言できない |
| 3 | `origin="deterministic"` の `observation` は**決定論 renderer が生成** | 事実記述部分に LLM の文字列が入らない |
| 4 | `uncertainty` 必須 | 確定値を作れない |
| 5 | `requires_human_review` を `true` 固定 | 本エージェント側で「レビュー不要」に落とせない |
| 6 | `unresolved_questions` の **question_code 化** | 論点の自由文経路を塞ぐ |
| 7 | claim→source 実在照合（§7-3） | 捏造 ID |
| 8 | span_kind allowlist（§8） | 証跡側の裁定文を引用経由で持ち込む経路 |

> **残余リスク（正直に記す・ユーザー裁定で受容済み）**: `origin="llm"` の `observation` と
> `suggested_additional_evidence[].rationale` は LLM が書く自由文であり、
> **`observation_kind` が宣言する型の範囲内で断定的な言い回しになる可能性は残る**。
> これは lexicon では閉じない（原理的に列挙側が負ける）。緩和は上表の 2/3/4/5/8 と
> 人間レビュー前提（`requires_human_review=true`）であり、**保証ではない**。
>
> **2026-07-30 ユーザー裁定（🔴1）**: この残余は**受容する**。完全構造化（LLM に文章を
> 書かせず決定論 renderer で描画する）は CTDClinicalSummaryAgent で過剰設計と判定され巻き戻された前例が
> あり、本テンプレートの価値の核（「あるのに足りない」の意味的説明）を定型文に潰すため採らない。
> **後続の独立レビューで同点を指摘された場合も、設計判断として受容した旨を回答する**
> （設計変更はしない）。この残余は `docs/07_operation_guide.md` に顧客向けにも明記する。

### 7-3. claim→source 実在照合

全 observation は `evidence_ids[]` に **Stage 1 が実在確認した `evidence_id`** のみを持てる。
Stage 5 が照合し、register に存在しない ID を参照する observation は破棄して
`unresolved_questions` へ降格する（捏造 ID の混入防止）。

#### 7-3-1. ID 照合だけでは中身の忠実性を担保しない — `observation_params` の双方向照合

**ID が実在することと、所見の中身が入力と一致することは別問題である**（GMP Manufacturing Deviation & CAPA Report Drafting Agent で実際に出た指摘と同型）。
`source_spans` は「証拠原文からの逐語引用」しか照合しないため、`observation` 本文に LLM が書いた
数値・期間・列挙値（例「2026-05 分をカバーしていない」）が Stage 3 の決定論結果と食い違っても、
ID 照合も span 照合も発火しない。

そこで全 observation に **`observation_params`（構造化パラメータ）を必須**とし、Stage 5 が
**双方向**に照合する:

| 方向 | 照合内容 | 不一致時 |
|------|---------|---------|
| params → 決定論結果 | `observation_params` の各値が **Stage 3 の `coverage_matrix` / `version_conflicts` に実在**すること（欠落月・母集団名・承認権限・版は決定論側が算出した値のみ許可） | 当該 observation を破棄し `unresolved_questions` へ降格 |
| 本文 → params | `observation` 本文から抽出した**日付・年月・数量・列挙値**が、すべて `observation_params` に含まれること（本文だけに現れる数値を許さない） | 同上 |

`observation_params` のキーは observation_kind ごとに固定する:

| `observation_kind` | 必須 params |
|---|---|
| `period_gap` | `missing_periods: list[str]`（`YYYY-MM`。Stage 3 が算出した値のみ） |
| `population_gap` | `required_population: str` / `covered_population: str` |
| `control_action_gap` | `required_control_action: str` |
| `approval_authority_gap` | `required_authority: str` / `evidence_authority: str` |
| `document_absent` | `required_evidence_type: str` |
| `version_conflict` | `conflicting_versions: list[str]` / `conflict_attribute: str` |
| `coverage_unverifiable` | `reason_code: str` |

**本文からの数値抽出は NFKC 正規化 + 漢数字変換 + 全角数字の正規化**を前段に通す
（`２０２６年５月` / `二〇二六年五月` が素通りするため）。

> **この照合は 🔴1 の裁定（LLM が説明文を書く中間案）と両立する。** LLM は説明の**文章**を書くが、
> **事実値は `observation_params` に構造化して宣言**しなければならず、本文の数値はその宣言と
> 一致しなければ通らない。文章表現の自由は残しつつ、数値の忠実性は機械が担保する。

> **ID 形式の制約（実測に基づく）**: `evidence_id` および内部発行 ID は
> `^[A-Za-z0-9_-]{1,64}$` とし、**ダッシュ区切りの UUID 形式（`str(uuid4())`）を使わない**。
> SDK 1.0.0 の `detect_pii` に対し 20,000 件を実測したところ、ダッシュ区切り UUID は
> **0.535%** が `phone_jp` / `my_number_jp` として誤検知され `[MASKED]` に破壊書換される
> （ダッシュが電話番号の区切りに一致するため）。**ダッシュ無しの hex（`uuid4().hex`）は
> 20,000 件で 0 件**だったので、ランダム ID が必要な場合は hex 形式を用いる。

### 7-4. `review_priority` は決定論で導出する

「どの不足・版不整合を先に確認すべきか」の順序は、**LLM に書かせず決定論で算出**する
（正典 §2-2: 非断定と重大度伝達は別問題 — severity を決定論導出して構造化提示）。

| 条件（Stage 3 の決定論照合結果から） | `review_priority` |
|---|---|
| 要求期間に対しカバレッジ欠落が 1 か月以上 | `high` |
| `version_status="superseded"` の証拠が要求期間に該当 | `high` |
| 同一 doc_type で `current` が複数存在し `approval_authority` が異なる | `high` |
| カバレッジ欠落が 1 か月未満、または `draft` 版の混在のみ | `medium` |
| 上記以外 | `low` |

`review_priority` は「人間がどれから見るか」の並べ替え情報であり、**規制上の重大性の判定ではない**
旨を `docs/07_operation_guide.md` に明記する。

---

## 8. span_kind allowlist — 引用可否を機械が決める

証拠側の文書（内部監査報告書・統制記録など）には「本統制は有効に運用されている」のような
**裁定文が載っていることがある**。逐語引用は出所（provenance）を保証するだけで、
引用内容の非結論性は保証しない — 正しく引用するほど結論が漏れる。
本テンプレートは出力に規制的結論を混入させてはならない制約が核にあるため、
MunicipalContractVariationEvidenceBriefAgent で確立した `span_kind` allowlist を適用する。

| 分類 | 引用 | 例 |
|------|:--:|------|
| `quantity` | ✅ | 「対象件数 1,240 件」 |
| `date` | ✅ | 「2026年4月1日から2026年5月31日まで」 |
| `identifier` | ✅ | 「規程番号 CR-114」 |
| `observed_condition` | ✅ | 「承認欄が空欄である」 |
| `procedural_record` | ✅ | 「四半期ごとにレビューを実施した記録が残されている」 |
| `adjudicative` | ❌ | 「本統制は有効である」「規程に適合している」 |
| `opinion` | ❌ | 「〜と解される」「所見としては〜」 |
| `unclassified` | ❌ | **既定値（fail-closed）** — 分類器が知らない文型は自動的に引用不可 |

**設計原則**
- 分類は**決定論**（`src/services/span_classifier.py`）。LLM に「これは事実か」を聞かない
  （結論文を事実と誤答した瞬間に穴が開く）
- LLM の役割は「どの span を引くか」の**選択のみ**。引いてよいかの判定は機械が持つ
- 評価的表現は「語」でなく**文型**で落とす（`〜はない` / `〜できる` / `〜が相当` 等）。
  語を消す対処は誤り（同じ語が正当な事実記述にも現れるため）
- 分類の前段に **NFKC 正規化 + zero-width 文字除去**を必ず通す。**オフセットは原文基準**で保つ
- 句点・接続語（したがって/よって/ゆえに）で切れる**複文**と長すぎる span（120 文字超）は引用不可

### 8-1. 引用が付かない所見の扱い — `origin` で分ける

レビュー指摘（「引用不可でも所見を出す」は結論漏れ経路になる）を受け、**所見の出自で分岐**する。

| origin | 引用の要否 | 引用不可・0 件のときの扱い |
|--------|----------|------------------------|
| `deterministic`（Stage 3 が証拠メタから機械導出） | **不要** | そのまま出す。根拠は `evidence_ids` と `params` の構造化値であり、本文引用に依存しない |
| `llm`（Stage 4 が本文から構成） | **必須（1 件以上）** | **observation に昇格させない。** `unresolved_questions` に `question_code="nonquotable_source_requires_human_review"` + 該当 `evidence_ids` を立てて降格する |

これにより、**根拠のない LLM 所見が裁定を述べる経路が消える**一方、証拠メタから機械的に導ける
所見（「証拠 EV-001 は要求期間 2026-04〜06 のうち 2026-05 をカバーしていない」）は
引用の有無に関わらず提供でき、本テンプレートの価値の核は失われない。

Stage 4 の生 LLM DTO は `origin="llm"` を内部付与して Stage 5 へ渡す。Stage 5 は検証済みの
LLM observation に `observation_id`、`origin`、決定論所見由来または既定の `review_priority` を
付与し、契約外キーを除去してから出力する。`suggested_additional_evidence` の内部出自も `llm` と
記録するが、公開 schema には含めず、`evidence_type` が `doc_type` 列挙外なら `other` に正規化する。

---

## 9. 十分性 rubric の統治（design-entry blocker (iv)）

「実質的十分性」は LLM の主観ではなく、**顧客の規制対応部門が承認した rubric** を統制基盤とする
（レビュー指摘への対応）。

rubric は **版固定の入力**として扱う（KB へ蓄積して本エージェントが自動更新するものではない）。

### 9-1. 承認状態の検証 — 「止める」のは構造不備だけ、「未承認」は出力に刻む

> **ユーザー裁定（2026-07-30・🔴5）**: 未承認 rubric で**実行を止めない**。
> サンプル rubric で動き、**未承認であることを出力に機械可読で刻む**。
> 理由: 本テンプレートは規制上の判定を出さない（所見と論点のみ）ため、rubric は
> 「評価の観点リスト」であって「規制上の判定基準」ではない。監査要件（どの版を適用したか）は
> 出力への記録で満たせる。カタログ商品として初手で動くことにも価値がある。

**fail-closed（`status="error"`）にするのは構造不備のみ**:

| # | 検証 | 不合格時 |
|---|------|---------|
| 1 | `criteria` が 1 件以上あり、各 `id` が重複しない | `status="error"` |
| 2 | `reviewer_role` が空でない | `status="error"` |
| 3 | 入力 `rubric_version` が指定された場合、config の `version` と**完全一致**すること | `status="error"`（意図しない別版での評価を防ぐ） |

**承認状態は止めずに刻む**:

| 条件 | `rubric_approved` | `degradation_reason` | 出力 `rubric_version` |
|---|:--:|---|---|
| `approved_by` と `approved_on` が実値（プレースホルダでない・`approved_on` が ISO 8601） | `true` | 追記なし | 例 `"1.0"` |
| いずれかがプレースホルダ（`unconfigured` / `SAMPLE...` / 日付としてパース不能） | `false` | `rubric_not_customer_approved` を追記 | 例 `"1.0-SAMPLE"` |

`rubric_approved` は出力契約（§7-1）の必須キーであり、監査側は**このブール 1 つで**
「顧客承認済みの基準で評価されたか」を機械判定できる。

**scaffold 出荷時の既定はサンプル rubric**（`approved_by: "SAMPLE (not customer-approved)"`）とし、
そのままでも動くが `rubric_approved: false` が刻まれる。顧客は導入時に承認者と承認日を置換する。

### 9-2. 運用ルール

- 適用した `rubric_version` を**出力に必ず記録**する（監査は「どの版を適用したか」を問う）
- **本エージェントは rubric を書き換えない**。更新は顧客の承認プロセスを経た config 差し替えのみ
- rubric の妥当性検証は顧客規制対応部門の責務（本テンプレートの保証範囲外。`docs/07` に明記）

---

## 10. Security — 5 層の実装配置

### S-1 Trust Gate

全ノードで `required_trust_level` を**明示宣言**する（暗黙の `ANONYMOUS` 継承は不可）。

| ノード | `required_trust_level` | 理由 |
|--------|----------------------|------|
| `PreProcessNode` | `VERIFIED_EXTERNAL` | 顧客の検査案件データを受領する |
| `EvidenceReviewWorkflowGraphNode`（`GraphNode`） | `VERIFIED_EXTERNAL` | **`GraphNode` の既定は `ANONYMOUS`** — 明示しないと内側と不整合になる |
| 内側 Stage 1〜5 | `VERIFIED_EXTERNAL` | 機微証拠の解決・LLM 呼び出し |
| `PostProcessNode` | `VERIFIED_EXTERNAL` | 機微を含む出力を扱う |

有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 つのみ。
`src/api/server.py` は認証結果から `caller_trust_level` を State に載せる
（既定 `ANONYMOUS` のままだと S-1 で全拒否される）。

### S-2 Input Gate

`FunctionNode._security_gate_input()` は `@final`。**触らない**。
ドメイン検査は `_extra_security_gate_input()` で行う。

| ノード | ドメイン検査 |
|--------|------------|
| `PreProcessNode` | `case_id` 必須／内部 `input_context` の未宣言キー reject／`evidence_index` の件数・キー・正規表現検証／`title` の PII 走査／rubric の構造検証と承認状態の判定（§9-1） |
| `EvidenceReviewWorkflowGraphNode` | **上流 status のパススルー**（下記） |
| 内側 Stage 1〜5 | 上流 status のパススルー |

> 🔴 **`pre_process → main` は無条件辺である**（framework 仕様）。`PreProcessNode` が
> `status="error"` を返しても **`main` は必ず呼ばれる**。したがって fail-closed を成立させるには、
> `EvidenceReviewWorkflowGraphNode._extra_security_gate_input()` で
> **上流 `status == "error"`・必須 ref／`case_id` 欠落・検証未完了**のいずれかなら
> そのまま error state を返し、`execute()` を一切実行しない**短絡**を実装する。
> 内側 Stage 1〜5 にも同じ短絡規約を置く。これは MR-B の必須実装項目であり、
> 「上流 error を握りつぶさない」ことを検証するテストを `tests/integration/` に置く。

> framework 既定の PII マスクが走るのは `user_input` / `validated_input` / `llm_response` の
> 3 フィールドのみ（SDK 1.0.0 実体で確認）。**ネストした dict は走査されない。**
> 本テンプレートはこれらに本文を載せない設計のため、機微本文の保護は §6 の payload_ref 隔離が
> 主防御であり、`title` 等の自由文メタは入口の明示走査で担保する（§6）。

### S-3 Output Gate

`FunctionNode._security_gate_output()` は `@final`。**触らない**。
ドメイン検査は `_extra_security_gate_output()` で行う（§7）。

| ノード | ドメイン検査 |
|--------|------------|
| `ProvenanceVerifyNode`（Stage 5） | claim→source 実在照合・span_kind allowlist・`origin="llm"` の引用 0 件降格 |
| `PostProcessNode` | closed allowlist（**再帰**）・禁止キー再帰排除・未知 enum reject・長さ検証・**出力全体の再帰 PII/credential 走査**・`requires_human_review` の存在確認 |

#### S-3 自由文走査パス（MR-B 確定）

`PostProcessNode._FREE_TEXT_PATHS` は以下の全パスを `*`（list 展開）付きで走査する。列挙外の
自由文を追加する場合は、この表と実装を同時に更新する。各パスで PII と credential を検出した場合は
マスクせず reject する。

| path |
|---|
| `("observations", "*", "observation")` |
| `("suggested_additional_evidence", "*", "rationale")` |
| `("requirement_segments", "*", "summary")` |
| `("observations", "*", "source_spans", "*", "text")` |
| `("version_conflict_observations", "*", "observation")` |
| `("version_conflict_observations", "*", "source_spans", "*", "text")` |
| `("suggested_additional_evidence", "*", "source_spans", "*", "text")` |

> `_extra_security_gate_output(result)` は **`result: dict` のみを受け取り `state` を受け取らない**
> （SDK 実体で確認）。分岐に必要な値は `execute()` が `formatted_output["mode"]` /
> `formatted_output["evaluation_state"]` として**宣言**し、ゲートはその値で分岐する。
> キーの有無で分岐する fail-open 実装にしない。宣言が欠落していたら `raise`。

### S-4 Audit Logging

`node_start` / `node_complete` / `node_error` は framework が `__call__()` で自動発行する。
**`execute()` 内で重複させない。**

各ノードの `execute()` 内で `emit_trace_event(event_type, payload, state)` を**最低 1 回**呼ぶ
（`from shared.utils.audit_logger import emit_trace_event`）。副作用の有無に関わらず必須。

| ノード | ドメインイベント |
|--------|----------------|
| `PreProcessNode` | `exam_case_scope_resolved` |
| Stage 1 | `evidence_register_normalized` |
| Stage 2 | `requirement_spec_extracted` |
| Stage 3 | `coverage_matched` |
| Stage 4 | `sufficiency_narrative_drafted` |
| Stage 5 | `evidence_provenance_verified` |
| `PostProcessNode` | `review_packet_emitted` |
| payload 解決時（Stage 1/4/5） | `evidence_payload_resolved` |

**成功経路だけに監査を置かない**（他テンプレートのレビューで繰り返し指摘されている型）。以下も必ず emit する:

| 事象 | イベント |
|------|---------|
| 入力品質ゲートでの reject（adapter） | `input_quality_rejected`（`reason_code` 付き） |
| 上流 error による短絡（main / 内側 Stage） | `upstream_error_short_circuited` |
| observation の降格（引用 0 件・params 不一致・捏造 ID） | `observation_demoted`（`demotion_reason` 付き） |
| rubric 未承認での続行 | `rubric_not_customer_approved` |
| 縮退（LLM 不在・payload 解決不能） | `evaluation_degraded`（`degradation_reason` 付き） |

### S-5 / secrets

- **`requires.secrets` は空**。LLM は `config["llm"]` 注入シームで受け取り、テンプレート内で
  API キーを解決しない（モデル ID・具体クライアントを `src/` に書かない）
- `os.environ` でシークレットを読まない。唯一の例外は entry-point の `INVOKE_AUTH_TOKEN`
  （`src/api/server.py`。`InvocationContext` 成立前の呼び出し元認証にあたるため、secrets 経由ではなく環境変数から読む）
- credential リテラルを `src/` に置かない（テスト用モック値は `tests/` に置く）

> `requires.secrets` は「**起動時に 1 つだけ存在すべき秘密**」の宣言欄である。リクエスト時に
> 要る鍵をここに載せると起動不能になる（InsuranceLargeLossReserveChangeEvidenceReviewAgent 実例）。本テンプレートは起動時必須の
> 秘密を持たない。

### Import Isolation

- [x] Level 0（`agenticstar`）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` は不要）
- [x] 他テンプレートを import しない
- [x] 写経部品の import identity は `src.<part>` に統一する

---

## 11. 縮退動作と既知の限界

### 11-1. `evaluation_state` と必須フィールド

| `evaluation_state` | 発生条件 | `observations` | `unresolved_questions` | `status` |
|---|---|---|---|---|
| `completed` | 全 Stage 実行 | 決定論 + LLM | あり得る | `success` |
| `deterministic_only` | `config["llm"]` 未注入、または LLM 呼び出し失敗 | **決定論のみ**（`origin="deterministic"`） | 抽出不能な**軸ごと**に `requirement_period_not_extractable` / `requirement_population_not_extractable` / `requirement_approval_authority_not_extractable` | `success` + `degradation_reason` |
| `not_performed` | `evidence_register` が空、または要求セグメントが 0 件 | **空配列** | `evidence_content_unavailable` 等の固定コード | `success` + `degradation_reason` |

`deterministic_only` / `not_performed` でも `formatted_output` の必須キー
（`contract_version` / `mode` / `case_id` / `rubric_version` / `rubric_approved` / `evaluation_state` /
`requires_human_review`）は必ず埋める。

### 11-2. その他の縮退

| 事象 | 挙動 |
|------|------|
| 証拠に `content_payload_ref` が**そもそも無い**（`content_text` 未提供） | **正常系**。当該証拠は構造化メタのみで Stage 3 の決定論照合に参加する。解決を試みない。`degradation_reason` は付けない（縮退ではなく想定内の入力） |
| `content_payload_ref` は**あるが解決不能**（TTL 切れ・store 再起動等） | **異常系**。当該証拠は構造化メタのみで評価し、`degradation_reason` に追記して `question_code="evidence_content_unavailable"` を立てる |
| 全証拠に本文が無い | Stage 4 は所見を生成できず（引用が付かず §8-1 で降格）、`evaluation_state="deterministic_only"` |
| 引用可能な span が得られない（`origin="llm"`） | observation に昇格させず降格（§8-1） |
| `case_id` 欠落 / rubric の構造不備（criteria 空・版不一致） | **fail-closed**。`status="error"`（main は §10 S-2 の短絡で実行されない） |
| rubric が顧客未承認（サンプルのまま） | **止めない**。`rubric_approved=false` + `degradation_reason="rubric_not_customer_approved"` を刻んで続行（§9-1） |

### 11-3. 既知の限界（`docs/07_operation_guide.md` に転記する）

- 証拠 register を構造化保有していない顧客では、当てはめ評価の価値が一般 checklist に縮退する
- 本文がプレーンテキストで供給されない場合（PDF のみ等）は本テンプレートの対象外
- `origin="llm"` の `observation` / `rationale` は自由文であり、断定的な言い回しの残余リスクがある
  （§7-2 の残余リスク）。保証ではなく緩和である
- `detect_pii` は日本語氏名を検知しない既知欠陥がある（FW 側の課題。テンプレート側で自前の
  日本語補完サニタイザは実装しない — 正典 §2-4 既定）。本テンプレートは payload_ref 隔離により
  氏名検知に依存しない防御を持つ
- LLM 注入シーム・payload store は in-memory 実装であり、**プロセス再起動で全 ref が失効**する。
  実バックエンドとの接続は scaffold / CI が実接続手段を提供した時点で解消する（`docs/07` に明記）

### 11-4. PB-5 checkpoint safety の実施方法（MR-B 確定）

`memory_enabled: false` のため本テンプレートは checkpointer を持たない。PB-5 は `invoke` 後に
State 相当の出力を JSON デシリアライズし、`request_text` / `content_text` の本文および credential
風キーが含まれないことを構造で検査する。`repr()` や msgpack バイト列の検索は使わない。
検査は本文を意図的に State に混入させた mutation で失敗することも確認し、空のチェックによる
vacuous pass を防ぐ。

---

## 12. 適用部品一覧（`parts/`）

| part-id | version | 適用先 | adaptation_points の埋め方 |
|---------|---------|--------|--------------------------|
| `cat2-graph-skeleton` | 0.1.3 | `src/nodes/main_node.py` | `subgraph_factory` に `EvidenceReviewWorkflowGraph` を渡す／`mode_specs` は `invoke` 単一モード／`propagate_hitl=False`／`error_strategy="propagate"`／上流 error 短絡（§10 S-2）を確認 |
| `output-envelope` | 0.1.2 | `src/nodes/post_process_node.py` | `format_invoke` で §7-1 のキーを構成／`required_fields_by_mode` に `contract_version`/`mode`/`case_id`/`rubric_version`/`rubric_approved`/`evaluation_state`/`requires_human_review` を必須指定／`_sanitize_client_echoes` で再帰 PII 走査 |
| `evidence-gate` | 0.1.0 | `src/nodes/inner/stage5_provenance_node.py` | `cited_ids_key="evidence_ids"`／`retrieved_records_key="evidence_index"`／`evidence_map_key="observations"`／`record_id_keys=("evidence_id",)` |
| `llm-draft-stage` | 0.1.0 | `src/nodes/inner/stage2_requirement_spec_node.py` / `stage4_narrative_node.py` | `config["llm"]` 注入シーム／`build_prompt` はドメイン固有／LLM 不在時 deterministic fallback |
| `payload-store` | 0.4.0 | `src/services/payload_store.py`（写経） | `scope_keys=("case_id",)`／`ttl_seconds=300`／invocation 内複数 read 可（§6） |
| `entry-adapter` | 0.3.0 | `src/api/server.py` | `SCOPE_KEYS=("case_id",)`／`require_verified_trust`／standalone token は **`/invoke` のみ**に開く |

部品がカバーする骨格は再発明しない。適用は `parts/tools/apply_part.py`、検収は `verify_part.py`。
写経部品（`# PART:` マーカー付き）に lint 違反が出た場合は**上流 `parts/` master を先に見る**
（byte-identity の維持が最優先）。

---

## 13. 設定値の所有者・検証・注入経路

SDK は `max_retry` / `memory_enabled` 等しか標準検証しない。**下表以外の値は自動では効かない**ため、
検証箇所と注入経路を明示する。

| 設定 | 所有 | 検証箇所 | 効かせ方 |
|------|------|---------|---------|
| `max_retry` / `memory_enabled` | framework | `BaseGraph._validate_config()` | 標準 |
| `timeout_seconds` | テンプレート | `PreProcessNode` | LLM 呼び出しに `timeout` として渡す（`src/services/llm_client.py`） |
| `max_evidence_entries` / `max_requirement_segments` | テンプレート | adapter（400）+ `PreProcessNode`（error） | 二重で検証（adapter 経由でない呼び出しに備える） |
| `payload_ttl_seconds` | テンプレート | `payload_store` 構築時 | コンストラクタ引数 |
| `max_quotable_span_chars` / `min_quotable_span_chars` | テンプレート | Stage 5 / post_process | span 長の上下限検証（下限が無いと逐語照合が vacuous） |
| `max_text_chars` | テンプレート | adapter（400） | 入力品質ゲート（§3-4） |
| `sufficiency_rubric` | 顧客 | `PreProcessNode`（§9-1: 構造不備のみ fail-closed） | `rubric_version` + `rubric_approved` を出力に記録 |
| `llm` | 呼び出し側 | `EvidenceReviewWorkflowGraph._validate_config()` | `config["llm"]`。**契約**: `complete(messages: list[dict]) -> dict`（`content` キーを持つ）。未注入は縮退（§11） |

```yaml
agent:
  id: "FIN-C2-188"
  name: "ExamEvidenceSufficiencyReviewAgent"
  version: "0.1.0"
  category: "Cat 2"
  industry: "FIN"
  base_type: "AgentBaseGraph"          # L1 クラス名（Level 2 型名ではない）
  module: "src.graph.graph"
  class: "ExamEvidenceSufficiencyReviewGraph"
  required_trust_level: "VERIFIED_EXTERNAL"
  requires:
    extras: []
    secrets: []                        # 起動時必須の秘密なし（LLM は config 注入）
  config:
    max_retry: 3
    timeout_seconds: 60
    memory_enabled: false              # HITL 非採用のため checkpointer 不要
    max_evidence_entries: 200
    max_requirement_segments: 50
    payload_ttl_seconds: 300
    max_quotable_span_chars: 120
    min_quotable_span_chars: 8
    max_text_chars: 200000
  sufficiency_rubric:
    rubric_id: "fsa-exam-evidence-v1"
    version: "1.0"
    approved_by: "SAMPLE (not customer-approved)"
    approved_on: "SAMPLE"
    reviewer_role: "規制対応責任者"
    escalation_path: "所見に不同意の場合は規制対応責任者が判断する"
    criteria:
      - id: "period_coverage"
        label: "要求された対象期間を証拠がカバーしているか"
```

> `hitl` キーは宣言しない（HITL 非採用 — `docs/01_proposal.md` HITL Gate 参照）。
> PB-7 は「auto-waived — non-HITL」として scaffold stub のまま保持する（exact-name 必須・
> 中身は auto-skip）。

### 13-1. MR-B の必須成果物（設計上の約束）

| # | 項目 | 理由 |
|---|------|------|
| 1 | `src/graph/graph.py` のクラス名を `ExamEvidenceSufficiencyReviewGraph` に揃える | manifest（`config/agent.yaml`）との一致 |
| 2 | `src/api/server.py` が **manifest 由来の config を読んでグラフを構築**する | 現 scaffold は `Graph()` を直生成し、rubric・上限・trust 宣言を読まない |
| 3 | `startup()` を実行する統合テスト（`with TestClient(app):`）を 1 本以上置く | `run-tests` は `pytest tests/` だけで startup hook を一度も起動しない。本番起動 blocker が全ゲート緑をすり抜ける |
| 4 | 匿名呼び出し（`ANONYMOUS`）が S-1 で拒否されることのテスト | 既定 trust のままだと全ノードが素通り／全拒否のどちらかに倒れる |
| 5 | 上流 `status="error"` で内側 Stage が実行されないことのテスト | `pre_process → main` 無条件辺（§10 S-2） |
| 6 | `pyproject.toml` の per-file-ignores から `src/graph/graph.py` と `tests/unit/test_main_node.py` を削除 | 実装置換で違反が解消するため |
| 7 | fail-closed テストに **provisioned 対照実験**を添える | `pytest.raises` だけだと state 形不足の `KeyError` で緑になり境界を何も検証しない |
| 8 | PB に「**実在するが無関係な `evidence_id` を引用**」ケースを入れる | ID 実在チェックだけでは、無関係な証拠を引いて数値は本文に書く回避を止められない |
| 9 | `observation_params` の**双方向照合**（params→決定論結果 / 本文→params）のテスト。本文に params 外の数値を混ぜた入力で降格することを確認 | §7-3-1。ID 照合・span 照合のどちらも発火しない経路 |
| 10 | span の**長さ下限 8 文字**が効くことのテスト（空文字・1 文字 span を投入して reject を確認） | 下限が無いと逐語照合が vacuous になる |
| 11 | 入力品質ゲートの**変異検査**（`%PDF-` 始まり・U+FFFD 混入・制御文字混入をそれぞれ投入し `reason_code` を確認） | 「ゲートを書いたが実は素通り」を防ぐ |
| 12 | **§4-B-5 の handoff negative test 4 本**（`draft_response` 注入 reject／外部 DTO への `response_draft` 400／`contract_version` 欠落・改変 reject／`grep -r "RemoteAgentNode\|Bank Regulatory Examination Request Response Drafting Agent" src/` が空） | design-entry blocker (ii) の機械的強制 |
| 13 | **`request_text` 必須／`content_text` 任意**の受入テスト（①`request_text` 欠落 → 400 ②全証拠に `content_text` 無し → `status="success"` かつ `evaluation_state="deterministic_only"` かつ `degradation_reason` に `evidence_content_unavailable` を**付けない** ③`content_payload_ref` 解決不能 → `degradation_reason` を**付ける**） | §3-3 / §11-2。「ref が無い」と「ref が解決できない」の混同を防ぐ |

---

## 14. SDK 1.0.0 実体確認記録（MR-A 前スパイク・2026-07-30 実施）

`.venv` に `agenticstar-agentcore==1.0.0` を install し introspection で確認した。
**設計はこの実体に合わせてある**（推測で書いていない）。

| 確認項目 | 実体 | 設計への反映 |
|---|---|---|
| `InvocationContext` のフィールド | `correlation_id` / `session_id` / `thread_id` / `parent_trace_id` / `caller_id` / `caller_trust_level` / `secrets` / `hitl_allowed`。**`tenant_id` は存在しない** | 案件スコープ（`case_id`）は **State 経由**で扱う |
| `InvocationContext.from_state()` | **State のフィールドから再構成**する（`correlation_id`/`session_id`/`thread_id`/`trace_id`/`caller_id`/`caller_trust_level`/`hitl_allowed`）。`config["configurable"]` は読まない | §5-3 のとおり。State に独自の ctx フィールドを足さない |
| `GraphNode.execute()` | `subgraph.invoke(user_input, session_id=ctx.session_id, ctx=ctx)` のみ。**`input_context` も親 State も内側へ渡らない** | §4-A-1 の JSON 直列化受渡しが必須 |
| `AgentBaseGraph.get_output()` | `formatted_output` or `result` を `output` キーに載せる。**カスタム State 欄は透過しない** | 内側は `get_output()` を override（§4-A-2）。外側は post_process で `formatted_output` へ畳み込む |
| `BaseGraph`（内側の親） | `register_nodes` / `add_edges` / `route` / `get_output` を自前で持てる（固定 3 スロット制約なし） | 内側 5 ステージ構成が可能（§2-2） |
| `FunctionNode` の PII スキャン対象 | `('user_input', 'validated_input', 'llm_response')` の 3 フィールドのみ。**ネスト dict は走査しない** | 機微本文は payload_ref 隔離を主防御に。`title` は入口で明示走査（§6） |
| `detect_pii` / `detect_credentials` の所在 | **`shared.security.pii_detector` / `shared.security.credential_detector`**（`framework.security` は wheel に存在しない） | 自前の正規表現を書かない（フレームワークが持つ検出器を再実装しない） |
| `detect_pii` の UUID 誤検知（実測） | ダッシュ区切り UUID: **107/20,000 = 0.535%** が `phone_jp`/`my_number_jp` として誤検知。ダッシュ無し hex: **0/20,000** | §7-3 の ID 形式制約 |
| `shared.services.llm` | **公開シンボルなし（空）** | LLM は `config["llm"]` 注入シーム |
| `shared.services` の内訳 | `database` / `embedding` / `llm` / `memory` / `vector_store`（いずれも実質スタブ） | 外部 DB / VectorStore に依存する設計にしない |
| `GraphNode` の abstract | `get_subgraph` / `extract_input` / `merge_output` の 3 つ。既定 `error_strategy="propagate"` / `propagate_hitl=False` / **`required_trust_level=ANONYMOUS`** | 3 メソッドすべて実装。trust を明示宣言（§10 S-1） |
| `_extra_security_gate_output` | 引数は `result: dict` のみ（`state` を受け取らない） | 分岐値は `formatted_output` に宣言（§10 S-3） |
| `AgentStatus` の値 | `pending` / `success` / `retry` / `error` / `timeout` / `awaiting_human` / `cancelled`（小文字） | `AgentStatus.<X>.value` を返す（bare Enum を返さない） |
| `TrustLevel` の値 | `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL`（文字列 enum・int 比較不可） | §10 S-1 のとおり |
| `emit_trace_event` | `(event_type: str, payload: dict, state: dict) -> None` | 各ノードで 1 回以上（§10 S-4） |
| `route()`（外側） | `status` を読み `success→post_process` / `retry→pre_process` / その他 `finalize` | override しない |
| `pre_process → main` の辺 | **無条件**（status に関わらず main が呼ばれる） | §10 S-2 の短絡が fail-closed の要 |

---

## 15. Design Decision Record（ユーザビリティ観点のレビュー結果を含む）

> **本節が設計判断の記録の正本。** MR-A 作成後に人間レビュアーへ提示した判断項目（🔴 6 件）の
> 確定結果もここに畳み込んである（「2026-07-30 ユーザー裁定」と記した行）。提示そのものは
> チャットで全文行い、確定結果は MR コメントにも投稿している。

| # | 論点 | 選択肢 | 採用 | 理由 |
|---|------|--------|------|------|
| 1 | L1 base type | `AgentBaseGraph` / `AutonomousBaseGraph` | **`AgentBaseGraph`** | ステージ固定パイプラインであり、次の行動を自ら決める think-act ループを持たない |
| 2 | 内側グラフの親 | `AgentBaseGraph`（3 スロット） / `BaseGraph`（自由構成） | **`BaseGraph`** | 5 ステージを 3 スロットへ詰め込むより、責務どおりのノード分割の方が単体テスト可能で読みやすい |
| 3 | Stage 順序 | 決定論照合を先 / LLM 抽出を先 | **LLM 抽出（Stage 2）→ 決定論照合（Stage 3）** | 「要求期間に対するカバレッジ欠落」は要求期間が抽出されて初めて算出できる（レビュー High 指摘） |
| 4 | 要求のセグメント化の位置 | pre_process / 内側 Stage 1 | **内側 Stage 1**（提案 §4 からの意図的逸脱） | 要求本文を State に載せない設計にしたため、本文を一時解決する Stage 1 で行うのが唯一整合する |
| 5 | version conflict の「回答影響度」 | LLM に重大度を書かせる / 決定論導出 | **決定論導出**（`review_priority`・§7-4） | 重大度の断定は規制判定に接近する。並べ替え情報として決定論で出せば、非断定を保ちつつ実用価値を落とさない |
| 6 | 出力フィールド名 | `insufficiency_reasons` 等の結論語彙 / observation 語彙 | **observation 語彙** | 「不足理由」は結論を含意する。所見にとどめる方針に合わせた |
| 7 | 断定語の denylist | 実装する / 実装しない | **実装しない**（§7-2） | 言い換えで原理的に閉じず false assurance になる。非結論性は §7-2 の 8 機構で構造的に担保する |
| 8 | 自由文の縮小度 | 完全構造化（LLM に文章を書かせない） / **`observation_kind` 列挙 + question_code 化** / 現状の自由文 | **中間案（列挙 + code 化）** — 2026-07-30 ユーザー裁定 | 完全構造化は CTDClinicalSummaryAgent で過剰設計と判定され巻き戻された前例がある。一方、無制約の自由文はレビュー指摘のとおり結論を通す。所見の**型**を閉じ、論点を code 化する中間で止める。**型の範囲内での断定的言い回しという残余リスクはユーザー裁定で受容**（§7-2） |
| 8-b | rubric 未承認時の挙動 | fail-closed で拒否 / **サンプルで動かし出力に刻む** | **サンプルで動かし `rubric_approved=false` を刻む** — 2026-07-30 ユーザー裁定 | 本テンプレートは規制上の判定を出さないため rubric は「評価の観点リスト」であり「判定基準」ではない。監査要件（どの版を適用したか）は出力への記録で満たせる。カタログ商品として初手で動くことにも価値がある。構造不備（criteria 空・版不一致）は引き続き fail-closed（§9-1） |
| 9 | span_kind allowlist | 採用 / 非採用 | **採用**（§8） | 証跡側の裁定文が正しい引用を通じて漏れる経路を塞ぐ |
| 10 | 引用が付かない所見 | 全部出す / 全部降格 / **origin で分ける** | **origin で分ける**（§8-1） | 決定論所見は証拠メタから機械導出でき引用不要。LLM 所見で引用が付かないものは降格する。価値の核を残しつつ結論漏れ経路を塞ぐ |
| 11 | HITL | 採用 / 非採用 | **非採用**（`docs/01_proposal.md` HITL Gate） | 4 判定すべて非該当。PB-7・`/resume` 認証・checkpointer 2 系統という事故多発箇所を構造的に回避できる |
| 12 | 蓄積（KB 永続化） | 持つ / 持たない | **持たない** | 提案でも蓄積フライホイールは核ではないと明記。証拠 register の正本は顧客が保有 |
| 13 | ユーザビリティ: **証拠**本文（`content_text`）の要否 | 必須 / **任意** | **任意**（検査要求の本文 `request_text` は必須。両者を混同しない — §3-3 の注記） | 証拠本文が無くても構造化メタ（`coverage`）だけで Stage 3 の決定論照合は成立し `evaluation_state="deterministic_only"` を返せる。導入初期の顧客が「全証拠を全文テキスト化してから」でないと使えない状態を避ける。一方 `request_text` を任意にすると要求セグメント化・`RequirementSpec` 抽出が成立せず本テンプレートの価値自体が消えるため、こちらは必須で固定する |
| 14 | ユーザビリティ: 出力の使いやすさ | 単一リスト / 所見と論点を分離 | **分離 + `review_priority` で並べ替え可能に** | 受け取った担当者が「どれから確認するか」「誰に聞くか」に直結する形にした |
| 15 | ユーザビリティ: 所有者情報 | 氏名を扱う / 役割 ID のみ | **`owner_id`（役割 ID）のみ** | 個人情報を持ち込まずに「誰に確認すべきか」は成立する |

---

## 15-bis. 共通部品リフレッシュの判断記録（2026-08-14）

`parts/` の最新版へ追随し、併せて scaffold 追随（新 CI 4 ゲート）と manifest の
AgentRegistry 準拠移行を実施した回の判断。

| # | 論点 | 判断 | 反映先 |
|---|---|---|---|
| R-1 | 部品の版上げ | `cat2-graph-skeleton` 0.1.3→0.1.4 / `entry-adapter` 0.3.0→**0.4.0（BREAKING）** / `evidence-gate` 0.1.0→**0.3.0（BREAKING）** / `llm-draft-stage` 0.1.0→**0.2.0（BREAKING）**。`output-envelope` 0.1.2 と `payload-store` 0.4.0 は既に最新のため無変更 | `src/` の各部品ディレクトリ（`verify_part.py` SUSPECT 0） |
| R-2 | entry-adapter 0.4.0 の operation | **`Operation.READ_INVOKE` で確定**。この AG は `/invoke` 単一・mode 分岐なし・**恒久的な業務状態を作らない**（PayloadStore は短命な一時 vault で、invoke 完了時に `delete` する）。endpoint 名ではなく実際の副作用で決めるという原則に照らして READ で足りる | `src/api/server.py` |
| 🔴 R-3 | `SCOPE_KEYS` の宣言 | **`("case_id",)` を維持**（ユーザー裁定 2026-08-14）。`case_id` は顧客区画キーではなく**リクエスト内で payload を束ねる隔離ラベル**である、という位置づけを `/invoke` の docstring に明記する。`verify_scope` seam は client 申告の `case_id` をそのまま返すが、この AG は永続化も KB 検索も持たず、`payload_ref` は uuid で応答にも出ないため他者の payload を引く経路がない | `src/api/server.py`（seam と docstring） |
| R-4 | manifest の形 | root-level キー + 単一 dotted `class` へ移行し、runtime パラメータ 9 キーと `sufficiency_rubric` を `config/config.yaml` へ移設（値・キー名とも不変） | `config/agent.yaml` / `config/config.yaml` |
| R-5 | 実 LLM 1 回転で見つかった契約違反 | **`llm.complete(messages, timeout=...)` を契約どおり `complete(messages)` に是正**。`BaseLLM` は `timeout` を取らないため、準拠クライアントでは Stage 2 / Stage 3 とも必ず `TypeError` になっていた。**リフレッシュ起因ではなく `origin/develop` に元から存在**。テストの fake が本番契約と違う形（`complete(self, messages, timeout)`）だったため全テスト緑で潜在していた | `src/nodes/inner/stages.py`・fake 3 箇所・`docs/03_test_spec.md` 実 LLM 2 回目 |

---

### 15-ter. 独立レビュー R1（2026-08-14・部品リフレッシュの push 後）

著者とは別のセッションで敵対レビューした結果。**High 0 / Medium 2 / Low 3**。

| 深刻度 | 指摘 | 対応 |
|---|---|---|
| **Medium-1** | 提案（`suggested_additional_evidence`）の採用条件が `reason in {"", "invalid_source_span"}` になっており、**LLM が `span.text` に原文を入れて offset を外すだけで、検証を通っていない原文が顧客出力へ到達する**（実験で到達を確認）。`origin/develop` に元からあり、リフレッシュ起因ではない | **受入** — 採用条件を `reason == ""` に限定。回帰テスト `test_suggestion_with_unverified_span_does_not_leak_raw_text_into_output` を追加し、**変異検査**（許容条件を戻すと赤）で証明力を確認 |
| **Medium-2** | `timeout_seconds` が誰にも読まれず、**LLM 呼び出しに実質的なタイムアウトが無い**。async endpoint 内で同期 graph を実行するため、停止した provider はイベントループごと塞ぐ | **一部受入（文書化で対応・コード対処は別 Issue）** — `timeout=` を `complete()` に戻すのは `BaseLLM` 契約違反なので不可。期限は**注入する LLM クライアントの構築時**に設定し、ASGI/proxy 側にも request timeout を置く必要がある。`docs/07` の「LLM call timeout: 60」は事実でなかったため、宣言のみである旨と運用上の必須設定に書き換えた |
| Low-1 | Stage 4 の error 契約が未テスト（Stage 4 だけ旧「provider 例外→縮退 success」に戻す変異で 257 passed） | **未対応（次回）** — Stage 2 側は `test_part_refresh_contracts.py` が守っている |
| Low-2 | manifest 移行後も `docs/07` / `docs/0A` / `README.md` が runtime 設定を `config/agent.yaml` と案内 | **受入** — `config/config.yaml` に統一 |
| Low-3 | `git diff --check` が EOF の余分な空行で失敗 | **受入** — 削除 |

**指摘なし（レビュアーが裏取りした観点）**: `verify_scope` seam の scope（攻撃シナリオで被害者 ref の解決不可を確認・外部 DTO は `extra="forbid"`・ref は 128bit 新規発行で応答に出ない）／`Operation.READ_INVOKE` の妥当性（`put`/`delete` は invocation 中の一時 vault 操作で恒久的業務状態を作らない）／LLM error 契約（`llm_not_configured` のみ縮退 success・provider 例外は `status=error`）／manifest 移行の完全性（旧 9 runtime キー + `sufficiency_rubric` が値まで完全一致・`["agent"]` を読む production コードなし）。

---

## 16. レビュー指摘の反映記録

### 16-1. 既知の指摘パターンとの照合（設計起草時）

他テンプレートの設計レビューで実際に返った 6 点を、あらかじめ本設計に当てた結果。**「該当なし」も記録する**（照合したのか飛ばしたのか後から分かるように）。

| # | 先回り項目 | 照合結果 | 対処 |
|---|---|---|---|
| 1 | provenance を ID 実在で終わらせない | ⚠️ **穴あり** — `source_spans` は証拠原文からの逐語引用しか照合せず、`observation` 本文に LLM が書いた数値（欠落月など）が Stage 3 の決定論結果と食い違っても発火しない | §7-3-1 を新設。`observation_params`（kind ごとに固定キー・値は決定論結果に実在するもののみ）を必須にし**双方向照合**。本文抽出は NFKC + 漢数字 + 全角数字を正規化してから |
| 1-b | 逐語引用の長さ下限 | ⚠️ 未対応だった — 空文字・1 文字 span は `text in source` が常に真で照合が vacuous | `min_quotable_span_chars: 8`（§7-1 / config） |
| 2 | 永続 KB の統治契約 | ✅ **該当なし** | 永続層を持たない（§15 DDR #12） |
| 3 | PoC 検証計画 | — **適用しない** | 当プロジェクトは PoC 不実施が確定ルール |
| 4 | §1 継承欄は継承クラスのみ | ⚠️ §1 に "Inner graph" を併記していた | §1 から外し「`GraphNode`／内側グラフは継承ではなく構成」と注記して §2／§4 へ |
| 5 | 外部ソース確認は実施日を明記 | ✅ | §14 に 2026-07-30。外部 API 依存は無い（顧客保有データのみ） |
| 6 | 入力 allowlist + 品質ゲート | ⚠️ 「パーサを持たない選択」のみで**マジックバイト判定と品質ゲートが無かった** | §3-4 に 5 項目（マジックバイト・U+FFFD 比率・制御文字比率・可読文字比率・長さ）＋ `rejected[]` での理由返却 |

**併せて対処した頻出のレビュー所見型**: 「S-4 監査が成功経路のみ」→ §10 S-4 に**失敗・降格経路の
イベント 5 種**を追加。MR-B 受入条件に PB ケースを 4 件追加（§13-1 #8〜#11）。

### 16-2. 敵対レビュー（2026-07-30）の反映記録

| 指摘 | 深刻度 | 裁定 | 反映先 |
|------|:--:|------|-------|
| `GraphNode` が `input_context`／親 State を内側へ渡さない | High | **受入** | §4（受渡し契約を新設・内側 `get_output()` override） |
| payload_ref 入口契約の自己矛盾（本文を受けるフィールドが無い） | High | **受入** | §3-1/§3-2（外部 DTO と内部 input_context を分離。クライアントは ref を送れない） |
| `resolve` 主体と single-use の矛盾（Stage 4 消費後に Stage 5 が検証不能） | High | **受入** | §6（解決許可主体を Stage 1/4/5 に明記・single-use は invocation 間の禁止と定義） |
| Stage 1 でカバレッジ欠落を決定論算出できない | High | **受入** | §2-2（5 ステージへ組替え・LLM 抽出 → 決定論照合の順） |
| closed allowlist が自由文経由の結論を閉じない | High | **部分受入** | §7-1/§7-2（`observation_kind` 列挙必須・`question_code` 化・残余リスクを明記）。**完全構造化描画は CTDClinicalSummaryAgent の巻き戻し前例により不採用**。2026-07-30 ユーザー裁定で中間案を採用し、**残余リスクは受容**（後続レビューで同点を指摘されても設計変更しない） |
| 「引用不可でも所見を出す」が結論漏れ経路 | High | **部分受入** | §8-1（`origin` で分岐。LLM 所見で引用 0 件は降格。決定論所見は引用不要） |
| `title` 等の自由文が PII 隔離を抜ける | High | **受入** | §3-3/§6（120 文字上限 + 入口 `detect_pii` 明示適用）・§10 S-3（出力全体の再帰走査） |
| `pre_process → main` 無条件辺で fail-closed が効かない | High | **受入** | §10 S-2（`_extra_security_gate_input()` の短絡を必須実装として明記・テスト required） |
| `accountable_owners` / `owner_directory` / `response_checklist` のデータフロー欠落 | Medium | **受入** | §5-2（join 規則・未登録 owner は reject・checklist は十分性判定に使わない） |
| nested schema が実装可能な粒度でない | Medium | **受入** | §7-1（`source_spans` のオフセット体系・enum・長さを全定義） |
| 縮退時の出力契約が §7 に無い | Medium | **受入** | §11-1（`evaluation_state` enum を出力契約へ追加・必須フィールド表） |
| rubric の承認・版選択が fail-closed でない | Medium | **部分受入** | §9-1。構造不備（criteria 空・`reviewer_role` 空・版不一致）は fail-closed にした。**未承認での実行拒否は 2026-07-30 ユーザー裁定で不採用** — サンプルで動かし `rubric_approved=false` + `degradation_reason` + `-SAMPLE` 版表記で刻む（監査は `rubric_approved` 1 値で機械判定できる） |
| manifest class 不一致・server が manifest を読まない | Medium | **受入** | §13-1（MR-B 必須成果物 1〜4） |
| `InvocationContext` の取得方法の記述が SDK 実体と違う | Medium | **受入** | §5-3・§14（実機で `from_state` のソースを確認して訂正） |
| config 値の注入経路が未定義 | Medium | **受入** | §13（所有者・検証箇所・注入方法の表） |
| UUID を PII マスクが破壊するのは誤り | Medium | **棄却（記述は精密化）** | 実測 20,000 件でダッシュ区切り UUID は **0.535% 誤検知**（他テンプレートの実測 0.58% と一致）。レビューでの反証は試行数不足と判断。ただし hex は 0% のため §7-3 を「ダッシュ区切り UUID を禁止・hex は可」に精密化 |
| 「検査回答上重大」等の結論語彙 | Medium | **部分受入** | README・docs の語彙を調整。承認済みの提案本文そのものの改訂は設計フェーズの範囲外とした |
| per-file-ignores を MR-B で削除・CI に ruff を追加 | Low | **受入（CI は範囲外）** | §13-1 #6。CI への ruff 追加は共通 CI 定義側の変更のため、別途の改善要望として記録 |

---

## 17. Framework Utilization チェック

- [x] `InvocationContext`（`from_state()` 経由・直接構築しない）
- [x] `SecurityViolationError`（fail-closed 経路）
- [x] S-2: `_extra_security_gate_input()`（`_security_gate_input()` は `@final` — 触らない）
- [x] S-3: `_extra_security_gate_output()`（`_security_gate_output()` は `@final` — 触らない）
- [x] S-4: `emit_trace_event()` を各 `execute()` で 1 回以上
- [x] `AgentStatus.<X>.value` を返す（bare Enum を返さない）
- [x] `BaseNode.__call__()` を override しない
- [x] ノードのインスタンス変数に可変状態を持たない（subgraph キャッシュを除く）
- [x] Level 0（`agenticstar`）を import しない
