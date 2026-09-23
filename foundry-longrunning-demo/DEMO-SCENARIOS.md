# Microsoft Foundry Agent Service 長時間実行デモ調査・実証結果

作成日: 2026-09-22

対象: Microsoft Foundry Agent Service の長時間実行、クラッシュ耐性、Human-in-the-loop、Steerable agents

検証リソースグループ: `<resource-group>`

検証リージョン: `northcentralus`

## 1. 結論

2026 年 8 月の Microsoft Foundry 更新で追加された各機能は、いずれも **Hosted Agent の preview 機能**として実装・実証可能です。

今回、Microsoft 公式の LangGraph resilient Responses サンプルを専用 Foundry project にデプロイし、以下を実エンドポイントで確認しました。

- `background=true`、`store=true` による長時間バックグラウンド実行
- SSE の sequence cursor を使った再接続
- LangGraph checkpoint と Foundry State Store による状態永続化
- hosted process の強制終了後に、同じ response を復旧
- `book_trip` 実行前の Human-in-the-loop 承認
- 実行中 turn に対する新しい指示の投入と steering

**最もデモ映えする案は「旅行計画・予約オペレーション」です。** 今回デプロイしたコードをそのまま使用でき、検索、途中経過、承認カード、予約完了、方向転換、クラッシュ復旧を短時間で連続して見せられます。

## 2. アナウンス内容の技術整理

| 機能 | 実装上の中心概念 | 今回の確認結果 |
|---|---|---|
| 長時間実行 | stored background response、durable work identity、SSE replay、cursor reconnect | 成功 |
| クラッシュ耐性 | `resilient_background=True`、永続 checkpoint、handler reinvocation | 成功 |
| Human-in-the-loop | LangGraph `interrupt()`、`mcp_approval_request` / `mcp_approval_response` | 成功 |
| Steerable agents | `steerable_conversations=True`、同一 conversation の queued turn、cooperative cancellation | 成功 |

### 2.1 長時間実行 API と状態管理

長時間実行は、通常の同期 request/response を長く待たせる方式ではありません。

1. client が response ID を安定 ID として決定する。
2. `background=true` と `store=true` で処理を開始する。
3. server は input と response event を永続化する。
4. client は切断後、同じ response ID を retrieve する。
5. `starting_after=<sequence_number>` で未受信イベントから再開する。

Responses protocol ではプラットフォームが stored response と SSE replay を提供します。Invocations protocol では、アプリ側が status、polling、streaming contract を定義します。

重要な設計ルール:

- stream ID は request/turn 単位にする。conversation ID を流用しない。
- task metadata には checkpoint 本体ではなく、小さい watermark や参照 ID だけを保存する。
- 大きい入力、生成物、会話履歴は Blob、DB、framework checkpointer へ保存する。
- reconnect 後の `response.in_progress` は snapshot reset として扱い、client 側の部分表示を snapshot で置換する。

### 2.2 クラッシュ耐性

Foundry runtime は work identity、input identity、lease、persisted input を管理します。process が停止して lease を失うと、別の process lifetime が同じ work を reclaim し、handler を再実行します。

ただし、ローカル変数や call stack が復元されるわけではありません。application は以下のいずれかを実装します。

- 安全な全体再実行
- Responses snapshot からの再開
- LangGraph / Microsoft Agent Framework checkpoint からの再開
- 小さい watermark による完了 phase の skip

副作用は at-least-once を前提にします。支払い、発注、メール送信、DB 更新には、安定した operation ID、downstream の idempotency key、または side-effect 前後の durable fence が必要です。

### 2.3 Human-in-the-loop

今回の sample は、`book_trip` を sensitive tool として分類し、LangGraph の `interrupt()` で停止します。client には `mcp_approval_request` が表示され、Approve または Deny を次 turn の `mcp_approval_response` として送信します。

承認待ちは process memory の待機ではありません。graph checkpoint が永続化されるため、数分、数時間、数日後でも同じ workflow を再開できる設計です。

### 2.4 Steerable agents

`steerable_conversations=True` を有効化すると、active turn 中に同じ conversation へ新しい input を送れます。

- 新 input は queue に入る。
- running handler は cooperative cancellation signal を受け取る。
- safe boundary で current turn を終了する。
- queued turn が conversation の次 turn として実行される。

