# Resilient Trip Advisor デモ実演手順

主画面は Web UI です。

`https://<container-app-fqdn>/`

Web UI の詳細は `WEB-UI-RUNBOOK.md` を参照してください。

## 起動

```powershell
Set-Location C:\CLI\Git\sample-codes\foundry-longrunning-demo
$endpoint = azd env get-value AGENT_LANGCHAIN_AZURE_RESILIENT_RESPONSES_STEERABLE_RESPONSES_ENDPOINT
Set-Location .\client
python .\client.py --url $endpoint --auth
```

## 画面構成

- **デモ操作**: 定型シナリオを 1 クリックで開始
- **ステータス**: 開始、streaming、承認待ち、復旧、完了を表示
- **会話領域**: Agent の途中経過、tool 実行、予約結果を表示
- **承認バー**: `book_trip` の実行前だけ表示
- **composer**: 実行中に追加指示を送ると steering
- **Raw events**: `Ctrl+R` で response ID と SSE event を表示

## シナリオ A: 出張検索から予約承認

1. **1. タイ家族旅行**を押す。
2. Web 検索と旅程作成の途中経過を確認する。
3. フライト、ホテル、アクティビティ、日別旅程、出典を確認する。
4. 予約を明示的に依頼した場合だけ approval UI を表示する。

期待値:

- 実在候補のみを提示
- 価格・空席は検索時点の目安と明記
- 家族構成と休憩を考慮
- 出典 URL を表示

## シナリオ B: 実行中の方向転換

1. **1. タイ家族旅行**を押す。
2. streaming 中に **2. 条件変更**を押す。
3. active turn が安全に終了し、queued turn が開始されることを確認する。
4. BTS 駅直結ホテルと午後休憩を優先した旅程へ変わることを確認する。

説明ポイント:

- 2 つの handler を並列実行する機能ではない。
- conversation history は線形。
- 新しい turn が queue に入り、current turn は cooperative cancellation される。

## シナリオ C: クラッシュ後の復旧

1. **3. 障害復旧**を押す。
2. Agent が `simulate_crash` を実行する。
3. status が `障害を検知 — 永続 checkpoint から復旧中...` に変わる。
4. client は新しい request を作らず、同じ response ID を retrieve する。
5. 最後の SSE cursor 以降を replay し、処理が `completed` になる。

期待される最終メッセージ:

```text
チェックポイントから再開でき、障害復旧デモは成功しました。
```

## シナリオ D: 却下

1. 予約を含む旅行依頼を入力する。
2. approval card で **却下**を押す。
3. `book_trip` が実行されず、Agent が却下結果を説明することを確認する。
4. composer から予算や条件を変更して再提案を依頼する。

## 自動 feasibility check

```powershell
Set-Location C:\CLI\Git\sample-codes\foundry-longrunning-demo
$endpoint = azd env get-value AGENT_LANGCHAIN_AZURE_RESILIENT_RESPONSES_STEERABLE_RESPONSES_ENDPOINT
python .\client\feasibility_probe.py `
  --url $endpoint `
  --out .\feasibility-results-v3.json `
  --timeout 420
```

確認済み結果:

| Check | Result |
|---|---|
| Human-in-the-loop approval | PASS |
| Booking confirmation | PASS |
| Steering | PASS |
| Crash recovery | PASS |
| Cursor reconnect | PASS |
| Agent version | `6` |
| Agent status | `active` |

## Headless UI smoke test

```powershell
python .\client\ui_smoke_test.py
```

このテストは Textual UI を仮想 terminal で mount し、**1. タイ家族旅行**ボタンが定型プロンプトを送信できることを検証します。

## リソース

- Resource group: `<resource-group>`
- Foundry project: `<foundry-project>`
- Agent: `langchain-azure-resilient-responses-steerable`
- Deployed version: `<current-version>`
- Model: `gpt-5.6-sol`
- Protocol: Responses `2.0.0`

リソースは保持します。現在は削除しません。

## 再デプロイ時の注意

Hosted Agent の既存 session は作成時の agent version に pin されます。新しい version を deploy した直後に旧 session が active の場合、新 version の検証が旧 container に滞留することがあります。

検証前に session 一覧を確認し、旧 version の active session が残っている場合は filesystem を保持したまま stop します。

```powershell
azd ai agent sessions list --output table
azd ai agent sessions stop <old-session-id>
```
