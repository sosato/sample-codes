# 実装計画: MAF vs GitHub Copilot SDK — Foundry Hosted Agent 差分検証ハーネス

作成日: 2026-08-19
対象リポジトリ: `C:\CLI\Git\work` (sosato/work)
成果物出力先: `C:\CLI\Local\20260819-foundry-hosted-agent-harness\`

---

## 1. 目的

同一の「Grounding ソース無し・3 ステップ推論ロジック」を、

- **A) Microsoft Agent Framework (MAF)**
- **B) GitHub Copilot SDK (Python)**

の 2 通りで実装し、いずれも **Microsoft Foundry Agent Service の Hosted Agent** としてデプロイして、
**実装コードの差分**と**実行時の動作差分**を定量・定性の両面で比較できる最小ハーネスを作る。

LLM は Foundry Model の `gpt-5.6-terra` を 1 デプロイのみ用意し、両実装で共有する
（モデル差を排除して「フレームワーク差」だけを観測するため）。

---

## 2. 現状分析（リポジトリ）

| 項目 | 状況 |
|---|---|
| `MAF_Samples/` | MAF (Python) の workflow サンプル 4 本 + `requirements.txt` (`agent-framework --pre`, `azure-identity` 等)。**ローカル実行前提**で、Hosted Agent 化はされていない（README にも「コンテナ化が必要」と記載のみ） |
| `maf_poc/` | MAF 概要・ラボ手順・テストシナリオ提案の Markdown。`maf_overview.md` に「GitHub Copilot SDK 統合(2026/01/27)」の記載あり |
| Copilot SDK 資産 | **無し**（新規） |
| Hosted Agent / azd / Bicep 資産 | **無し**（新規） |

→ 本計画は既存 `MAF_Samples` を書き換えず、**新規の独立ハーネス**として作る。
既存サンプルは 3 ステップロジックの記述スタイル（Executor 分割・env var 命名）の参考に留める。

---

## 3. 調査で確定した技術事実（実装の前提）

出典はすべて Microsoft Learn / PyPI の一次情報。

### 3.1 Hosted Agent 共通

- Hosted Agents は **GA**。コンテナ化・Web サーバ・スケール・セッション状態永続化・Entra エージェント ID をプラットフォームが担う。
  出典: `learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents`
- デプロイ経路は 2 系統:
  1. **コンテナ** (`azd deploy` が ACR へ build & push) — `how-to/deploy-hosted-agent`
  2. **ソースコード ZIP** (`--deploy-mode code`、Docker 不要) — `how-to/deploy-hosted-agent-code`
- プロトコルは 2 種:
  - **Responses**: OpenAI 互換 `/responses`。会話履歴・ストリーミング・セッションはプラットフォーム管理。**推奨**
  - **Invocations**: HTTP リクエスト/レスポンスを完全に自前制御
- ランタイムに **自動注入される環境変数**: `FOUNDRY_PROJECT_ENDPOINT` / `AZURE_AI_MODEL_DEPLOYMENT_NAME` / `APPLICATIONINSIGHTS_CONNECTION_STRING`
- ローカル実行は `azd ai agent run`（`http://localhost:8088`）、呼び出しは `azd ai agent invoke --local "..."`
- 必要権限: 既存 Foundry プロジェクトなら project スコープの `Foundry Project Manager`、新規作成なら RG スコープの `Owner`
- `azd` 1.27.1 以上。拡張名がドキュメント間で **`azure.ai.agents` と `microsoft.foundry` の 2 表記**あり → 実機で確認する（§8 リスク）

### 3.2 A) MAF 側

- **専用ホスティング統合あり**: `agent-framework-foundry-hosting`（Python, prerelease）
- `pip install --pre agent-framework-foundry agent-framework-foundry-hosting azure-identity`
- 最小コード（Responses）:
  ```python
  client = FoundryChatClient(project_endpoint=..., model=..., credential=DefaultAzureCredential())
  agent  = Agent(client=client, instructions="...", default_options={"store": False})
  ResponsesHostServer(agent).run()
  ```
- `Workflow.as_agent()` で **ワークフローをそのまま Agent として公開可能** → 3 ステップを MAF Workflow で表現できる
- `store: False` 推奨（履歴の二重管理回避）
- `InvocationsHostServer` / `InvocationAgentServerHost` も選択可
- 認証は `DefaultAzureCredential`（ホスト側で専用 Entra エージェント ID が付与される）→ **トークン管理コード不要**

