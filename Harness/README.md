# MAF vs GitHub Copilot SDK — Foundry Hosted Agent 差分検証ハーネス

同一の「Grounding ソース無し・3 ステップ推論ロジック」を
**Microsoft Agent Framework (MAF)** と **GitHub Copilot SDK** の 2 通りで実装し、
どちらも **Microsoft Foundry Agent Service の Hosted Agent** としてデプロイして、
実装差分と動作差分を比較するための最小ハーネスです。

- LLM は Foundry Model の `gpt-5.6-terra` を **1 デプロイだけ**用意し、両実装で共有します
  （モデル差を排除して「フレームワーク差」だけを観測するため）。
- リソースはすべて **`RG-Harness-Test`** に展開します。

計画の全文は [`PLAN.md`](PLAN.md) を、比較結果は [`REPORT.md`](REPORT.md) を参照してください。

---

## 1. 3 ステップの内容

| # | ステップ | 内容 | 出力 |
|---|---|---|---|
| 1 | `decompose` | 入力課題を 2〜3 個のサブ課題へ分解 | `{"subtasks": [...]}` |
| 2 | `solve` | 各サブ課題へ独立に短く回答 | `{"answers": [{subtask, answer}]}` |
| 3 | `synthesize` | 統合し、矛盾を自己検証して最終回答 | `{"final_answer", "contradictions", "confidence"}` |

ツール呼び出し・外部検索・ファイルアクセスは全ステップで禁止しています（`shared/prompts.py`）。
プロンプト文字列は両実装で **同一の定数を import** しており、差分がフレームワーク側だけに出るようにしています。

---

## 2. ディレクトリ構成

```
.
├── README.md
├── PLAN.md                      # 実装計画 (調査結果・リスク・タスク分解)
├── REPORT.md                    # 比較レポート (実測値を追記していく)
├── shared/                      # 正本。両実装が import する共通仕様
│   ├── prompts.py               #   3 ステップのプロンプト定数
│   ├── inputs.json              #   評価入力 5 件
│   └── tracing.py               #   JSONL 計測 + JSON パーサ
├── agents/
│   ├── maf/
│   │   ├── main.py              #   Workflow(3 Executor) → as_agent → ResponsesHostServer
│   │   ├── requirements.txt
│   │   └── azure.yaml.sample
│   └── copilot/
│       ├── main.py              #   CopilotClient(BYOM) → ResponsesAgentServerHost
│       ├── main_invocations.py  #   同ロジックの Invocations 版 (公式サンプル準拠)
│       ├── poc_byom.py          #   R1 検証: gpt-5.6-terra が BYOM で使えるか
│       ├── requirements.txt
│       ├── Dockerfile           #   R2 対策 (CLI ランタイム同梱)
│       └── azure.yaml.sample
├── bench/
│   ├── run_compare.py           # 両エンドポイントへ同一入力を投げて計測
│   ├── analyze.py               # JSONL → Markdown 比較表
│   └── stub_server.py           # Azure 無しでベンチ経路を検証するスタブ
├── tools/
│   └── sync_shared.py           # shared/ を agents/*/ へ複製 (deploy 前に必須)
└── logs/                        # 実行ログ JSONL (gitignore)
```

---

## 3. 前提条件

| 項目 | 要件 | 備考 |
|---|---|---|
| Python | ローカル検証は 3.11+ / Hosted Agent は **3.13+** | `azd ai agent init` の要件 |
| Azure CLI | `az login` 済み | |
| Azure Developer CLI | **1.27.1 以上** | `azd version` で確認 |
| azd 拡張 | `azd ext install microsoft.foundry` | ドキュメントに `azure.ai.agents` 表記もあるため実機で確認 |
| 権限 | 既存プロジェクト: `Foundry Project Manager` (project スコープ) / 新規: `Owner` (RG スコープ) | |
| Docker | Copilot SDK 版をコンテナデプロイする場合のみ | |

