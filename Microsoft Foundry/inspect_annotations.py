"""
=============================================================================
inspect_annotations.py — Foundry Agent 応答の annotations インスペクター
=============================================================================

Foundry Portal の Playground と同じ方式（agent_reference）で Responses API を
呼び出し、レスポンス全体を保存・出力して annotations の構造を確認します。

MCP ツールの require_approval="always" に対応するため、mcp_approval_request を
検知したら自動承認して会話を継続します。

実行方法:
  pip install -r requirements.txt
  az login
  python inspect_annotations.py
"""

import json
import re
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient


def _decode_unicode_escapes(s):
    """文字列中の \\uXXXX パターンを実際の Unicode 文字に変換する。"""
    def _replace(m):
        return chr(int(m.group(1), 16))
    return re.sub(r"\\u([0-9a-fA-F]{4})", _replace, s)


def _decode_recursive(obj):
    """JSON オブジェクトを再帰的に走査し、文字列中の \\uXXXX をデコードする。"""
    if isinstance(obj, str):
        return _decode_unicode_escapes(obj)
    elif isinstance(obj, dict):
        return {k: _decode_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decode_recursive(v) for v in obj]
    return obj


# =============================================================================
# ★ ここを書き換えてください ★
# =============================================================================
ENDPOINT = ENDPOINT = "https://FOUNDRYNAME.services.ai.azure.com/api/projects/PRJNAME"
AGENT_NAME = "my-agent"
AGENT_VERSION = "1"
USER_MESSAGE = "あなたは何が出来ますか？"

MAX_APPROVAL_ROUNDS = 5  # 承認ループの上限


# =============================================================================
# ヘルパー: mcp_approval_request を探して承認入力を作る
# =============================================================================
def build_approvals(output_items):
    """output 内の mcp_approval_request を全て approve する入力リストを返す。"""
    approvals = []
    for item in output_items:
        if item.get("type") == "mcp_approval_request":
            approvals.append({
                "type": "mcp_approval_response",
                "approve": True,
                "approval_request_id": item["id"],
            })
    return approvals


# =============================================================================
# ヘルパー: Output アイテムの詳細表示
# =============================================================================
def print_output_items(output_items):
    print("=" * 70)
    print(f"Output アイテム一覧 ({len(output_items)} 件)")
    print("=" * 70)

    for i, item in enumerate(output_items):
        item_type = item.get("type", "?")
        print(f"\n{'─' * 70}")
        print(f"Output [{i}]  type={item_type}")
        print(f"{'─' * 70}")

        if item_type == "message":
            contents = item.get("content", [])
            print(f"  content items: {len(contents)}")
            for j, content in enumerate(contents):
                ct = content.get("type", "?")
                print(f"\n  content[{j}] type={ct}")
                if ct == "output_text":
                    text = content.get("text", "")
                    annotations = content.get("annotations", [])
                    print(f"  text (先頭500文字):")
                    print(f"    {text[:500]}")
                    print(f"\n  ★ annotations ({len(annotations)} 件):")
                    for k, ann in enumerate(annotations):
                        print(f"    annotation[{k}]:")
                        print(json.dumps(ann, indent=4, ensure_ascii=False))
                    if not annotations:
                        print("    (なし)")
                else:
                    print(f"  raw: {json.dumps(content, ensure_ascii=False)[:500]}")
        else:
            print(json.dumps(item, indent=2, ensure_ascii=False)[:2000])


# =============================================================================
# メイン処理
# =============================================================================
def main():
    print("=" * 70)
    print("Foundry Agent Annotation Inspector (agent_reference)")
    print("=" * 70)
    print(f"  Endpoint      : {ENDPOINT}")
    print(f"  Agent Name    : {AGENT_NAME}")
    print(f"  Agent Version : {AGENT_VERSION}")
    print(f"  Message       : {USER_MESSAGE}")
    print()

    # --- 認証 & クライアント ---
    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=ENDPOINT, credential=credential)
    openai_client = project_client.get_openai_client()

    agent_ref = {
        "agent_reference": {
            "name": AGENT_NAME,
            "version": AGENT_VERSION,
            "type": "agent_reference",
        }
    }

    # --- 初回 Responses API 実行 ---
    print("[1] Responses API (agent_reference) 実行中...")
    response = openai_client.responses.create(
        input=[{"role": "user", "content": USER_MESSAGE}],
        extra_body=agent_ref,
    )
    data = response.model_dump(mode="json")
    print(f"    ✅ id={response.id}  status={data.get('status')}")

    # --- 承認ループ ---
    for round_num in range(1, MAX_APPROVAL_ROUNDS + 1):
        approvals = build_approvals(data.get("output", []))
        if not approvals:
            break

        print(f"\n[1-{round_num}] mcp_approval_request {len(approvals)} 件を自動承認中...")
        for ap in approvals:
            print(f"    → approve: {ap['approval_request_id']}")

        response = openai_client.responses.create(
            input=approvals,
            extra_body={
                **agent_ref,
                "previous_response_id": data["id"],
            },
        )
        data = response.model_dump(mode="json")
        print(f"    ✅ id={response.id}  status={data.get('status')}")
    else:
        if build_approvals(data.get("output", [])):
            print(f"\n⚠️  {MAX_APPROVAL_ROUNDS} ラウンド後もまだ承認要求あり。打ち切り。")

    print()

    # --- 全レスポンス保存 (raw + デコード済み) ---
    output_file = "_response_full.json"
    decoded_file = "_response_decoded.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    decoded = _decode_recursive(data)
    with open(decoded_file, "w", encoding="utf-8") as f:
        json.dump(decoded, f, indent=2, ensure_ascii=False, default=str)
    print(f"[2] 最終レスポンスを保存")
    print(f"    raw     : {output_file}")
    print(f"    decoded : {decoded_file}")
    print()

    # --- output_text 表示 ---
    print("[3] output_text:")
    print("-" * 70)
    print(response.output_text)
    print("-" * 70)
    print()

    # --- Output アイテム詳細出力 ---
    print_output_items(data.get("output", []))

    print()
    print("=" * 70)
    print("完了")
    print("=" * 70)


if __name__ == "__main__":
    main()