### 3.3 B) GitHub Copilot SDK 側

- Foundry 公式は「bring-your-own」枠。`requirements.txt` に `github-copilot-sdk` を足し、
  ホスティングライブラリ (`azure-ai-agentserver-responses>=1.0.0b7` or `azure-ai-agentserver-invocations>=1.0.0b5`) を併用
- 公式サンプルは **Invocations プロトコル**版 (`bring-your-own/invocations/github-copilot`)
- PyPI `github-copilot-sdk` **1.0.11** / Python **>=3.11**。実体は **「GitHub Copilot CLI を JSON-RPC で駆動する SDK」**
  - **ランタイムバイナリの取得が必要**: `python -m copilot download-runtime`（未取得なら初回実行時に自動 DL）
  - キャッシュ先 (Linux): `~/.cache/github-copilot-sdk/cli/<version>/copilot`
  - 制御用 env: `COPILOT_CLI_PATH` / `COPILOT_CLI_EXTRACT_DIR` / `COPILOT_SKIP_CLI_DOWNLOAD` / `COPILOT_CLI_DOWNLOAD_BASE_URL`
  - `CopilotClient(base_directory=...)` で `COPILOT_HOME` を指定可（**書き込み可能ディレクトリが限られる環境向け**＝Hosted Agent で重要）
  - トランスポート: stdio（既定・子プロセス）/ TCP / in-process FFI（実験的）
- **BYOM（カスタムプロバイダー）対応**:
  ```python
  session = await client.create_session(
      model="gpt-5.6-terra",           # provider 指定時は必須
      provider={
          "type": "azure",             # *.openai.azure.com は必ず "azure"
          "base_url": "https://<res>.openai.azure.com",  # ホストのみ。/openai/v1 は付けない
          "bearer_token": token,       # api_key より優先
          "wire_api": "completions",
          "azure": {"api_version": "2025-04-01-preview"},
      },
  )
  ```
  - Foundry プロジェクト形式のエンドポイントを使う場合は `type: "openai"` +
    `https://<res>.services.ai.azure.com/api/projects/<project>/openai/v1/`（社内スキル `azure-hosted-copilot-sdk` の記載）
  - **`bearer_token` は静的。約 1 時間で失効し自動更新されない** → セッション生成ごとに取得する実装が必須
  - **`wire_api: "responses"` は多ターン tool call を壊す**（`store:false` で 400）→ `"completions"` 固定
  - **暗号化コンテンツ制約**: Copilot SDK はプロンプトを暗号化するため、復号対応モデルのみ動作。
    非対応モデルは `400 Encrypted content is not supported` またはサイレントタイムアウト。
    既知の対応系列は o-series と gpt-5 ファミリー。**`gpt-5.6-terra` の可否は未検証 → 最優先 PoC 項目（§8-R1）**
  - 既定 `mode="copilot-cli"` は CLI エージェントとしての既定プロンプト/ツール/メモリが効く。
    純粋な推論比較には `mode="empty"` 系や `system_message` 上書きで**素の状態に近づける**必要がある（差分観測の重要ポイント）

### 3.4 モデル

- `gpt-5.6-terra`（OpenAI フォーマット、バージョン `2026-07-09`）は Foundry のモデルとして実在。
  出典: Foundry model router 対応モデル表（`gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna`）
- リージョン可用性とクォータは実機で `az cognitiveservices` / ポータルで確認（§5-T1）

---

## 4. 前提・決定事項（曖昧点に対する判断）

autopilot のため質問せず以下を決定。異論があれば差し替え可。

| # | 論点 | 決定 | 理由 |
|---|---|---|---|
| D1 | 「3 ステップ」の中身 | **分解 → 個別回答 → 統合/自己検証** の 3 回 LLM 呼び出し | Grounding 不要・ツール不要で最小、かつ「複数ステップのオーケストレーション」の差が出る |
| D2 | 言語 | **Python** に統一 | Copilot SDK の Foundry 公式サンプルが Python、MAF も Python サンプル資産が既存 |
| D3 | プロトコル | 両方とも **Responses を主**、Copilot SDK 版のみ **Invocations も副次実装** | 公平比較のため主軸を揃え、公式サンプル準拠版も残す |
| D4 | デプロイ方式 | **`--deploy-mode code`（ZIP）を主**、Copilot SDK 版はバイナリ同梱が必要なら **コンテナへフォールバック** | Docker 依存を最小化しつつ、CLI ランタイム同梱要件に対応 |
| D5 | モデルデプロイ | `gpt-5.6-terra` を **1 デプロイのみ**、両実装で共有 | モデル差の排除。コスト最小 |
| D6 | Foundry プロジェクト | **RG-Harness-Test 内に新規 1 つ**、その中に hosted agent を 2 つ | 比較条件を揃える／リソース最小化 |
| D7 | 出力の決定性 | 温度等は既定のまま、**同一入力を N=5 回**実行して差分を統計的に見る | Copilot SDK 側は温度パラメータの露出が限定的なため |
| D8 | 中間ステップの可視化 | 3 ステップの各中間結果を **構造化ログ (JSONL)** で残す | 「動作差分」の実体を後から比較可能にする |
| D9 | 認証 | MAF=`DefaultAzureCredential`、Copilot SDK=`DefaultAzureCredential` からトークン取得して `bearer_token` へ | Hosted Agent の Entra エージェント ID を両方で使う（キー不使用） |

