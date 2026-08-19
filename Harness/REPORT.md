# 比較レポート: MAF vs GitHub Copilot SDK（Foundry Hosted Agent）

- 作成日: 2026-08-19
- 対象: 同一の「Grounding ソース無し・3 ステップ推論」を 2 フレームワークで実装し、
  Microsoft Foundry Agent Service の Hosted Agent としてデプロイした場合の差分
- モデル: Foundry Model `gpt-5.6-terra`（バージョン `2026-07-09`、両実装で 1 デプロイを共有）
- リソースグループ: `RG-Harness-Test`

> **本レポートの状態: 実測完了**
> 両実装を `RG-Harness-Test` に **Hosted Agent として実際にデプロイし**、
> 同一入力 5 件 × 3 反復（計 30 リクエスト、成功率 30/30）を計測済み。
> §5 の数値はすべて実測値です。

---

## 1. サマリ（先に結論）

| 観点 | 結論 |
|---|---|
| ホスティング統合 | **MAF が一級市民**。`agent-framework-foundry-hosting` の 3 行で Hosted Agent 化できる。Copilot SDK は「bring-your-own」枠で、汎用の `azure-ai-agentserver-*` を自分で被せる |
| モデル指定 | MAF は環境変数のみで完結。Copilot SDK は **BYOM の `provider` 辞書を自前構築**し、トークンも **audience を選んで**自分で取得・更新する必要がある |
| 配布サイズ | MAF **137 MB** に対し、Copilot SDK は **85 MB + CLI ランタイム 151.6 MB ≒ 237 MB**。Copilot SDK は Python パッケージだけでは動かない |
| 3 ステップの表現 | MAF は Workflow の Executor として宣言的に表現。Copilot SDK は `system_message` がセッション単位のため **ステップごとにセッションを作り直す**手続き的な実装になる |
| 出力品質 | **差は見られない**（同一モデル・同一プロンプトのため当然）。総トークンも MAF 19,921/14,129、Copilot 21,280/13,793 とほぼ同等 |
| レイテンシ | E2E P50 は Copilot **23.3s** / MAF **25.5s** とほぼ互角。ただし内訳は大きく異なる（§5.3） |
| セッション初期化 | **Copilot SDK は 1 ステップ目に約 5s 余分にかかる**（CLI プロセス spawn）。MAF decompose P50 3.1s に対し Copilot 8.0s |
| 可観測性 | 両方ともステップ別トークン使用量を取得できる（Copilot は `AssistantUsageData` **イベント**を購読する必要がある） |
| エラー処理 | **Copilot SDK は失敗を例外ではなくイベントで通知する**。購読しないと「例外なし・応答も空」の無音失敗になる（実際に踏んだ） |
| 認可 | MAF は Foundry プロジェクト経由で追加設定不要。**Copilot SDK は audience の選択を誤ると 401/PermissionDenied**（§7-3） |
| R1（最大リスク） | **解消**。`gpt-5.6-terra` は Copilot SDK の BYOM で問題なく動作した |

---

## 2. 検証環境

| 項目 | 値 |
|---|---|
| 実施日 | 2026-08-19 |
| OS（開発機） | Windows / Python 3.11.9 |
| Hosted Agent ランタイム | Python 3.13（`--runtime python_3_13`） |
| Azure リージョン | eastus2 |
| リソースグループ | `RG-Harness-Test` |
| Foundry アカウント / プロジェクト | `harness-foundry-92361` / `harness-proj` |
| モデルデプロイ | `gpt-5.6-terra` バージョン `2026-07-09`、SKU `GlobalStandard`、容量 50 |
| デプロイ方式 | **ZIP（`--deploy-mode code`、`dependencyResolution: remote_build`）**。開発機に Docker が無いためコンテナ方式は未使用 |
| azd | 1.30.0 + 拡張 `azure.ai.agents` 1.0.0-beta.9 |
| 計測 | 5 入力 × 3 反復 × 2 実装 = 30 リクエスト（成功 30/30）+ コールド 2 リクエスト |

### 2.1 解決されたバージョン（実測）

**MAF 版** (`agents/maf/requirements.txt`)

| パッケージ | バージョン |
|---|---|
| `agent-framework-core` | 1.13.0 |
| `agent-framework-foundry` | 1.10.4 |
| `agent-framework-openai` | 1.12.0（`agent-framework-foundry` の依存） |
| `agent-framework-foundry-hosting` | 1.0.0b260730（prerelease） |
| `azure-ai-projects` | 2.3.0 |
| `azure-identity` | 1.26.0b2 |

