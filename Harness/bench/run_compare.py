"""比較ドライバ: 両 Hosted Agent へ同一入力を投げて計測を収集する。

標準ライブラリのみで動作する (エージェント側の依存を持ち込まないため)。

計測は 2 層で取る:
  1. **クライアント側**: E2E レイテンシ / HTTP ステータス / 応答サイズ
  2. **サーバ側**: 応答 JSON の ``trace`` (ステップ別レイテンシ・トークン)
     — Hosted Agent のコンテナ内 JSONL は取り出せないため応答に同梱している

使い方
------
ローカル (`azd ai agent run` を 2 つ別ポートで起動している場合)::

    python bench/run_compare.py --maf-url http://localhost:8088/responses \
                                --copilot-url http://localhost:8089/responses \
                                --iterations 5

デプロイ済みエンドポイント (Entra トークンが必要)::

    $env:HARNESS_BEARER = (az account get-access-token `
        --resource https://ai.azure.com --query accessToken -o tsv)
    python bench/run_compare.py --maf-url <agent-endpoint>/responses ...

コールドスタート計測::

    python bench/run_compare.py --cold --maf-url ... --copilot-url ...
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "shared" / "inputs.json"
DEFAULT_OUT = ROOT / "logs" / "bench.jsonl"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _post(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, str, float]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("HARNESS_BEARER")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return response.status, text, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, text, (time.perf_counter() - started) * 1000
    except Exception as exc:  # noqa: BLE001 - ネットワーク失敗も 1 サンプルとして残す
        return 0, f"{type(exc).__name__}: {exc}", (time.perf_counter() - started) * 1000


def _request_payload(protocol: str, text: str) -> dict[str, Any]:
    if protocol == "invocations":
        return {"message": text}
    return {"input": text}


def _extract_agent_result(protocol: str, raw: str) -> dict[str, Any]:
    """エージェントの応答本文から 3 ステップ結果 JSON を取り出す。

    Responses プロトコルは OpenAI 互換のため本文が入れ子になる。
    形が変わっても落とさないよう、複数の位置を順に探す。
    """
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw[:2000]}

    if not isinstance(body, dict):
        return {"_raw": raw[:2000]}

    if protocol == "invocations":
        return body

    # 1) output_text ショートカット
    candidates: list[str] = []
    if isinstance(body.get("output_text"), str):
        candidates.append(body["output_text"])

    # 2) output[].content[].text
    for item in body.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                candidates.append(content["text"])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    # 3) すでに 3 ステップ結果そのものが返っている場合
    if "final_answer" in body or "trace" in body:
        return body
    return {"_raw": raw[:2000]}


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------
def _load_inputs() -> list[dict[str, str]]:
    return json.loads(INPUTS.read_text(encoding="utf-8"))["inputs"]


def _sample(
    *,
    impl: str,
    url: str,
    protocol: str,
    input_id: str,
    text: str,
    iteration: int,
    timeout: float,
    phase: str,
) -> dict[str, Any]:
    status, raw, latency_ms = _post(url, _request_payload(protocol, text), timeout)
    result = _extract_agent_result(protocol, raw) if status == 200 else {"_raw": raw[:2000]}
    final_answer = result.get("final_answer", "")
    return {
        "kind": "bench",
        "phase": phase,
        "impl": impl,
        "protocol": protocol,
        "url": url,
        "input_id": input_id,
        "iteration": iteration,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "http_status": status,
        "ok": status == 200 and bool(final_answer),
        "e2e_latency_ms": round(latency_ms, 2),
        "run_id": result.get("run_id"),
        "final_answer_chars": len(final_answer) if isinstance(final_answer, str) else 0,
        "final_answer": final_answer if isinstance(final_answer, str) else "",
        "confidence": result.get("confidence"),
        "contradictions": result.get("contradictions", []),
        "subtask_count": len(
            (result.get("steps", {}).get("decompose", {}) or {}).get("subtasks", []) or []
        ),
        "trace": result.get("trace", []),
        "error": None if status == 200 else raw[:500],
    }


def _append(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def run(args: argparse.Namespace) -> int:
    targets: list[tuple[str, str]] = []
    if args.maf_url:
        targets.append(("maf", args.maf_url))
    if args.copilot_url:
        targets.append(("copilot-sdk", args.copilot_url))
    if not targets:
        print("ERROR: --maf-url か --copilot-url を少なくとも 1 つ指定してください。")
        return 2

    dataset = _load_inputs()
    out = Path(args.out)
    rows: list[dict[str, Any]] = []

    if args.cold:
        # コールドスタート: 各実装につき 1 入力を 1 回だけ
        probe = dataset[0]
        for impl, url in targets:
            row = _sample(
                impl=impl,
                url=url,
                protocol=args.protocol,
                input_id=probe["id"],
                text=probe["text"],
                iteration=0,
                timeout=args.timeout,
                phase="cold",
            )
            rows.append(row)
            print(
                f"[cold ] {impl:<12} {row['e2e_latency_ms']:>9.0f} ms  "
                f"http={row['http_status']} ok={row['ok']}"
            )
        _append(out, rows)
        print(f"\nSaved {len(rows)} row(s) -> {out}")
        return 0

    for iteration in range(args.iterations):
        for item in dataset:
            for impl, url in targets:
                row = _sample(
                    impl=impl,
                    url=url,
                    protocol=args.protocol,
                    input_id=item["id"],
                    text=item["text"],
                    iteration=iteration,
                    timeout=args.timeout,
                    phase="warm",
                )
                rows.append(row)
                print(
                    f"[{item['id']:<12}#{iteration}] {impl:<12} "
                    f"{row['e2e_latency_ms']:>9.0f} ms  "
                    f"http={row['http_status']} ok={row['ok']}"
                )

    _append(out, rows)

    print(f"\nSaved {len(rows)} row(s) -> {out}")
    for impl, _ in targets:
        values = [r["e2e_latency_ms"] for r in rows if r["impl"] == impl and r["ok"]]
        if values:
            print(
                f"  {impl:<12} n={len(values):<3} "
                f"median={statistics.median(values):.0f} ms "
                f"max={max(values):.0f} ms"
            )
        else:
            print(f"  {impl:<12} 成功サンプルなし")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MAF vs Copilot SDK 比較ドライバ")
    parser.add_argument("--maf-url", default=os.environ.get("MAF_AGENT_URL"))
    parser.add_argument("--copilot-url", default=os.environ.get("COPILOT_AGENT_URL"))
    parser.add_argument(
        "--protocol", choices=["responses", "invocations"], default="responses"
    )
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--cold", action="store_true", help="コールドスタート計測のみ")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