---

## 5. タスク分解

### フェーズ T0: 準備・検証ゲート（Azure 課金前）

| ID | タスク | 完了条件 |
|---|---|---|
| T0-1 | `az login` / サブスクリプション確認、`azd version >= 1.27.1`、`azd ext install` の正しい拡張名を実機確認 | バージョンと拡張名がログに記録される |
| T0-2 | `gpt-5.6-terra` の対象リージョン可用性・クォータ確認 | デプロイ可能リージョンを 1 つ確定 |
| T0-3 | **費用見積りとユーザー承認取得**（§7） | 明示承認が得られる |

### フェーズ T1: 共通仕様の確定

| ID | タスク | 完了条件 |
|---|---|---|
| T1-1 | 3 ステップの system/user プロンプト 3 種を `shared/prompts.py` として確定（両実装で文字列共有） | 両実装が同一プロンプト定数を import |
| T1-2 | 評価用入力セット（Grounding 不要な推論課題 5 件）を `shared/inputs.json` に定義 | 5 件が確定 |
| T1-3 | 中間結果ログのスキーマ（step, prompt_hash, latency_ms, tokens, output）を確定 | JSONL スキーマ定義完了 |

### フェーズ T2: A) MAF 実装

| ID | タスク | 完了条件 |
|---|---|---|
| T2-1 | `agents/maf/` 作成。`FoundryChatClient` + 3 Executor の Workflow（decompose→solve→synthesize） | ローカルで `python main.py` 相当が動く |
| T2-2 | `Workflow.as_agent()` → `ResponsesHostServer` でラップ、`store: False` | `/responses` が応答 |
| T2-3 | ステップごとの計測フック（レイテンシ・トークン）を挿入し JSONL 出力 | ログが 3 行/実行で出る |
| T2-4 | `azd ai agent init --protocol responses --deploy-mode code` → `azd ai agent run` でローカル検証 | `azd ai agent invoke --local` が成功 |

### フェーズ T3: B) Copilot SDK 実装

| ID | タスク | 完了条件 |
|---|---|---|
| T3-1 | **PoC 先行**: ローカルで `github-copilot-sdk` + provider BYOM → `gpt-5.6-terra` に 1 発疎通（暗号化コンテンツ制約の可否判定） | 成功 or 明確な失敗理由の記録 |
| T3-2 | T3-1 が失敗した場合の代替判断（§8-R1 の分岐） | 代替モデル or 代替構成を確定 |
| T3-3 | `agents/copilot/` 作成。`CopilotClient` を 1 セッションで 3 ターン送る 3 ステップ実装 | ローカルで 3 ステップ完走 |
| T3-4 | `bearer_token` をセッション生成ごとに再取得するヘルパー実装 | 1 時間超の連続実行で 401 が出ない |
| T3-5 | `base_directory` / `COPILOT_HOME` を書き込み可能パスへ固定、`COPILOT_CLI_PATH` でランタイム事前配置 | 実行時ダウンロード無しで起動 |
| T3-6 | `ResponsesAgentServerHost` でラップ（主）＋ `InvocationAgentServerHost` 版（副）を用意 | 両方ローカルで応答 |
| T3-7 | 計測フック・JSONL 出力を T2-3 と同一スキーマで実装 | ログが同形式で出る |

### フェーズ T4: インフラ（RG-Harness-Test）

