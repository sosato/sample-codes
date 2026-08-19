# 比較レポート: MAF vs GitHub Copilot SDK（Foundry Hosted Agent）

- 作成日: 2026-08-19
- 対象: 同一の「Grounding ソース無し・3 ステップ推論」を 2 フレームワークで実装し、
  Microsoft Foundry Agent Service の Hosted Agent としてデプロイした場合の差分
- モデル: Foundry Model `gpt-5.6-terra`（バージョン `2026-07-09`、両実装で 1 デプロイを共有）
- リソースグループ: `RG-Harness-Test`

> **本レポートの状態**
> §1〜§4（静的な実装差分・環境検証）は **ローカルで実測済み**です。
> §5（動作差分）は `RG-Harness-Test` への実展開が必要なため **未計測**で、
> 表の枠だけを用意しています。展開後に `bench/analyze.py --out bench_result.md`
> の出力を §5 に貼り込んでください。

---

## 1. サマリ（先に結論）

| 観点 | 結論 |
|---|---|
| ホスティング統合 | **MAF が一級市民**。`agent-framework-foundry-hosting` の 3 行で Hosted Agent 化できる。Copilot SDK は「bring-your-own」枠で、汎用の `azure-ai-agentserver-*` を自分で被せる |
| モデル指定 | MAF は環境変数のみで完結。Copilot SDK は **BYOM の `provider` 辞書を自前構築**し、トークンも自分で取得・更新する必要がある |
| 配布サイズ | MAF **137 MB** に対し、Copilot SDK は **85 MB + CLI ランタイム 151.6 MB ≒ 237 MB**。Copilot SDK は Python パッケージだけでは動かない |
| 3 ステップの表現 | MAF は Workflow の Executor として宣言的に表現。Copilot SDK は `system_message` がセッション単位のため **ステップごとにセッションを作り直す**手続き的な実装になる |
| 可観測性 | MAF はステップ別のトークン使用量を取得できる。**Copilot SDK はトークン使用量を公開していない** |
| 最大リスク | Copilot SDK の暗号化コンテンツ制約により **`gpt-5.6-terra` が BYOM で動くか未確認**（§6 R1） |

---

## 2. 検証環境

| 項目 | 値 |
|---|---|
| 実施日 | 2026-08-19 |
| OS | Windows |
| Python | 3.11.9（Hosted Agent 側は 3.13+ が必要） |
| 検証方法 | 実装ごとに独立した venv を作成し、依存解決・import・3 ステップ配線・ベンチ経路を実行 |
| Azure 接続 | **なし**（§5 のみ未実施） |

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
| トークン使用量 | 応答オブジェクトから取得可能（本ハーネスは複数の属性名を防御的に探索） | **公開されていない**（本ハーネスでは `null` を記録） |
| OpenTelemetry | `agent-framework` が対応。App Insights 接続文字列は自動注入 | `azure-ai-agentserver` が**インポート時に自動でトレース設定**を行う |
| ローカル実行時の副作用 | 同左（`agent-framework-foundry-hosting` は `azure-ai-agentserver-*` に依存するため同じ挙動） | `azure-ai-agentserver` が Azure IMDS(`169.254.169.254`) へ問い合わせ、Azure 外では毎回タイムアウトのエラースパンが標準出力へ出る（無害だがログが極端に読みにくい） |

> ホスティングサーバを構築する（= `azure-ai-agentserver` を import する）時点で
> IMDS へのプローブが走るため、**ローカル検証では JSON トレースが標準出力を埋め尽くす**。
> `--fake` / `--local` / `--bench` モードはホスティングサーバを構築しないため影響を受けない。

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

> 上記の数値はスタブに対する検証であり、**モデル性能の比較ではない**。

---

## 5. 動作差分（要 Azure 展開・未計測）

`RG-Harness-Test` へ展開後に、`bench/run_compare.py` → `bench/analyze.py` の出力を貼り込む。

### 5.1 エンドツーエンド レイテンシ（warm）

| impl | n | P50 (ms) | P95 (ms) | min | max |
|---|---|---|---|---|---|
| maf | | | | | |
| copilot-sdk | | | | | |

### 5.2 コールドスタート

| impl | デプロイ直後 (ms) | アイドル後 (ms) | 備考 |
|---|---|---|---|
| maf | | | |
| copilot-sdk | | | CLI プロセス spawn の分だけ不利と予想 |

### 5.3 ステップ別レイテンシ / トークン

| impl | step | n | P50 (ms) | P95 (ms) | avg in-tok | avg out-tok |
|---|---|---|---|---|---|---|
| maf | decompose | | | | | |
| maf | solve | | | | | |
| maf | synthesize | | | | | |
| copilot-sdk | decompose | | | | | n/a |
| copilot-sdk | solve | | | | | n/a |
| copilot-sdk | synthesize | | | | | n/a |

### 5.4 出力の安定性（同一入力 5 回）

| impl | input | avg chars | sd chars | subtask 数 | avg confidence | ユニーク回答数 |
|---|---|---|---|---|---|---|
| | | | | | | |

### 5.5 障害系

