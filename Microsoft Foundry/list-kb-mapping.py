"""
Foundry IQ Knowledge Base ↔ Agent マッピング一括取得スクリプト

Azure AI Foundry の Agent 一覧と Azure AI Search の Knowledge Base 一覧を
REST API で取得し、KB がどのエージェントに紐づいているかをクロスリファレンスします。

前提条件:
  - az login 済みであること
  - 対象の Foundry プロジェクト / AI Search サービスへのアクセス権があること

使い方:
  python list_kb_agent_mapping.py
"""

import json
import subprocess
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# ── 設定 ────────────────────────────────────────────────────
# 環境に併せて変更してください
FOUNDRY_ENDPOINT = "https://FOUNDRYNAME.services.ai.azure.com"
FOUNDRY_PROJECT = "PRJNAME"
AI_SEARCH_ENDPOINT = "https://AISNAME.search.windows.net"
AI_SEARCH_API_VERSION = "2025-11-01-Preview"
OUTPUT_DIR = Path(__file__).parent
# ────────────────────────────────────────────────────────────


def get_access_token(resource: str) -> str:
    """az CLI でアクセストークンを取得"""
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True, shell=True,
    )
    return result.stdout.strip()


def api_get(url: str, token: str) -> dict:
    """Bearer トークン付き GET リクエスト"""
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(req) as resp:
        return json.loads(resp.read().decode())


def extract_kb_name_from_url(server_url: str) -> str | None:
    """MCP server_url から KB 名を抽出
    例: .../knowledgebases/knowledgebase216/mcp?... → knowledgebase216
    """
    m = re.search(r"/knowledgebases/([^/]+)/", server_url)
    return m.group(1) if m else None


def fetch_agents(token: str) -> list[dict]:
    """Foundry プロジェクトの全エージェントを取得"""
    url = f"{FOUNDRY_ENDPOINT}/api/projects/{FOUNDRY_PROJECT}/agents?api-version=v1"
    data = api_get(url, token)
    return data.get("data", [])


def fetch_knowledge_bases(token: str) -> list[dict]:
    """Azure AI Search の全 Knowledge Base を取得"""
    url = f"{AI_SEARCH_ENDPOINT}/knowledgebases?api-version={AI_SEARCH_API_VERSION}"
    data = api_get(url, token)
    return data.get("value", [])


def build_mapping(agents: list[dict], kbs: list[dict]) -> dict:
    """KB → Agent / Agent → KB の双方向マッピングを構築"""
    kb_to_agents: dict[str, list[dict]] = {}
    agent_to_kbs: dict[str, list[dict]] = {}

    # KB 一覧を辞書化
    kb_info = {}
    for kb in kbs:
        kb_info[kb["name"]] = {
            "name": kb["name"],
            "description": kb.get("description", ""),
            "knowledgeSources": [ks["name"] for ks in kb.get("knowledgeSources", [])],
            "model": kb.get("models", [{}])[0]
                      .get("azureOpenAIParameters", {})
                      .get("modelName", "N/A"),
        }
        kb_to_agents[kb["name"]] = []

    # エージェント → KB マッピング
    for agent in agents:
        agent_id = agent["id"]
        agent_name = agent["name"]
        latest = agent.get("versions", {}).get("latest", {})
        definition = latest.get("definition", {})
        tools = definition.get("tools", [])
        model = definition.get("model", "N/A")
        status = latest.get("status", "unknown")

        connected_kbs = []
        for tool in tools:
            if tool.get("type") == "mcp" and "knowledgebases" in tool.get("server_url", ""):
                kb_name = extract_kb_name_from_url(tool["server_url"])
                if kb_name:
                    connected_kbs.append({
                        "kb_name": kb_name,
                        "connection_id": tool.get("project_connection_id", ""),
                        "server_url": tool.get("server_url", ""),
                    })
                    # KB → Agent にも追加
                    if kb_name not in kb_to_agents:
                        kb_to_agents[kb_name] = []
                    kb_to_agents[kb_name].append({
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "model": model,
                        "status": status,
                    })

        agent_to_kbs[agent_id] = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "model": model,
            "status": status,
            "connected_kbs": connected_kbs,
        }

    # KB 一覧のうちどのエージェントにも紐づいていないものを明示
    orphan_kbs = [name for name, agents_list in kb_to_agents.items() if not agents_list]

    return {
        "kb_to_agents": kb_to_agents,
        "agent_to_kbs": agent_to_kbs,
        "kb_info": kb_info,
        "orphan_kbs": orphan_kbs,
    }