| ID | タスク | 完了条件 |
|---|---|---|
| T4-1 | `azd provision` で `RG-Harness-Test` に Foundry account/project、`gpt-5.6-terra` デプロイ、App Insights、（必要なら ACR）を作成 | 全リソースが RG-Harness-Test 配下 |
| T4-2 | RBAC 確認（`Foundry Project Manager` / エージェント ID のモデルアクセス） | 権限エラー無し |
| T4-3 | `azd deploy` で hosted agent を 2 つデプロイ、`active` 到達を確認 | 2 エージェントとも `active` |

### フェーズ T5: 検証と差分測定

| ID | タスク | 完了条件 |
|---|---|---|
| T5-1 | 比較ドライバ `bench/run_compare.py`：5 入力 × 5 反復 を両エンドポイントへ投げ、JSONL 収集 | 50 実行分のログ |
| T5-2 | コールドスタート測定（デプロイ直後 1 発目 / アイドル後 1 発目） | 各 3 回計測 |
| T5-3 | 障害系: 不正入力、長文入力、連続実行でのトークン失効挙動 | 各ケースの挙動を記録 |
| T5-4 | App Insights のトレース/OpenTelemetry 出力の粒度比較 | スクリーンショット or KQL 結果 |

### フェーズ T6: レポート

| ID | タスク | 完了条件 |
|---|---|---|
| T6-1 | `REPORT.md`（§6 の比較マトリクスを埋めたもの）を出力先へ作成 | 全項目に実測値または「該当なし」 |
| T6-2 | `README.md`（セットアップ・実行・後片付け手順） | `azd down` を含む |

---

## 6. 比較マトリクス（測定・記述する項目）

### 6.1 実装差分（静的）

| 観点 | MAF | Copilot SDK |
|---|---|---|
| 依存パッケージ数 / インストールサイズ | | |
| エージェント定義に要した LOC | | |
| 3 ステップの表現方法 | Workflow + Executor | セッション内 3 ターン |
| ホスティング統合 | 専用 (`*-foundry-hosting`) | 汎用 (`azure-ai-agentserver-*`) |
| モデル指定 | env 自動注入で完結 | `provider` を明示構築 |
| 認証コード量 | `DefaultAzureCredential` のみ | トークン取得＋再取得ロジック |
| 追加ランタイム同梱 | 不要 | **Copilot CLI バイナリ必要** |
| デプロイ方式 | code(ZIP) 可 | code(ZIP) 可否を検証、要なら container |
| 構造化出力 / 型付け | | |
| Responses プロトコル適合 | ネイティブ | ラッパー経由 |

### 6.2 動作差分（動的）

| 指標 | 測定方法 |
|---|---|
| コールドスタート時間 | デプロイ直後・アイドル後の初回応答 |
| ステップ別レイテンシ (P50/P95) | JSONL の `latency_ms` |
| エンドツーエンド レイテンシ | 同上合計 |
| 入出力トークン数・推定コスト | ログ + Foundry メトリクス |
| ストリーミング挙動 | SSE 有無・初回トークン到達時間 |
| 出力の安定性 | 同一入力 5 回の差分（長さ・構造・結論一致率） |
| 3 ステップ中間結果の質 | 分解粒度・自己検証の指摘有無 |
| セッション状態の永続性 | `$HOME` 永続化の効き方 / `COPILOT_HOME` |
| 可観測性 | App Insights 上のスパン粒度 |
| エラー時挙動 | 401/400/タイムアウト時のメッセージと回復 |
| 既定システムプロンプトの影響 | Copilot SDK の CLI モード由来の混入有無 |

---

## 7. コストと承認ゲート（重要）

**T4 以降は Azure の実リソースを新規展開するため、着手前にユーザーの明示承認を必須とする。**

| リソース | 課金モデル | 概算 |
|---|---|---|
| Foundry account + project | 基本無料（従量はモデル側） | $0 |
| `gpt-5.6-terra` デプロイ | 従量（トークン） | 検証 50 実行 × 3 ステップ程度なら数 USD 未満 |
| Application Insights | 取り込み量従量 | 数 USD/月未満 |
| Azure Container Registry (Basic) | 約 $0.17/日 | 必要時のみ |
| Hosted Agent 実行基盤 | 実行時間従量 | 検証規模では小 |

- 想定は **$100/day を大きく下回る**が、規約に従い実展開前に対象・概算・rollback を提示して承認を取る。
- rollback / cleanup: `azd down`（RG ごと削除）。RG は本検証専用の `RG-Harness-Test` なので副作用なし。
- **注意**: 既存 Foundry プロジェクトを選ぶと `azd down` はプロジェクトを残すため、**新規作成を選ぶ**こと（D6）。