**Copilot SDK 版** (`agents/copilot/requirements.txt`)

| パッケージ | バージョン |
|---|---|
| `github-copilot-sdk` | 1.0.9 |
| Copilot CLI ランタイム | **v1.0.78**（別途ダウンロード） |
| `azure-ai-agentserver-core` | 2.1.0b1 |
| `azure-ai-agentserver-responses` | 2.1.0b1 |
| `azure-ai-agentserver-invocations` | 1.1.0b1 |
| `azure-identity` | 1.25.3 |

---

## 3. 実装差分（静的・実測）

### 3.1 依存とサイズ

| 指標 | MAF | Copilot SDK | 備考 |
|---|---|---|---|
| pip パッケージ数 | **106** | **82** | 独立 venv での実測 |
| venv サイズ | **137 MB** | **85 MB** | 同上 |
| 追加ランタイム | なし | **`copilot.exe` 151.6 MB** | `python -m copilot download-runtime` で取得 |
| 実効配布サイズ | **137 MB** | **≒ 237 MB** | Copilot SDK は CLI 同梱が前提 |
| メタパッケージの罠 | `agent-framework`（全プロバイダー同梱）を使うと依存が激増する。Foundry 系のみ指定すべき | — | 本ハーネスは Foundry 系のみ指定 |

> **依存衝突（実測）**
> `agent-framework-github-copilot 1.0.1` は `github-copilot-sdk==1.0.2` を**厳密ピン**する。
> 両フレームワークを同一環境に入れると Copilot SDK が 1.0.9 → 1.0.2 へ強制ダウングレードされた。
> **実装ごとに環境を分けること**（Hosted Agent は元々別デプロイなので実運用上の制約にはならない）。

### 3.2 コード量

| ファイル | 総行数 | 実行行（コメント・空行を除く） |
|---|---|---|
| `agents/maf/main.py` | 391 | **284** |
| `agents/copilot/main.py` | 435 | **342** |
| `agents/copilot/main_invocations.py` | 72 | 51 |

上記には `--fake` / `--bench` などの検証用スキャフォールドが含まれるため、
**フレームワーク固有の必須コード**だけを比べると差はさらに明確になる。

| 必須要素 | MAF | Copilot SDK |
|---|---|---|
| クライアント生成 | `FoundryChatClient(project_endpoint, model, credential)` の 1 呼び出し | `CopilotClient(mode, base_directory)` + `await client.start()` |
| モデル指定 | 環境変数 `AZURE_AI_MODEL_DEPLOYMENT_NAME` を渡すだけ | `provider` 辞書（`type` / `base_url` / `bearer_token` / `wire_api` / `azure.api_version`）を自前構築 |
| 認証 | `DefaultAzureCredential()` を渡して終わり | トークンを自分で取得し、**失効前に再取得するキャッシュを実装**（本ハーネスで約 25 行） |
| ランタイム準備 | 不要 | `COPILOT_HOME` / `COPILOT_CLI_PATH` の解決コードが必要（約 15 行） |
| 応答の取り出し | `response.text` | イベントハンドラで `AssistantMessageData` を収集し、`SessionIdleData` で完了検知（約 15 行） |
| ホスティング | `ResponsesHostServer(agent).run()`（**3 行**） | `ResponsesAgentServerHost()` + `@app.response_handler` + `TextResponse(...)`（**約 15 行**） |

### 3.3 3 ステップの表現方法

| | MAF | Copilot SDK |
|---|---|---|
| 表現 | `WorkflowBuilder` + `@executor` の**宣言的グラフ** | `session` を 3 回作る**手続き的な逐次処理** |
| ステップ間の受け渡し | 型付き dataclass メッセージ（`Decomposed` / `Solved`） | Python のローカル変数 |
| ステップ固有の system prompt | `chat_client.as_agent(instructions=...)` でステップごとに Agent を作成 | `system_message` が**セッション単位**のため、ステップごとにセッションを作り直す必要がある |
| Agent 化 | `Workflow.as_agent()` でそのまま Hosted Agent 化できる | Workflow 抽象が無いため、ハンドラ内で自前に順序制御 |
| 途中経過の可視化 | ワークフローイベントとして取得可能 | 自前で記録するしかない |