これは並列 fork ではありません。conversation history は常に線形で、同時に 2 turn が同じ状態を書き換えない設計です。

## 3. 構築した環境

| 項目 | 値 |
|---|---|
| Resource group | `<resource-group>` |
| Foundry account | `<foundry-account>` |
| Foundry project | `<foundry-project>` |
| Region | `northcentralus` |
| Agent | `langchain-azure-resilient-responses-steerable` |
| Agent version | `<current-version>` |
| Agent status | `active` |
| Model deployments | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` |
| Model SKU | `DataZoneStandard` |
| Capacity | `300` each |
| Hosted resources | `0.5 CPU`, `1 GiB` |
| Protocol | Responses `2.0.0` |

Foundry portal と Azure portal の deployment-specific URL はローカルの運用メモで管理し、リポジトリには保存しません。

リソースとテストコードは依頼どおり削除していません。Web Search 用に DataZoneStandard capacity 300 の3モデルと Azure Container Apps を追加しています。リソースを保持するため、モデル呼び出し、Web Search、Container Apps、ACR、Log Analytics に継続課金が発生し得ます。

## 4. 実装構成

```text
foundry-longrunning-demo/
├─ azure.yaml
├─ DEMO-SCENARIOS.md
├─ feasibility-results.json
├─ client/
│  ├─ app.py                  # Textual CUI
│  ├─ conversation.py         # response ID、cursor、retry、approval、steering
│  ├─ client.py               # Azure 認証と CUI 起動
│  └─ feasibility_probe.py    # 非対話の実環境 probe
└─ src/langchain-azure-resilient-responses/
   ├─ main.py                 # LangGraph、tools、approval、crash simulation
   ├─ langgraph.json
   ├─ requirements.in
   └─ eval.yaml               # 非同期生成を投入済み
```

### Agent code

`main.py` の graph:

```text
START
  ↓
agent (Foundry model)
  ├─ search_flights / search_hotels → tools → agent
  ├─ book_trip → approval interrupt → tool execution → agent
  ├─ simulate_crash → process kill → checkpoint recovery
  └─ no tool → END
```

状態永続化には `FoundryCheckpointSaver(user_isolation=True)` を使用します。Hosted 環境では Foundry State Store、local 環境では file-backed store へ自動的に切り替わります。

### Client code

`conversation.py` は以下を実装しています。

- client-generated stable response ID
- `background=true`
- `stream=true`
- `store=true`
- SSE `sequence_number` の保存
- `starting_after` による再接続
- retryable な `5xx`、transport error、`424 session_not_ready` の再試行
- approval request の抽出
- active response に対する steering turn
- cancel API

## 5. UI 構成

今回の Textual CUI は、デモ用として十分に視認性があります。

```text
┌ Resilient Responses / LangGraph long-running agent ┐
│ Status: Streaming / Recovering / Approval required │
├─────────────────────────────────────────────────────┤
│ User / Agent transcript                             │
│ - 検索開始                                           │
│ - フライト候補                                       │
│ - ホテル候補                                         │
│ - 予約案                                             │
├─────────────────────────────────────────────────────┤
│ Sensitive tool call requires approval               │
│                            [ Approve ] [ Deny ]      │
├─────────────────────────────────────────────────────┤
│ Message composer               [ Send ] [ Cancel ]   │
└─────────────────────────────────────────────────────┘
```

表示上の特徴:

- 黒緑系の背景と高コントラスト文字
- streaming 中の status 表示
- approval 時だけ Approve / Deny を表示
- `Ctrl+R` で raw SSE events を表示
- active response 中にも composer を有効化し、steering を実演
- reconnect 中は `Recovering` を表示

Foundry portal の Playground は通常 chat の説明には適しますが、stable response ID、cursor reconnect、同時 steering、approval control を一度に見せるには、この専用 CUI の方が適しています。

## 6. 実環境 feasibility check

実行コード:

```powershell
python .\client\feasibility_probe.py `
  --url (azd env get-value AGENT_LANGCHAIN_AZURE_RESILIENT_RESPONSES_STEERABLE_RESPONSES_ENDPOINT) `
  --out .\feasibility-results.json `
  --timeout 360
```

