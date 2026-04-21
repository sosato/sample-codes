"""
=============================================================================
inspect_content_filter.py — Foundry Agent 応答の content_filter_results 検査
=============================================================================

inspect_annotations.py をベースに、Foundry Agent (Responses API +
agent_reference) のレスポンスおよびエラー本文から content_filter / prompt_filter
関連の情報のみを抽出して整形表示します。

ガードレール (Guardrails) によりブロックされた場合、
  - 成功レスポンスでは status=incomplete, finish_reason="content_filter"
  - エラーレスポンスでは BadRequestError(400) の本文に content_filter_result
が含まれます。本スクリプトは両方を捕捉します。

参照: foundry-agent-guardrails.md §4.2

実行方法:
  pip install -r requirements.txt
  az login
  python inspect_content_filter.py
"""

import json
import re
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

# openai 例外は Responses API 経由で発生する可能性があるためインポート
try:
    from openai import APIStatusError, BadRequestError
except Exception:  # pragma: no cover
    APIStatusError = Exception  # type: ignore
    BadRequestError = Exception  # type: ignore


# =============================================================================
# ★ ここを書き換えてください ★
# =============================================================================

ENDPOINT = ENDPOINT = "https://FOUNDRYNAME.services.ai.azure.com/api/projects/PRJNAME"
AGENT_NAME = "my-agent"
AGENT_VERSION = "1"

# ガードレール検証用にここを書き換える。
# 例: 暴力的、ヘイト、ジェイルブレイク誘発などの文言で content_filter を発火させる。
USER_MESSAGE = "あなたは何が出来ますか？"

MAX_APPROVAL_ROUNDS = 5  # MCP 承認ループの上限


# =============================================================================
# Unicode デコードユーティリティ (ベース流用)
# =============================================================================
def _decode_unicode_escapes(s):
    def _replace(m):
        return chr(int(m.group(1), 16))
    return re.sub(r"\\u([0-9a-fA-F]{4})", _replace, s)


def _decode_recursive(obj):
    if isinstance(obj, str):
        return _decode_unicode_escapes(obj)
    elif isinstance(obj, dict):
        return {k: _decode_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decode_recursive(v) for v in obj]
    return obj


# =============================================================================
# MCP 自動承認 (ベース流用)
# =============================================================================
def build_approvals(output_items):
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
# Content Filter 抽出ロジック
# =============================================================================
# 探索対象キー (Azure OpenAI / Foundry のレスポンス・エラー本文に登場するもの)
FILTER_KEYS = {
    "content_filter_results",
    "prompt_filter_results",
    "content_filter_result",   # エラー本文側 (innererror.content_filter_result)
}