> MAF 側の実装上の注意（実測で判明、いずれも `Workflow.as_agent()` 利用時の必須事項）
>
> 1. `@executor` の型注釈は `typing.get_type_hints` で解決されるため、
>    **executor 関数を関数内（ローカルスコープ）で定義すると `WorkflowContext[...]` の解決に失敗する**。
>    `ValueError: Function instance parameter 'ctx' must be annotated as WorkflowContext...` になるため、
>    executor と依存する型・import は**モジュールトップレベル**に置くこと。
> 2. `Workflow.as_agent()` は入力を必ず `list[Message]` へ正規化して開始 Executor に渡す。
>    開始 Executor が `str` を受け取る宣言だと Agent 化の時点で
>    `ValueError: Workflow's start executor cannot handle list[Message]` で失敗する。
>    **開始 Executor は `list[Message]` を受け取り、自分でテキストを取り出す**必要がある
>    （ローカル実行時も `workflow.run([Message(role="user", contents=[text])])` で呼ぶ）。

### 3.4 プロトコルとデプロイ

| | MAF | Copilot SDK |
|---|---|---|
| 推奨プロトコル | Responses（`ResponsesHostServer`） | Responses（本ハーネスの主系） |
| 公式サンプルのプロトコル | Responses | **Invocations**（`bring-your-own/invocations/github-copilot`） |
| Invocations 対応 | `InvocationsHostServer` あり | `InvocationAgentServerHost` を直接使用 |
| ZIP デプロイ（`--deploy-mode code`） | 問題なし | **CLI ランタイムの取得可否に依存**（§6 R2） |
| コンテナデプロイ | 任意 | ZIP が不可なら必須。`Dockerfile` を同梱済み |
| 会話履歴 | プラットフォーム管理（`store: False` 推奨） | プラットフォーム管理（ただし Copilot 側にも独自セッション概念があり**二重になりうる**） |

### 3.5 可観測性

| 指標 | MAF | Copilot SDK |
|---|---|---|
| トークン使用量 | `AgentResponse.usage_details`（**dict**）に `input_token_count` / `output_token_count`。属性アクセスでは取れない点に注意 | `AssistantUsageData` **イベント**を購読すると `input_tokens` / `output_tokens` / `reasoning_tokens` / `cost` / `duration` まで取れる。**購読しなければ何も得られない** |
| エラー通知 | 例外として送出される | **例外ではなく `ModelCallFailureData` / `SessionErrorData` イベント**。購読しないと「例外なし・応答も空」という無音の失敗になる（本検証で実際に踏み、原因特定に時間を要した） |
| OpenTelemetry | `agent-framework` が対応。App Insights 接続文字列は自動注入 | `azure-ai-agentserver` が**インポート時に自動でトレース設定**を行う |
| ローカル実行時の副作用 | 同左（`agent-framework-foundry-hosting` は `azure-ai-agentserver-*` に依存するため同じ挙動） | `azure-ai-agentserver` が Azure IMDS(`169.254.169.254`) へ問い合わせ、Azure 外では毎回タイムアウトのエラースパンが標準出力へ出る（無害だがログが極端に読みにくい） |

> ホスティングサーバを構築する（= `azure-ai-agentserver` を import する）時点で
> IMDS へのプローブが走るため、**ローカル検証では JSON トレースが標準出力を埋め尽くす**。
> `--fake` / `--local` / `--bench` モードはホスティングサーバを構築しないため影響を受けない。

### 3.6 認証・認可（実測で最も差が出た箇所）

| | MAF | Copilot SDK |
|---|---|---|
| 呼び出し経路 | `FoundryChatClient` が Foundry プロジェクト経由でモデルを呼ぶ | `provider` で指定した HTTP エンドポイントを直接呼ぶ |
| 必要な audience | SDK が内部で解決（実装者は意識しない） | **経路ごとに異なり、実装者が選ぶ必要がある** |
| Foundry プロジェクト経路 | 既定 | `type="openai"` + `<project>/openai/v1/` + audience **`https://ai.azure.com/.default`** |
| Azure OpenAI ホスト経路 | 使わない | `type="azure"` + `https://<account>.openai.azure.com` + audience **`https://cognitiveservices.azure.com/.default`** + `azure.api_version` |
| Hosted Agent での可否 | そのまま動作 | **プロジェクト経路のみ**。AOAI ホスト直叩きはエージェント ID では `PermissionDenied`（RBAC を account スコープで付与しても解消せず） |
| ローカル（ユーザー資格情報）での可否 | 動作 | **両経路とも動作**するため、ローカルで通ってもデプロイで落ちる |