def format_summary(mapping: dict, agents_raw: list, kbs_raw: list) -> str:
    """人間が読みやすいサマリーテキストを生成"""
    lines = []
    lines.append("=" * 72)
    lines.append("  Foundry IQ: Knowledge Base ↔ Agent マッピングレポート")
    lines.append(f"  生成日時: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"  Foundry: {FOUNDRY_ENDPOINT}/api/projects/{FOUNDRY_PROJECT}")
    lines.append(f"  AI Search: {AI_SEARCH_ENDPOINT}")
    lines.append("=" * 72)

    lines.append(f"\n■ 概要: エージェント {len(agents_raw)} 件 / KB {len(kbs_raw)} 件\n")

    # ── KB → Agent ──
    lines.append("-" * 72)
    lines.append("■ Knowledge Base → 紐づきエージェント")
    lines.append("-" * 72)
    for kb_name, agents_list in mapping["kb_to_agents"].items():
        kb = mapping["kb_info"].get(kb_name, {})
        desc = kb.get("description", "") or "(説明なし)"
        sources = ", ".join(kb.get("knowledgeSources", [])) or "N/A"
        lines.append(f"\n  KB: {kb_name}")
        lines.append(f"    説明: {desc}")
        lines.append(f"    Knowledge Sources: {sources}")
        lines.append(f"    検索モデル: {kb.get('model', 'N/A')}")
        if agents_list:
            for a in agents_list:
                lines.append(f"    → Agent: {a['agent_name']}  (model={a['model']}, status={a['status']})")
        else:
            lines.append(f"    → ⚠ 紐づきエージェントなし (未使用)")

    # ── Agent → KB ──
    lines.append(f"\n{'─' * 72}")
    lines.append("■ エージェント → 紐づき Knowledge Base")
    lines.append("─" * 72)
    for agent_id, info in mapping["agent_to_kbs"].items():
        lines.append(f"\n  Agent: {info['agent_name']}  (id={agent_id})")
        lines.append(f"    モデル: {info['model']}  ステータス: {info['status']}")
        if info["connected_kbs"]:
            for kb in info["connected_kbs"]:
                lines.append(f"    → KB: {kb['kb_name']}  (connection={kb['connection_id']})")
        else:
            lines.append(f"    → KB 接続なし")

    # ── 未使用 KB ──
    if mapping["orphan_kbs"]:
        lines.append(f"\n{'─' * 72}")
        lines.append("■ ⚠ 未使用 Knowledge Base (どのエージェントにも紐づいていない)")
        lines.append("─" * 72)
        for name in mapping["orphan_kbs"]:
            lines.append(f"  - {name}")

    lines.append(f"\n{'=' * 72}")
    return "\n".join(lines)


def main():
    print("🔑 Azure トークン取得中...")
    foundry_token = get_access_token("https://ai.azure.com")
    search_token = get_access_token("https://search.azure.com")

    print("📡 エージェント一覧取得中...")
    agents = fetch_agents(foundry_token)
    print(f"   → {len(agents)} 件のエージェントを取得")

    print("📡 Knowledge Base 一覧取得中...")
    kbs = fetch_knowledge_bases(search_token)
    print(f"   → {len(kbs)} 件の KB を取得")

    print("🔗 マッピング構築中...")
    mapping = build_mapping(agents, kbs)

    # JSON 出力
    json_path = OUTPUT_DIR / "kb_agent_mapping.json"
    output_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "foundry_endpoint": f"{FOUNDRY_ENDPOINT}/api/projects/{FOUNDRY_PROJECT}",
        "ai_search_endpoint": AI_SEARCH_ENDPOINT,
        "summary": {
            "total_agents": len(agents),
            "total_kbs": len(kbs),
            "orphan_kbs": mapping["orphan_kbs"],
        },
        "kb_to_agents": mapping["kb_to_agents"],
        "agent_to_kbs": mapping["agent_to_kbs"],
        "kb_info": mapping["kb_info"],
    }
    json_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 JSON 出力: {json_path}")

    # テキストサマリー出力
    txt_path = OUTPUT_DIR / "kb_agent_mapping_report.txt"
    summary = format_summary(mapping, agents, kbs)
    txt_path.write_text(summary, encoding="utf-8")
    print(f"📄 レポート出力: {txt_path}")

    # コンソールにもサマリー表示
    print("\n" + summary)


if __name__ == "__main__":
    main()