> **重要 — 環境を分けること**
> `agent-framework-github-copilot` は `github-copilot-sdk==1.0.2` を厳密ピンしています。
> MAF 版と Copilot SDK 版を**同一の venv / 同一の requirements.txt に混在させない**でください
> （混在させると Copilot SDK が 1.0.2 へ強制ダウングレードされます）。

---

## 4. ローカル検証（Azure 不要）

Azure に接続せず、3 ステップの配線とベンチ経路だけを確認できます。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r agents\maf\requirements.txt      # MAF 版を見る場合
python tools\sync_shared.py                     # shared/ を各エージェントへ複製

# 3 ステップの配線確認 (LLM 呼び出しをスタブ化)
python agents\maf\main.py --fake
python agents\copilot\main.py --fake

# ベンチ経路の確認 (別ターミナルで 2 つ起動)
python bench\stub_server.py --port 8088 --impl maf         --extra-latency 0.15
python bench\stub_server.py --port 8089 --impl copilot-sdk --extra-latency 0.35

python bench\run_compare.py `
    --maf-url     http://127.0.0.1:8088/responses `
    --copilot-url http://127.0.0.1:8089/responses `
    --iterations 2
python bench\analyze.py
```

---

## 5. Azure への展開（課金あり）

> **費用ゲート**: ここから先は `RG-Harness-Test` に実リソースを新規作成します。
> 実行前に §8 の見積りを確認してください。検証後は必ず `azd down` で片付けます。

### 5.1 R1 の事前検証（最優先）

Copilot SDK はプロンプトを暗号化して送るため、**復号に対応したモデルでしか BYOM が動きません**。
`gpt-5.6-terra` の可否が未確認のため、実装を動かす前にここで判定します。

```powershell
az login
$env:FOUNDRY_PROJECT_ENDPOINT     = "https://<account>.services.ai.azure.com/api/projects/<project>"
$env:AZURE_AI_MODEL_DEPLOYMENT_NAME = "gpt-5.6-terra"
$env:AZURE_OPENAI_ENDPOINT        = "https://<account>.openai.azure.com"   # 任意

python agents\copilot\poc_byom.py
```

- exit 0 … いずれかのプロバイダー構成で疎通成功。結果は `agents/copilot/poc_byom_result.json`
- exit 1 … 全構成失敗。`REPORT.md` の R1 分岐 (a)/(b)/(c) に従って方針を決める

### 5.2 provision と deploy

各エージェントディレクトリで個別に実行します（**別々の azd プロジェクト**になります）。

```powershell
python tools\sync_shared.py        # ← deploy 前に必ず実行 (shared/ を同梱するため)

cd agents\maf
azd ai agent init --protocol responses --deploy-mode code
azd provision                      # 初回のみ: RG-Harness-Test / Foundry / モデル / App Insights
azd ai agent run                   # ローカル起動 (http://localhost:8088)
azd ai agent invoke --local "APIのページネーションはどちらを選ぶべきか"
azd deploy                         # Hosted Agent へデプロイ
azd ai agent invoke "APIのページネーションはどちらを選ぶべきか"
```

Copilot SDK 版も同様です。ZIP デプロイで Copilot CLI ランタイムの取得に失敗する場合は、
同梱の `Dockerfile` を使うコンテナデプロイへ切り替えます。

```powershell
cd ..\copilot
azd ai agent init --protocol responses --deploy-mode code
azd deploy
```

> `azd provision` で Foundry プロジェクトを **新規作成** してください。
> 既存プロジェクトを選ぶと `azd down` がプロジェクトと RG を残すため、後片付けが漏れます。

### 5.3 モデルデプロイの確認

```powershell
az cognitiveservices account deployment list `
    --name <account> --resource-group RG-Harness-Test -o table
```

---

## 6. 比較の実行