> これが本検証で最も時間を要した差分。
> ローカルでは AOAI ホスト経路が動くのに Hosted Agent では動かないため、
> 「ローカル OK → デプロイで無音失敗」という最悪の再現性になった。

---

## 4. ハーネス自体の検証結果（実測）

| 検証項目 | 結果 |
|---|---|
| `shared/prompts.py` の共有 | 両実装が同一定数を import することを確認 |
| 3 ステップ配線（`--fake`） | MAF / Copilot SDK いずれも `decompose → solve → synthesize` を完走し、同一スキーマの JSON を返すことを確認 |
| 計測精度 | `asyncio.sleep(0.25)` に対し `253.0 ms` を記録。**タイマは正確** |
| JSONL 出力 | 4 行（3 ステップ + `__total__`）が同一スキーマで出力されることを確認 |
| 応答同梱トレース | `trace` フィールドがステップ別レイテンシ・トークンを含むことを確認 |
| ベンチ経路 | スタブサーバに対し 20 サンプル収集 → `analyze.py` が 5 種類の比較表を生成することを確認 |
| コールドスタート計測 | `--cold` モードが動作することを確認 |
| `tools/sync_shared.py` | `shared/` 8 ファイルを両エージェントへ複製、`--check` で差分検出できることを確認 |
| Invocations 版 | `InvocationAgentServerHost` の構築成功を確認 |
| MAF の Agent 化 | `Workflow.as_agent()` → `WorkflowAgent`、`ResponsesHostServer(agent)` の構築成功を確認 |
| Copilot の Responses 化 | `ResponsesAgentServerHost` + `@app.response_handler` の構築成功を確認 |
| **Azure 実展開** | 両エージェントを `RG-Harness-Test` に ZIP デプロイし `active` 到達を確認 |
| **実モデル疎通** | `gpt-5.6-terra` に対し両実装が 3 ステップを完走（30/30 成功） |
| **ベンチ実測** | デプロイ済みエンドポイントへ 30 リクエスト、`analyze.py` が全表を生成 |

> 上記の数値はスタブに対する検証であり、**モデル性能の比較ではない**。

---

## 5. 動作差分（実測）

計測条件: 評価入力 5 件 × 3 反復 × 2 実装 = **30 リクエスト（成功 30/30）**。
`bench/run_compare.py` がデプロイ済みエンドポイントへ直接 POST し、
応答に同梱された `trace` からステップ別の値を回収した。

> **重要な計測上の性質**
> `run_compare.py` はセッション ID を指定しないため、**リクエストごとに新しい
> エージェントセッションが生成される**（`azd ai agent sessions list` で確認）。
> したがって以下の「warm」の数値も **毎回セッション初期化を含む**。
> セッションを再利用した場合は両実装とも短縮されるが、Copilot SDK の方が
> 初期化コストが大きいため差は縮まる方向に働く。

### 5.1 エンドツーエンド レイテンシ（クライアント実測）

| impl | n | P50 (ms) | P95 (ms) | min | max |
|---|---|---|---|---|---|
| copilot-sdk | 15 | **23,293** | 26,268 | 18,027 | 26,443 |
| maf | 15 | **25,492** | 33,240 | 18,997 | 37,773 |

- 中央値では Copilot SDK がわずかに速いが、**ばらつきは MAF の方が大きい**（max 37.8s vs 26.4s）。
- E2E（23〜25s）とサーバ側ステップ合計（17〜18s）の差 **約 6〜7s** は、
  両実装に共通するプラットフォーム側のオーバーヘッド（セッション確保・履歴ストア I/O 等）。

### 5.2 コールドスタート

| impl | E2E (ms) | http | 結果 |
|---|---|---|---|
| maf | 18,657 | 200 | OK |
| copilot-sdk | 18,994 | 200 | OK |

デプロイ直後の初回リクエストでは、MAF は「初回応答まで 6.2s」、
Copilot SDK は「初回応答まで 19.2s」だった（`azd ai agent invoke` の first-byte 計測）。
**ストリーミング開始までの体感差は Copilot SDK が明確に不利。**