| ケース | MAF | Copilot SDK |
|---|---|---|
| 空入力 | | |
| 超長文入力 | | |
| トークン失効（1 時間超の連続実行） | 該当なし（SDK が更新） | 本ハーネスは失効 5 分前に再取得。無対策なら 401 |
| モデル未デプロイ | | |

### 5.6 コスト

| impl | 総入力トークン | 総出力トークン | 推定コスト |
|---|---|---|---|
| maf | | | |
| copilot-sdk | | | |

---

## 6. リスクと対応状況

| ID | リスク | 状態 | 対応 |
|---|---|---|---|
| **R1** | Copilot SDK はプロンプトを暗号化するため、**復号対応モデルでしか BYOM が成立しない**。`gpt-5.6-terra` の可否が不明 | **未検証（最優先）** | `agents/copilot/poc_byom.py` を実装済み。3 種のプロバイダー構成を順に試して判定する。失敗時の分岐は下記 |
| **R2** | Copilot CLI ランタイム（**151.6 MB**）を実行時にダウンロードできない可能性（egress 制限 / 読み取り専用 FS） | **緩和策実装済み** | `Dockerfile` でビルド時に `download-runtime` し `COPILOT_CLI_PATH` / `COPILOT_SKIP_CLI_DOWNLOAD=1` を設定。`COPILOT_HOME` は書き込み可能パスへフォールバック |
| **R3** | `bearer_token` は静的で約 1 時間で失効し、自動更新されない | **対応済み** | `_TokenCache` が失効 5 分前に再取得。プロバイダー設定はステップごとに再構築 |
| **R4** | `agent-framework-foundry-hosting` が prerelease | **緩和済み** | 検証済みバージョンを `requirements.txt` に記録（1.0.0b260730） |
| **R5** | `azd` 拡張名がドキュメント間で `azure.ai.agents` / `microsoft.foundry` と揺れる | **未検証** | T0-1 で実機確認する |
| **R6** | `gpt-5.6-terra` のリージョン可用性・クォータ | **未検証** | モデル自体が Foundry のモデル表に存在することは確認済み。リージョンは要確認 |
| **R7** | Copilot SDK の既定 `mode="copilot-cli"` がコーディングエージェント用プロンプト／ツール／メモリを混入させる | **対応済み** | `COPILOT_CLIENT_MODE=empty` + `system_message.mode=replace` を既定にした。`empty` でどこまで素になるかは実測で確認する |
| **R8** | 権限不足（`Foundry Project Manager` / RG `Owner`） | **未検証** | 展開前に `az role assignment list` で確認 |

### R1 の分岐（PoC 失敗時）

1. **(a) 設定調整**: `wire_api` / `azure.api_version` / プロバイダー `type` の組み合わせを変える
   （`poc_byom.py` が 3 パターンを自動で試行する）
2. **(b) 制約自体を成果とする**: gpt-5 系の対応モデルを追加デプロイして Copilot SDK 版を動かし、
   「**Copilot SDK は使えるモデルが限定される**」ことを最重要の差分として報告する。
   その場合、MAF 版とはモデルが異なるためレイテンシ比較には注記を付ける
3. **(c) GitHub 既定モデル構成**: BYOM を諦めて GitHub 側のモデルを使う。
   Foundry Model を使うという前提から外れるため、最後の手段とする

---

## 7. 現時点での所見

1. **Foundry Hosted Agent への適合度は MAF が明確に上**。専用ホスティング統合があり、
   モデル・認証・テレメトリがすべて環境変数と `DefaultAzureCredential` で解決する。
   Copilot SDK は「任意のフレームワークを載せられる」枠組みに乗る形で、接着コードを自分で書く。
2. **Copilot SDK は本質的に「CLI を駆動する SDK」**であり、ライブラリではない。
   151.6 MB のネイティブランタイムが必須で、コンテナサイズ・コールドスタート・
   サンドボックスの書き込み権限すべてに影響する。サーバーレス寄りの Hosted Agent とは相性に難がある。
3. **モデルの自由度は MAF が上**。Copilot SDK には暗号化コンテンツ制約があり、
   Foundry にデプロイした任意のモデルを使えるとは限らない（R1）。
4. **一方で Copilot SDK には Copilot 固有の機能**（ツール実行、権限ハンドリング、
   メモリ、スキル、MCP、サブエージェント）が最初から載っている。
   本ハーネスはそれらを意図的に無効化して素の推論を比較しているため、
   **ツールを使うエージェントでは評価が変わりうる**点に注意。
5. 3 ステップ程度の逐次ロジックであれば、MAF の Workflow 抽象の恩恵は限定的。
   ただし分岐・並列・チェックポイント・Human-in-the-Loop まで広げると差は拡大する。

---

## 8. 次のアクション

1. `az login` → `azd version` / 拡張名の実機確認（R5）
2. `gpt-5.6-terra` のリージョン可用性とクォータ確認（R6）
3. **費用承認を取得**したうえで `RG-Harness-Test` に `azd provision`
4. `agents/copilot/poc_byom.py` を実行して **R1 を判定**（最優先）
5. 両エージェントを `azd deploy` し、`bench/run_compare.py --cold` → `--iterations 5`
6. `bench/analyze.py --out bench_result.md` の出力を §5 に貼り込み、§7 の所見を実測で更新
7. `azd down` で `RG-Harness-Test` を後片付け
