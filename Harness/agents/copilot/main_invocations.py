"""B') GitHub Copilot SDK 版 (Invocations プロトコル)。

Foundry 公式の bring-your-own サンプル
(`samples/python/hosted-agents/bring-your-own/invocations/github-copilot`)
に合わせた副系。`main.py` (Responses) とロジックは共有し、
**プロトコル層だけが異なる**ようにして「プロトコル差分」を単独で観測できるようにする。

Responses との違い:
  * 会話履歴・セッションのライフサイクルをプラットフォームが管理しない
  * リクエスト / レスポンスのスキーマを自前定義できる
  * OpenAI 互換クライアントからは直接叩けない

デプロイ::

    azd ai agent init --protocol invocations --deploy-mode code
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from main import ThreeStepRunner  # noqa: E402

os.environ.setdefault("HARNESS_PROTOCOL", "invocations")

_runner = ThreeStepRunner()


def build_app():
    from azure.ai.agentserver.invocations import InvocationAgentServerHost
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    app = InvocationAgentServerHost()

    @app.invoke_handler
    async def handle_invoke(request: Request):  # noqa: D401 - 規定シグネチャ
        raw = (await request.body()).decode("utf-8").strip()
        try:
            body = json.loads(raw)
            user_message = body.get("message") or body.get("input") or raw
        except json.JSONDecodeError:
            user_message = raw

        if not user_message:
            return Response(content="Missing 'message' in request", status_code=400)

        try:
            result = await _runner.run(str(user_message))
        except Exception as exc:  # noqa: BLE001 - 失敗内容を呼び出し側へ返す
            return JSONResponse(
                {"error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )
        return JSONResponse(result)

    return app


def main() -> int:
    build_app().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