### 5.3 ステップ別レイテンシ / トークン（サーバ実測）

| impl | step | n | P50 (ms) | P95 (ms) | avg in-tok | avg out-tok |
|---|---|---|---|---|---|---|
| copilot-sdk | decompose | 15 | **8,039** | 8,643 | 236 | 137 |
| copilot-sdk | solve | 15 | 6,199 | 7,929 | 366 | 490 |
| copilot-sdk | synthesize | 15 | 3,555 | 5,436 | 750 | 261 |
| copilot-sdk | **合計** | 15 | 18,273 | 20,793 | 1,353 | 888 |
| maf | decompose | 15 | **3,099** | 3,600 | 210 | 123 |
| maf | solve | 15 | 7,829 | 10,726 | 331 | 523 |
| maf | synthesize | 15 | 3,619 | 6,457 | 724 | 268 |
| maf | **合計** | 15 | 17,556 | 25,950 | 1,264 | 914 |

**最重要の差分**:

- **1 ステップ目（decompose）で Copilot SDK が約 5.0s 遅い**（8.0s vs 3.1s）。
  出力トークン数はほぼ同じ（137 vs 123）なので、これはモデル差ではなく
  **Copilot CLI プロセスの起動＋セッション確立コスト**。
- 2 ステップ目以降は CLI が温まるため逆転し、`solve` は Copilot SDK の方が 1.6s 速い。
- 合計は 18.3s vs 17.6s とほぼ同等。つまり **3 ステップ程度では初期化コストが吸収される**が、
  1 ステップだけの単純なエージェントでは Copilot SDK が明確に不利になる。

### 5.4 トークン消費（30 リクエスト合計 + コールド 2 件）

| impl | runs | 入力トークン | 出力トークン |
|---|---|---|---|
| maf | 16 | 19,921 | 14,129 |
| copilot-sdk | 16 | 21,280 | 13,793 |

入力が Copilot SDK 側で約 7% 多い。プロンプトは完全に同一なので、
**Copilot SDK 側で付加されるセッション・プロトコル由来のオーバーヘッド**と考えられる
（`mode="empty"` + 空 `ToolSet` でもゼロにはならない）。

### 5.5 出力の安定性（同一入力 3 回）

| impl | input | n | avg chars | sd chars | subtask 数 | avg confidence | ユニーク回答数 |
|---|---|---|---|---|---|---|---|
| copilot-sdk | ambiguity-05 | 3 | 332 | 28 | [3] | 0.97 | 3/3 |
| copilot-sdk | design-03 | 3 | 293 | 25 | [3] | 0.93 | 3/3 |
| copilot-sdk | estimate-02 | 3 | 289 | 48 | [3] | 0.98 | 3/3 |
| copilot-sdk | logic-01 | 3 | 75 | 3 | [3] | 0.99 | 3/3 |
| copilot-sdk | tradeoff-04 | 3 | 385 | 31 | [3] | 0.97 | 3/3 |
| maf | ambiguity-05 | 3 | 393 | 12 | [3] | 0.95 | 3/3 |
| maf | design-03 | 3 | 307 | 48 | [3] | 0.94 | 3/3 |
| maf | estimate-02 | 3 | 244 | 16 | [3] | 0.99 | 3/3 |
| maf | logic-01 | 3 | 82 | 9 | [3] | 0.99 | 3/3 |
| maf | tradeoff-04 | 3 | 394 | 53 | [3] | 0.97 | 3/3 |

- **すべての実行で subtask 数 = 3、JSON 構造も期待どおり**。両実装とも構造化出力の指示に安定して従った。
- 回答本文は毎回異なる（3/3 ユニーク）が、文字数の標準偏差は小さく、
  自己申告 confidence も 0.93〜0.99 でほぼ同じ。
- **出力品質・安定性に実装間の差は見られない**（同一モデル・同一プロンプトなので期待どおりの結果）。

### 5.6 障害系（実測で遭遇したもの）