```powershell
# デプロイ済みエンドポイントを叩く場合はトークンが必要
$env:HARNESS_BEARER = (az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)

# コールドスタート (デプロイ直後 / アイドル後に実行)
python bench\run_compare.py --cold `
    --maf-url     "<maf-agent-endpoint>/responses" `
    --copilot-url "<copilot-agent-endpoint>/responses"

# ウォーム計測 (5 入力 × 5 反復 = 実装ごとに 25 サンプル)
python bench\run_compare.py --iterations 5 `
    --maf-url     "<maf-agent-endpoint>/responses" `
    --copilot-url "<copilot-agent-endpoint>/responses"

# 集計して REPORT.md へ貼る
python bench\analyze.py --out bench_result.md
```

`run_compare.py` はクライアント側の E2E レイテンシに加えて、
応答 JSON に同梱された `trace`（ステップ別レイテンシ・トークン）も収集します。
Hosted Agent のコンテナ内 JSONL は取り出せないため、この同梱トレースが唯一のステップ別データ源です。

---

## 7. 環境変数

| 変数 | 用途 | 対象 |
|---|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Foundry プロジェクトのエンドポイント（Hosted Agent が自動注入） | 両方 |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | モデルデプロイ名（自動注入） | 両方 |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | テレメトリ接続文字列（自動注入） | 両方 |
| `HARNESS_LOG_DIR` | JSONL 出力先。未設定なら `$HOME/harness-logs` | 両方 |
| `HARNESS_PROTOCOL` | ログに記録するプロトコル名 | 両方 |
| `HARNESS_STEP_TIMEOUT` | 1 ステップのタイムアウト秒（既定 120） | Copilot |
| `COPILOT_PROVIDER_TYPE` | `openai`（既定）/ `azure` | Copilot |
| `AZURE_OPENAI_ENDPOINT` | `type=azure` のときのホスト。**パスを付けない** | Copilot |
| `COPILOT_API_VERSION` | `type=azure` のときの api_version | Copilot |
| `COPILOT_CLIENT_MODE` | `empty`（既定）/ `copilot-cli` | Copilot |
| `COPILOT_SYSTEM_MESSAGE_MODE` | `replace`（既定）/ `append` | Copilot |
| `COPILOT_HOME` | Copilot CLI の状態書き込み先 | Copilot |
| `COPILOT_CLI_PATH` / `COPILOT_SKIP_CLI_DOWNLOAD` | 同梱ランタイムを使う場合 | Copilot |
| `HARNESS_BEARER` | bench がデプロイ済みエンドポイントを叩く際の Bearer | bench |

---

## 8. 費用と後片付け

| リソース | 課金 | 概算 |
|---|---|---|
| Foundry account / project | 基本無料 | $0 |
| `gpt-5.6-terra` デプロイ | 従量（トークン） | 検証規模（50 実行 × 3 ステップ）で数 USD 未満 |
| Application Insights | 取り込み量従量 | 数 USD/月未満 |
| Azure Container Registry (Basic) | 約 $0.17/日 | コンテナデプロイ時のみ |
| Hosted Agent 実行基盤 | 実行時間従量 | 検証規模では小 |

```powershell
cd agents\maf     ; azd down
cd ..\copilot     ; azd down
```

`azd down` は **その azd 環境が作成した** RG を削除します。
既存プロジェクトを選択していた場合はリソースが残るため、`RG-Harness-Test` を直接確認してください。

```powershell
az group show --name RG-Harness-Test -o table   # 残っていないか確認
```

---

## 9. 参考ドキュメント

- Foundry Hosted Agents (概念): https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
- 自前コードを Hosted Agent としてデプロイ: https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-deploy-own-code
- ソースコード (ZIP) デプロイ: https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-code
- コンテナデプロイ: https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent
- Agent Framework の Foundry ホスティング統合: https://learn.microsoft.com/agent-framework/hosting/foundry-hosted-agent
- GitHub Copilot SDK (Python): https://github.com/github/copilot-sdk/tree/main/python
