"""ベンチ経路をオフラインで検証するためのスタブサーバ。

Azure に接続せずに `run_compare.py` → `analyze.py` の配線を確かめるための最小実装。
Foundry の Responses / Invocations の**応答形だけ**を模倣する
(認証・ストリーミング・セッション管理は模倣しない)。

使い方::

    python bench/stub_server.py --port 8088 --impl maf
    python bench/stub_server.py --port 8089 --impl copilot-sdk --extra-latency 0.4

    python bench/run_compare.py --maf-url http://localhost:8088/responses \
                                --copilot-url http://localhost:8089/responses \
                                --iterations 2

標準ライブラリのみ。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import prompts  # noqa: E402
from shared.tracing import ensure_utf8_stdout, summarize  # noqa: E402

_CONFIG: dict[str, Any] = {"impl": "maf", "extra_latency": 0.15, "jitter": 0.05}


class _Rec:
    """`summarize()` に渡せる最小のレコード互換オブジェクト。"""

    def __init__(self, step: str, index: int, latency_ms: float, chars: int) -> None:
        self.step = step
        self.step_index = index
        self.latency_ms = latency_ms
        self.input_tokens = 120 + index * 40
        self.output_tokens = 60 + index * 20
        self.output_chars = chars
        self.ok = True
        self.error = None


def _simulate(problem: str) -> dict[str, Any]:
    records = []
    for index, name in enumerate(prompts.STEP_ORDER):
        base = _CONFIG["extra_latency"] * (1 + index * 0.3)
        delay = max(0.0, base + random.uniform(-_CONFIG["jitter"], _CONFIG["jitter"]))
        time.sleep(delay)
        records.append(_Rec(name, index, delay * 1000, 80 + index * 30))

    subtasks = ["前提の確認", "主要因の特定", "結論の検証"]
    return {
        "run_id": f"stub-{int(time.time() * 1000)}",
        "impl": _CONFIG["impl"],
        "final_answer": f"(stub/{_CONFIG['impl']}) {problem[:60]} に対する最終回答。",
        "contradictions": [],
        "confidence": round(random.uniform(0.55, 0.9), 2),
        "steps": {
            "decompose": {"subtasks": subtasks},
            "solve": {"answers": [{"subtask": s, "answer": "(stub)"} for s in subtasks]},
        },
        "trace": summarize(records),  # type: ignore[arg-type]
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass  # ベンチ出力を汚さない

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler の規定名
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}

        problem = str(body.get("input") or body.get("message") or "")
        if not problem:
            self._send(400, {"error": "missing input"})
            return

        result = _simulate(problem)

        if self.path.rstrip("/").endswith("responses"):
            payload = {
                "id": result["run_id"],
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(result, ensure_ascii=False),
                            }
                        ],
                    }
                ],
            }
        else:
            payload = result

        self._send(200, payload)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description="ベンチ検証用スタブサーバ")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--impl", default="maf")
    parser.add_argument("--extra-latency", type=float, default=0.15)
    parser.add_argument("--jitter", type=float, default=0.05)
    args = parser.parse_args()

    _CONFIG.update(
        impl=args.impl, extra_latency=args.extra_latency, jitter=args.jitter
    )
    server = serve(args.port)
    print(f"stub[{args.impl}] listening on http://127.0.0.1:{args.port} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