| 事象 | 実装 | 症状 | 原因と対処 |
|---|---|---|---|
| コンテナ起動失敗 | 両方 | `session_not_ready`（HTTP 424） | `sys.path.insert(..., parents[2])` が `/app/main.py` で `IndexError`。存在する親だけを追加するよう修正 |
| 無音の失敗 | Copilot SDK | 例外も出ず全ステップの応答が空 | 失敗が `ModelCallFailureData` イベントで通知されるため。購読して例外化するよう修正 |
| `PermissionDenied` | Copilot SDK | `Principal does not have access to API/Operation` | エージェント ID で Azure OpenAI ホストを直叩きしていた。Foundry プロジェクト経路へ変更 |
| `401 audience is incorrect` | Copilot SDK | `audience is incorrect (https://ai.azure.com)` | プロジェクト経路は `https://ai.azure.com/.default` の audience が必要。経路ごとにスコープを切り替えるよう修正 |
| `ValueError: requires base_directory` | Copilot SDK | 起動不能 | `mode="empty"` は `base_directory` が必須 |
| `ValueError: without available_tools` | Copilot SDK | セッション作成不能 | `mode="empty"` は各セッションで `available_tools` の明示が必須（空 `ToolSet()` を指定） |
| Agent 化失敗 | MAF | `start executor cannot handle list[Message]` | `Workflow.as_agent()` は開始 Executor に `list[Message]` を渡す |
| 型解決失敗 | MAF | `ctx must be annotated as WorkflowContext` | `@executor` をローカルスコープで定義していた |

## 6. リスクと対応状況

| ID | リスク | 状態 | 結果 |
|---|---|---|---|
| **R1** | Copilot SDK の暗号化コンテンツ制約により `gpt-5.6-terra` が BYOM で動かない可能性 | **解消（実測）** | **動作する**。`type="azure"` + `api_version=2025-04-01-preview` で PONG 応答 8.3s（`poc_byom.py`）。Hosted Agent ではプロジェクト経路でも動作 |
| **R2** | Copilot CLI ランタイム（151.6 MB）を実行時にダウンロードできない可能性 | **解消（実測）** | **ZIP デプロイ（Docker 不要）で問題なく動作**。コンテナ内ログに `CopilotClient._start_cli_server starting Copilot CLI` を確認。同梱用 `Dockerfile` は結果的に不要だったが、egress 制限環境向けに残置 |
| **R3** | `bearer_token` が約 1 時間で失効し自動更新されない | **対応済み** | スコープ別トークンキャッシュが失効 5 分前に再取得。約 30 分の連続計測で 401 なし |
| **R4** | `agent-framework-foundry-hosting` が prerelease | **緩和済み** | 1.0.0b260730 で動作確認。`requirements.txt` に下限を明記 |
| **R5** | `azd` 拡張名の表記揺れ | **解消（実測）** | **両方存在する**。`azure.ai.agents`（1.0.0-beta.10）と `microsoft.foundry`（1.0.0-beta.2）。本検証は `azure.ai.agents` を使用 |
| **R6** | `gpt-5.6-terra` のリージョン可用性・クォータ | **解消（実測）** | eastus2 で `GlobalStandard` / `2026-07-09` が利用可能。容量 50 でデプロイ成功 |
| **R7** | Copilot SDK の既定 `mode="copilot-cli"` がコーディングエージェント用の文脈を混入させる | **対応済み** | `mode="empty"` + `system_message.mode=replace` + 空 `ToolSet()` で素の推論にできた。ただし **`empty` は `base_directory` と `available_tools` の明示を強制する**（省略すると `ValueError`） |
| **R8** | 権限不足 | **一部残** | MAF は追加設定不要。Copilot SDK は **audience の選択**が本質で、RBAC 追加では解決しなかった（§7-3） |

### 新たに判明したリスク

| ID | 内容 | 対処 |
|---|---|---|
| **N1** | **ローカルで動く構成が Hosted Agent で動かない**（AOAI ホスト直叩き）。しかも無音で失敗する | 経路ごとに audience を切り替え、Hosted Agent ではプロジェクト経路を既定にした |
| **N2** | Copilot SDK は失敗をイベントで通知するため、素朴な実装では原因が一切分からない | `ModelCallFailureData` / `SessionErrorData` を購読して例外化 |
| **N3** | Hosted Agent のコンテナは `/app/main.py` 配置のため、`Path(__file__).parents[2]` が `IndexError` | 存在する親だけを `sys.path` へ追加 |
| **N4** | セッション ID を指定しないとリクエストごとに新規セッションが作られ、毎回初期化コストがかかる | 計測条件として明示。実運用ではセッション再利用を検討 |

---

## 7. 所見（実測に基づく結論）