def _walk(obj, path=""):
    """dict/list を再帰走査して (path, key, value) を yield。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{path}.{k}" if path else k
            yield child_path, k, v
            yield from _walk(v, child_path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            child_path = f"{path}[{i}]"
            yield from _walk(v, child_path)


def extract_filter_findings(root):
    """
    レスポンス／エラー本文を再帰走査し、content_filter 関連を全件収集。
    返り値:
      {
        "filter_blocks":   [{"path": str, "key": str, "value": dict|list}, ...],
        "finish_reasons":  [{"path": str, "value": str}, ...],
        "status":          str | None,
        "incomplete_reason": str | None,
        "error":           {"code": str, "message": str, "raw": ...} | None,
      }
    """
    findings = {
        "filter_blocks": [],
        "finish_reasons": [],
        "status": None,
        "incomplete_reason": None,
        "error": None,
    }

    if not isinstance(root, (dict, list)):
        return findings

    # トップレベル status / incomplete_details / error
    if isinstance(root, dict):
        if "status" in root and isinstance(root["status"], str):
            findings["status"] = root["status"]
        incomplete = root.get("incomplete_details")
        if isinstance(incomplete, dict):
            findings["incomplete_reason"] = incomplete.get("reason")
        err = root.get("error")
        if isinstance(err, dict):
            findings["error"] = {
                "code": err.get("code"),
                "message": err.get("message"),
                "raw": err,
            }

    # 再帰走査
    for path, key, value in _walk(root):
        if key in FILTER_KEYS and isinstance(value, (dict, list)):
            findings["filter_blocks"].append({
                "path": path,
                "key": key,
                "value": value,
            })
        elif key == "finish_reason" and isinstance(value, str):
            findings["finish_reasons"].append({"path": path, "value": value})

    return findings


# =============================================================================
# 整形プリンタ
# =============================================================================
def _fmt_category(name, info):
    """
    info は dict (例: {"filtered": true, "severity": "medium"}) または
    {"detected": false, "filtered": false} などのバリエーションがある。
    """
    if not isinstance(info, dict):
        return f"      {name}: {info!r}"
    filtered = info.get("filtered")
    severity = info.get("severity")
    detected = info.get("detected")

    parts = []
    if filtered is not None:
        parts.append(f"filtered={'YES' if filtered else 'no '}")
    if severity is not None:
        parts.append(f"severity={severity}")
    if detected is not None:
        parts.append(f"detected={'YES' if detected else 'no '}")
    # 想定外フィールドも残す
    extras = {k: v for k, v in info.items()
              if k not in ("filtered", "severity", "detected")}
    if extras:
        parts.append(f"extra={json.dumps(extras, ensure_ascii=False)}")

    flag = "🚫" if (filtered is True or detected is True) else "  "
    return f"      {flag} {name:30s}  " + "  ".join(parts)


def print_filter_block(block):
    path = block["path"]
    key = block["key"]
    value = block["value"]
    print(f"\n  ┌─ [{key}] @ {path}")
    if isinstance(value, list):
        # prompt_filter_results は list of {prompt_index, content_filter_results}
        for i, item in enumerate(value):
            print(f"  │  list[{i}]:")
            if isinstance(item, dict):
                idx = item.get("prompt_index")
                if idx is not None:
                    print(f"  │    prompt_index={idx}")
                inner = item.get("content_filter_results", item)
                if isinstance(inner, dict):
                    for cat, info in inner.items():
                        if cat == "prompt_index":
                            continue
                        print(_fmt_category(cat, info))
                else:
                    print(f"  │    {item!r}")
            else:
                print(f"  │    {item!r}")
    elif isinstance(value, dict):
        for cat, info in value.items():
            print(_fmt_category(cat, info))
    print(f"  └─")


def print_summary(findings, title):
    print("=" * 70)
    print(title)
    print("=" * 70)
    print(f"  status            : {findings['status']}")
    print(f"  incomplete_reason : {findings['incomplete_reason']}")
    if findings["finish_reasons"]:
        print(f"  finish_reasons    :")
        for fr in findings["finish_reasons"]:
            mark = "🚫" if fr["value"] == "content_filter" else "  "
            print(f"    {mark} {fr['value']}  @ {fr['path']}")
    else:
        print(f"  finish_reasons    : (なし)")
    if findings["error"]:
        err = findings["error"]
        mark = "🚫" if err.get("code") == "content_filter" else "⚠️ "
        print(f"  error             : {mark} code={err.get('code')!r}")
        print(f"                      message={err.get('message')!r}")
    else:
        print(f"  error             : (なし)")
    print()
    blocks = findings["filter_blocks"]
    print(f"  filter_blocks     : {len(blocks)} 件")
    for b in blocks:
        print_filter_block(b)
    if not blocks:
        print("  (content_filter_results / prompt_filter_results は見つかりませんでした)")


# =============================================================================
# 例外本文 → JSON dict に正規化
# =============================================================================
def error_to_dict(exc):
    """openai 例外から JSON 本文を取り出す。失敗したら str 化して返す。"""
    # 1) .response.json()
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            return resp.json()
        except Exception:
            pass
        try:
            text = getattr(resp, "text", None)
            if text:
                return json.loads(text)
        except Exception:
            pass
    # 2) .body
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            return {"error": {"code": "unknown", "message": body}}
    # 3) フォールバック
    return {"error": {"code": type(exc).__name__, "message": str(exc)}}


# =============================================================================
# メイン処理
# =============================================================================
def main():
    print("=" * 70)
    print("Foundry Agent Content Filter Inspector (agent_reference)")
    print("=" * 70)
    print(f"  Endpoint      : {ENDPOINT}")
    print(f"  Agent Name    : {AGENT_NAME}")
    print(f"  Agent Version : {AGENT_VERSION}")
    print(f"  Message       : {USER_MESSAGE}")
    print()

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

    data = None
    error_payload = None

    # --- 初回 Responses API 実行 ---
    print("[1] Responses API (agent_reference) 実行中...")
    try:
        response = openai_client.responses.create(
            input=[{"role": "user", "content": USER_MESSAGE}],
            extra_body=agent_ref,
        )
        data = response.model_dump(mode="json")
        print(f"    ✅ id={response.id}  status={data.get('status')}")
    except (BadRequestError, APIStatusError) as exc:
        error_payload = error_to_dict(exc)
        status_code = getattr(exc, "status_code", None)
        print(f"    🚫 API エラー status_code={status_code}")
        print(f"       (エラー本文を解析対象に切り替えます)")
    except Exception as exc:
        error_payload = {
            "error": {
                "code": type(exc).__name__,
                "message": str(exc),
            }
        }
        print(f"    🚫 想定外の例外: {type(exc).__name__}: {exc}")

    # --- 承認ループ (成功時のみ) ---
    if data is not None:
        for round_num in range(1, MAX_APPROVAL_ROUNDS + 1):
            approvals = build_approvals(data.get("output", []))
            if not approvals:
                break
            print(f"\n[1-{round_num}] mcp_approval_request {len(approvals)} 件を自動承認中...")
            for ap in approvals:
                print(f"    → approve: {ap['approval_request_id']}")
            try:
                response = openai_client.responses.create(
                    input=approvals,
                    extra_body={
                        **agent_ref,
                        "previous_response_id": data["id"],
                    },
                )
                data = response.model_dump(mode="json")
                print(f"    ✅ id={response.id}  status={data.get('status')}")
            except (BadRequestError, APIStatusError) as exc:
                error_payload = error_to_dict(exc)
                status_code = getattr(exc, "status_code", None)
                print(f"    🚫 承認ラウンド中に API エラー status_code={status_code}")
                data = None
                break
        else:
            if data and build_approvals(data.get("output", [])):
                print(f"\n⚠️  {MAX_APPROVAL_ROUNDS} ラウンド後もまだ承認要求あり。打ち切り。")

    print()

    # --- JSON 保存 ---
    if data is not None:
        with open("_filter_response_full.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        with open("_filter_response_decoded.json", "w", encoding="utf-8") as f:
            json.dump(_decode_recursive(data), f, indent=2, ensure_ascii=False, default=str)
        print("[2] 最終レスポンスを保存")
        print("    raw     : _filter_response_full.json")
        print("    decoded : _filter_response_decoded.json")
    if error_payload is not None:
        with open("_filter_error.json", "w", encoding="utf-8") as f:
            json.dump(error_payload, f, indent=2, ensure_ascii=False, default=str)
        print("[2] エラー本文を保存")
        print("    error   : _filter_error.json")
    print()

    # --- フィルター抽出 & 表示 ---
    if data is not None:
        findings = extract_filter_findings(data)
        print_summary(findings, "[3] 成功レスポンスの content_filter サマリ")
        print()

    if error_payload is not None:
        findings_err = extract_filter_findings(error_payload)
        print_summary(findings_err, "[4] エラー本文の content_filter サマリ")
        print()

    print("=" * 70)
    print("完了")
    print("=" * 70)


if __name__ == "__main__":
    main()