### 6.1 Human-in-the-loop

結果: **成功**

- Paris 2 泊の出張予約を依頼
- agent が flight と hotel を検索
- `book_trip` の approval request を生成
- Approve を送信
- 予約 confirmation `TRIP-4E3D5B27F357` を生成

確認した approval arguments:

```json
{
  "city": "Paris",
  "nights": 2,
  "flight": "Paris Express AA123",
  "hotel": "Paris Grand Hotel"
}
```

### 6.2 Steering

結果: **成功**

- 5 都市の詳細な日本旅行を生成中に、新しい turn を送信
- server metadata で steerable conversation を確認
- 「京都 2 日間だけへ変更」という指示を active turn 中に投入
- queued turn が完了し、京都だけの旅程へ方向転換

### 6.3 Crash recovery と reconnect

結果: **成功**

- agent に `simulate_crash` tool の実行を指示
- hosted process が強制終了
- client は同じ response ID と最後の SSE cursor を保持
- `recovering` state を 3 回観測
- restarted host が LangGraph checkpoint から pending tool call を再開
- 最終 response は `completed`
- 出力で `Crash recovery succeeded` を確認

### 6.4 Probe 全体

| Check | Result |
|---|---|
| Human-in-the-loop | PASS |
| Steering | PASS |
| Crash recovery | PASS |
| Reconnect | PASS |
| Total duration | 144.38 秒 |

詳細は `feasibility-results.json` に保存しています。

## 7. デモシナリオ案 5 件

### 案 1: 海外出張計画・予約オペレーション

**推奨度: 1 位 / 現在の実装で即デモ可能**

#### ストーリー

ユーザーが「パリへの 2 泊出張を手配して」と依頼します。agent はフライトとホテルを並行調査し、途中経過を stream 表示します。予約直前に approval UI を出し、承認後に booking を実行します。

途中でユーザーが「やはりロンドンへ変更」「直行便を優先」と入力すると、active turn を steering します。最後に crash simulation を行い、同じ response が復旧する様子を見せます。

#### コード構成

- LangGraph: search → recommendation → approval → booking
- Foundry model: tool selection と説明生成
- Foundry State Store: graph checkpoint
- Responses API: background、store、stream、reconnect
- deterministic confirmation: side-effect の idempotency を説明

#### UI 構成

- 左または中央: 会話 transcript
- 上部: `Planning` / `Waiting approval` / `Recovering` / `Completed`
- 右または下部: flight/hotel proposal card
- approval bar: Approve / Deny
- raw event drawer: sequence number、response ID、checkpoint

#### デモ映えポイント

- travel は観客が即座に理解できる。
- streaming、approval、steering の違いを視覚化しやすい。
- crash 後も booking context が残るため、復旧の価値が明確。

### 案 2: 調達・購買申請エージェント

**推奨度: 2 位 / 業務価値を説明しやすい**

#### ストーリー

部門ユーザーが PC 100 台の調達を依頼します。agent は要件整理、候補 vendor 比較、価格・納期・リスク評価、発注書案作成まで長時間処理します。金額閾値を超える発注は manager approval を要求します。

審査中に「納期を優先」「特定 vendor を除外」という指示を steering します。承認後の purchase order 作成は安定 operation ID で重複防止します。

#### コード構成

- supplier search tools
- scoring graph
- purchase request state store
- `purchase_request_id` を idempotency key として固定
- approval policy: 金額、vendor risk、国外送信

#### UI 構成

- vendor 比較 table
- price / lead time / risk の 3 軸表示
- workflow timeline
- approval card に差分、金額、監査理由を表示
- reconnect 後に checkpoint marker を強調

#### デモ映えポイント

- 「二重発注しない」が crash resilience の価値として伝わる。
- approval policy を経営・監査担当へ説明しやすい。

### 案 3: セキュリティインシデント対応コマンドセンター

**推奨度: 3 位 / steering の価値が最も強い**

#### ストーリー

agent が alert、ログ、資産情報、脅威 intelligence を収集し、数十分かかる incident triage を実行します。途中で新しい IOC や経営判断が入ると、「封じ込め優先」「対象 server を隔離しない」などの指示で方向転換します。