1. **Foundry Hosted Agent への適合度は MAF が明確に上**。専用ホスティング統合があり、
   モデル指定・認証・audience・テレメトリがすべて環境変数と `DefaultAzureCredential` だけで解決する。
   Copilot SDK は「任意のフレームワークを載せられる」枠組みに乗る形で、接着コードを自分で書く。
   実際、**デプロイまでに要した修正回数は Copilot SDK が MAF の 4 倍以上**だった（§5.6）。

2. **Copilot SDK は本質的に「CLI を駆動する SDK」**であり、ライブラリではない。
   151.6 MB のネイティブランタイムが必須で、配布サイズ・初期化時間・
   サンドボックスの書き込み権限すべてに影響する。
   ただし **ZIP デプロイでも実際に動作した**ため、当初 R2 で想定したほどの障害にはならなかった。

3. **最大の実装差分は「認可の audience」**。MAF は `FoundryChatClient` が吸収するが、
   Copilot SDK では呼び出し先ごとに audience を選ぶ必要があり、
   - Foundry プロジェクト経路 → `https://ai.azure.com/.default`
   - Azure OpenAI ホスト経路 → `https://cognitiveservices.azure.com/.default`
   さらに **Hosted Agent のエージェント ID は後者を使えない**（RBAC を足しても `PermissionDenied`）。
   ローカルでは両方通るため、**「ローカル成功 → デプロイで無音失敗」**という最悪の再現性になった。

4. **モデルの自由度に懸念はあったが、`gpt-5.6-terra` では問題なかった**（R1 解消）。
   ただし暗号化コンテンツ制約自体は残るため、**別モデルへ切り替える際は毎回検証が必要**。
   `poc_byom.py` をその都度のゲートとして使うのが現実的。

5. **性能はほぼ互角、ただし内訳が違う**。3 ステップ合計は 18.3s（Copilot）vs 17.6s（MAF）。
   一方 **1 ステップ目は Copilot SDK が約 5s 遅い**（CLI プロセス起動）。
   したがって **ステップ数が少ないエージェントほど Copilot SDK が不利**で、
   ステップが増えるほど差は薄まる。

6. **出力品質・安定性には差が無い**。同一モデル・同一プロンプトなので当然だが、
   「フレームワークを変えても推論結果は変わらない」ことを実測で確認できた意義は大きい。
   選定は**運用特性（統合の深さ・初期化コスト・可観測性・認可）で決めるべき**で、
   出力品質は判断材料にならない。

7. **可観測性は Copilot SDK の方が実は豊富**（当初の想定と逆）。
   `AssistantUsageData` は入出力トークンに加え `reasoning_tokens` / `cost` /
   `inter_token_latency` / `finish_reason` まで返す。
   ただし**イベント購読が前提**で、知らなければ何も得られない。

8. **Copilot SDK の真価は今回意図的に無効化した機能**（ツール実行、権限ハンドリング、
   メモリ、スキル、MCP、サブエージェント、カスタムエージェント）にある。
   本ハーネスは素の推論だけを比較しているため、
   **ツールを使うエージェントでは評価が逆転しうる**点に注意。

---

## 8. 残作業と後片付け

### 未実施

1. `--protocol invocations` 版（`main_invocations.py`）のデプロイと Responses との比較
2. セッション再利用時のレイテンシ計測（現状はリクエストごとに新規セッション）
3. Application Insights 上のトレース粒度の比較
4. `Dockerfile` によるコンテナデプロイ経路の検証（開発機に Docker が無いため未実施）

### 後片付け

検証が済んだら以下で `RG-Harness-Test` を削除する（**課金が継続するため必須**）。

```powershell
az group delete --name RG-Harness-Test --yes --no-wait
```

`azd down` は既存 Foundry プロジェクトを選択して初期化しているため、
リソースを残す点に注意（README §8 参照）。

### デプロイ済みリソース（2026-08-19 時点）

| 種別 | 名前 |
|---|---|
| リソースグループ | `RG-Harness-Test`（eastus2） |
| Foundry アカウント | `harness-foundry-92361` |
| Foundry プロジェクト | `harness-proj` |
| モデルデプロイ | `gpt-5.6-terra`（GlobalStandard, 容量 50） |
| Hosted Agent | `three-step-maf`（v2, active） |
| Hosted Agent | `three-step-copilot`（v3, active） |