---

## 8. リスクと緩和

| ID | リスク | 影響 | 緩和 |
|---|---|---|---|
| R1 | **`gpt-5.6-terra` が Copilot SDK の暗号化コンテンツ非対応**（`400 Encrypted content is not supported`） | B 実装が成立しない | T3-1 で最優先 PoC。失敗時は (a) `wire_api`/`api_version` 調整、(b) 比較用に別の対応モデル(gpt-5 系)を **追加デプロイして「モデル制約」自体を差分として報告**、(c) それも不可なら B は GitHub 既定モデル構成で実装しモデル差を明記 |
| R2 | Hosted Agent コンテナで Copilot CLI バイナリを実行時ダウンロードできない（egress 制限 / 読み取り専用 FS） | B のデプロイ失敗 | ビルド時に `python -m copilot download-runtime` して同梱＋`COPILOT_CLI_PATH` 指定。ZIP デプロイで不可なら container デプロイへ切替（D4） |
| R3 | `bearer_token` の 1 時間失効 | 長時間セッションで 401 | セッション生成ごとにトークン再取得（T3-4）。検証項目としても記録 |
| R4 | `agent-framework-foundry-hosting` が prerelease で破壊的変更 | A のビルド失敗 | `requirements.txt` でバージョン固定。動作確認時のバージョンを REPORT に明記 |
| R5 | `azd` 拡張名の表記揺れ (`azure.ai.agents` / `microsoft.foundry`) | 初期化失敗 | T0-1 で実機確認し、確定した方を全手順に反映 |
| R6 | `gpt-5.6-terra` のリージョン未提供・クォータ不足 | 展開不可 | T0-2 で事前確認。代替リージョン候補を 2 つ用意 |
| R7 | Copilot SDK の既定 CLI モードがコーディングエージェント的挙動を混入 | 比較が不公平 | `mode` / `system_message` を明示制御し、「素にできる度合い」自体を差分として記録 |
| R8 | 権限不足 (`Foundry Project Manager` / RG `Owner`) | provision 失敗 | T0-1 で `az role assignment list` により事前確認 |

---

## 9. 成果物ディレクトリ構成（予定）

```
C:\CLI\Local\20260819-foundry-hosted-agent-harness\
├── README.md                    # セットアップ・実行・後片付け
├── REPORT.md                    # 比較レポート（最終成果物）
├── shared\
│   ├── prompts.py               # 3 ステップ共通プロンプト
│   ├── inputs.json              # 評価入力 5 件
│   └── tracing.py               # JSONL 計測ヘルパー（両実装で共有）
├── agents\
│   ├── maf\
│   │   ├── main.py              # Workflow(3 Executor) → as_agent → ResponsesHostServer
│   │   ├── requirements.txt
│   │   └── azure.yaml           # azd ai agent init 生成
│   └── copilot\
│       ├── main.py              # ResponsesAgentServerHost + CopilotClient(BYOM)
│       ├── main_invocations.py  # 公式サンプル準拠(副)
│       ├── requirements.txt
│       ├── Dockerfile           # R2 対策でランタイム同梱する場合のみ
│       └── azure.yaml
├── bench\
│   ├── run_compare.py           # 5 入力 × 5 反復のドライバ
│   └── analyze.py               # JSONL → 比較表生成
└── logs\                        # 実行結果 JSONL（gitignore 対象）
```

---

## 10. 完了条件 (Definition of Done)

1. `RG-Harness-Test` 配下に Foundry project・`gpt-5.6-terra` デプロイ・hosted agent × 2 が `active` で存在する
2. 両エージェントが同一入力に対して 3 ステップの中間結果を含む応答を返す
3. `bench/run_compare.py` が 50 実行分の JSONL を出力し、`analyze.py` が §6 の表を埋める
4. `REPORT.md` に実装差分・動作差分・制約（特に R1/R2 の結論）が実測値付きで記載される
5. `azd down` による後片付け手順が README に記載され、動作確認済み
6. コード変更を伴う各フェーズで lint/実行検証が通っている

---

## 11. 実行順序サマリ

```
T0（承認ゲート） → T1 → T2 / T3-1(PoC) 並行 → T3 残り → T4（Azure 展開） → T5 → T6
```

- T2（MAF）と T3-1（Copilot SDK PoC）は依存が無いため並行可
- **T3-1 の結果が B の設計を左右する**ため、T3 の残りは T3-1 完了後に着手
- T4 は T0-3 の承認取得後にのみ実行