アカウント無効化、host isolation、firewall rule 変更などは Human-in-the-loop を必須にします。

#### コード構成

- evidence collection graph
- incident ID 単位の durable state
- evidence checkpoint は append-only
- containment action に idempotency key
- active turn steering と cancellation

#### UI 構成

- incident timeline
- evidence stream
- current hypothesis と confidence
- proposed containment actions
- red approval banner
- process restart 時の recovery indicator

#### デモ映えポイント

- 状況変化の速い incident response と steering の相性が良い。
- 「agent が暴走せず、人間が途中介入できる」を強く訴求できる。

### 案 4: 規制文書レビュー・当局提出エージェント

**推奨度: 4 位 / 長時間処理と監査性を説明しやすい**

#### ストーリー

大量の規制文書、契約書、添付資料を分析し、提出 draft、根拠一覧、欠落項目を生成します。reviewer は途中で対象地域や解釈方針を変更できます。当局や外部組織への送信前に approval を要求します。

#### コード構成

- document chunk processing
- phase checkpoint: ingest → extract → validate → draft → approve
- artifact は Blob、checkpoint pointer は Foundry State Store
- submission ID による重複送信防止

#### UI 構成

- document processing progress
- citation panel
- unresolved issues queue
- draft diff
- Submit approval card

#### デモ映えポイント

- 数時間処理、途中結果、再接続の必要性が自然。
- audit log と approval の必然性が高い。

### 案 5: 製造品質問題・リコール判断エージェント

**推奨度: 5 位 / executive demo に適する**

#### ストーリー

agent が複数工場の検査結果、部品 lot、顧客クレーム、出荷先を長時間分析し、影響範囲と recall candidate を作成します。新しい検査結果が到着すると分析方針を steering します。

生産停止、顧客通知、recall 開始は approval を必須にします。process crash 後も調査対象 lot と完了済み分析を復旧します。

#### コード構成

- lot graph traversal
- evidence checkpoint
- recall scope versioning
- outbound notification の idempotency
- multi-role approvals

#### UI 構成

- lot lineage graph
- plant / product / region impact map
- confidence と未確認 evidence
- proposed action cards
- executive approval panel

#### デモ映えポイント

- 地図、系譜 graph、影響範囲を使った視覚表現が強い。
- crash resilience と Human-in-the-loop を重大業務の安全性として説明できる。

## 8. 推奨デモ進行

案 1 を 8～10 分で実演する構成:

1. Paris 2 泊の出張予約を入力する。
2. flight / hotel 検索と途中出力を見せる。
3. active turn 中に「京都 2 日間へ変更」を投入し、steering を見せる。
4. 元の Paris シナリオを再実行し、booking approval を表示する。
5. Deny を一度見せ、理由を変更して再提案させる。
6. Approve し、confirmation を表示する。
7. `simulate_crash` を実行する。
8. UI が `Recovering` となり、同じ response ID で完了することを見せる。
9. raw events で sequence cursor と replay を確認する。

## 9. 本番化前の注意点

- 全機能 preview。SLA はなく、API と package version は変更され得る。
- Responses の crash recovery は `store=true`、`background=true` が前提。
- foreground response は process crash 後に reinvoke されない。
- external side effect は at-least-once を前提に設計する。
- approval 対象操作を risk taxonomy として先に定義する。
- queue full、cancel、timeout、stale input precondition を UI で明示する。
- checkpoint schema は deployment version 間で互換性を保つ。
- user isolation と retention policy を確認する。
- preview 機能の region availability は導入時に再確認する。

## 10. 公式情報

- [What's new in Microsoft Foundry](https://learn.microsoft.com/en-us/azure/foundry/whats-new-foundry)
- [Long-running agent API reference](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/long-running-agent-reference)
- [Resilience for long-running hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/long-running-agent-resilience)
- [Manage state for long-running agents](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-task-state)
- [Stream long-running output with reconnect](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/stream-with-reconnect)
- [Recover long-running work after a crash](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/recover-long-running-work)
- [Add a human-in-the-loop approval step](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/add-human-in-the-loop)
- [Steer an in-flight agent turn](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/steer-hosted-agent)
- [Deploy a crash-resilient long-running agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-resilient-agent)
- [Deploy a steerable agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-steerable-agent)
