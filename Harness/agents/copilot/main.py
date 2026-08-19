"""B) GitHub Copilot SDK 版: 3 ステップ推論 Hosted Agent (Responses プロトコル)。

構成
----
Copilot SDK には Foundry 専用のホスティング統合が無いため、
汎用の ``azure-ai-agentserver-responses`` でラップする (bring-your-own 方式)。

    CopilotClient (= Copilot CLI を JSON-RPC 駆動)
      └─ step ごとに session を作成 → send → assistant.message を収集
    ResponsesAgentServerHost (= /responses エンドポイント)

MAF 版との実装差分の要点 (REPORT.md の比較対象):
  * モデル指定を ``provider`` として自前構築する必要がある (BYOM)
  * ``bearer_token`` は静的で約 1 時間で失効するため、都度取得が必要 (R3)
  * Copilot CLI のランタイムバイナリを同梱・事前配置する必要がある (R2)
  * system_message はセッション単位のため、ステップごとにセッションを作り直す

環境変数
--------
自動注入 (Hosted Agent):
  ``FOUNDRY_PROJECT_ENDPOINT`` / ``AZURE_AI_MODEL_DEPLOYMENT_NAME``
本実装で参照:
  ``AZURE_OPENAI_ENDPOINT``        Azure OpenAI ホストを使う場合 (type="azure")
  ``COPILOT_PROVIDER_TYPE``        "openai" (既定) / "azure"
  ``COPILOT_API_VERSION``          type="azure" のときの api_version
  ``COPILOT_CLIENT_MODE``          "empty" (既定) / "copilot-cli"
  ``COPILOT_SYSTEM_MESSAGE_MODE``  "replace" (既定) / "append"
  ``COPILOT_HOME``                 CLI の書き込み先 (Hosted Agent では要指定)
  ``COPILOT_CLI_PATH``             同梱したランタイムバイナリのパス
  ``HARNESS_LOG_DIR`` / ``HARNESS_ITERATIONS``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import prompts  # noqa: E402
from shared.tracing import (  # noqa: E402
    RunTracer,
    ensure_utf8_stdout,
    parse_json_output,
    summarize,
)

IMPL = "copilot-sdk"
DEFAULT_MODEL = "gpt-5.6-terra"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
STEP_TIMEOUT_SECONDS = float(os.environ.get("HARNESS_STEP_TIMEOUT", "120"))
# 失効の少し前に取り直す (bearer_token は自動更新されない)
TOKEN_REFRESH_MARGIN_SECONDS = 300.0


def _model_name() -> str:
    return os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.environ.get(
        "FOUNDRY_MODEL", DEFAULT_MODEL
    )


def _protocol() -> str:
    return os.environ.get("HARNESS_PROTOCOL", "responses")


# ---------------------------------------------------------------------------
# 認証 (R3: bearer_token の失効対策)
# ---------------------------------------------------------------------------
class _TokenCache:
    """`DefaultAzureCredential` のトークンを期限管理付きでキャッシュする。"""

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_on: float = 0.0
        self._credential: Any = None
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            now = time.time()
            if self._token and now < self._expires_on - TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token
            if self._credential is None:
                from azure.identity import DefaultAzureCredential

                self._credential = DefaultAzureCredential()
            # 同期 API のためスレッドへ逃がす
            access = await asyncio.to_thread(self._credential.get_token, TOKEN_SCOPE)
            self._token = access.token
            self._expires_on = float(getattr(access, "expires_on", now + 3600))
            return self._token


_TOKENS = _TokenCache()


async def build_provider() -> dict[str, Any]:
    """BYOM のプロバイダー設定を都度組み立てる (トークンは毎回検証済みのものを使う)。"""
    token = await _TOKENS.get()
    provider_type = os.environ.get("COPILOT_PROVIDER_TYPE", "openai").lower()

    if provider_type == "azure":
        base_url = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        if not base_url:
            raise RuntimeError(
                "COPILOT_PROVIDER_TYPE=azure には AZURE_OPENAI_ENDPOINT が必要です "
                "(例: https://<account>.openai.azure.com)。パスは付けないこと。"
            )
        provider: dict[str, Any] = {
            "type": "azure",
            "base_url": base_url,
            "bearer_token": token,
            # wire_api="responses" は store:false で 400 になり多ターンが壊れる。
            "wire_api": "completions",
        }
        api_version = os.environ.get("COPILOT_API_VERSION")
        if api_version:
            provider["azure"] = {"api_version": api_version}
        return provider

    project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not project:
        raise RuntimeError(
            "FOUNDRY_PROJECT_ENDPOINT is not set. "
            "Use `azd ai agent run` locally, or pass --fake for an offline check."
        )
    return {
        "type": "openai",
        "base_url": f"{project}/openai/v1/",
        "bearer_token": token,
        "wire_api": "completions",
    }


# ---------------------------------------------------------------------------
# Copilot CLI ランタイム (R2: 実行時ダウンロードを避ける)
# ---------------------------------------------------------------------------
def prepare_runtime_env() -> None:
    """書き込み可能な `COPILOT_HOME` を確保する。

    Hosted Agent のサンドボックスでは書き込み可能パスが限られる。
    `$HOME` はターン間で永続化されるため、既定で `$HOME/.copilot` を使う。
    """
    if not os.environ.get("COPILOT_HOME"):
        home = Path(os.path.expanduser("~")) / ".copilot"
        try:
            home.mkdir(parents=True, exist_ok=True)
            os.environ["COPILOT_HOME"] = str(home)
        except OSError:
            import tempfile

            fallback = Path(tempfile.gettempdir()) / "copilot-home"
            fallback.mkdir(parents=True, exist_ok=True)
            os.environ["COPILOT_HOME"] = str(fallback)


# ---------------------------------------------------------------------------
# 3 ステップ実行本体
# ---------------------------------------------------------------------------
class ThreeStepRunner:
    """Copilot SDK で 3 ステップ推論を実行する。

    `CopilotClient` の起動 (= CLI プロセス spawn) はコストが高いため、
    クライアントはプロセス内で 1 つだけ保持して使い回す。
    `system_message` はセッション単位の設定なので、セッションはステップごとに作る。
    """

    def __init__(self, *, fake: bool = False) -> None:
        self._fake = fake
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            prepare_runtime_env()
            from copilot import CopilotClient

            client = CopilotClient(
                # "empty" は CLI 既定のコーディングエージェント向けプロンプト/
                # ツール/メモリを載せない。素の推論比較のため既定でこちらを使う。
                mode=os.environ.get("COPILOT_CLIENT_MODE", "empty"),
                base_directory=os.environ.get("COPILOT_HOME"),
            )
            await client.start()
            self._client = client
            return client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop()
            finally:
                self._client = None

    async def _send_step(
        self, step: prompts.StepSpec, user_text: str
    ) -> tuple[str, int | None, int | None]:
        """1 ステップ = 1 セッションを作り、応答テキストを回収する。"""
        if self._fake:
            return _fake_step_output(step), None, None

        from copilot.session import PermissionHandler
        from copilot.session_events import AssistantMessageData, SessionIdleData

        client = await self._get_client()
        provider = await build_provider()

        chunks: list[str] = []
        done = asyncio.Event()

        session = await client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            model=_model_name(),
            provider=provider,
            system_message={
                "mode": os.environ.get("COPILOT_SYSTEM_MESSAGE_MODE", "replace"),
                "content": step.system,
            },
        )
        try:

            def on_event(event: Any) -> None:
                match event.data:
                    case AssistantMessageData() as data:
                        chunks.append(str(data.content))
                    case SessionIdleData():
                        done.set()

            session.on(on_event)
            await session.send(user_text)
            await asyncio.wait_for(done.wait(), timeout=STEP_TIMEOUT_SECONDS)
        finally:
            with_suppress = getattr(session, "disconnect", None)
            if with_suppress is not None:
                try:
                    await session.disconnect()
                except Exception:  # noqa: BLE001 - クリーンアップ失敗は握りつぶす
                    pass

        # Copilot SDK はトークン使用量を公開していないため None を記録する
        # (これ自体が MAF との可観測性の差分として REPORT に載る)。
        return "".join(chunks).strip(), None, None

    async def run(
        self, problem: str, *, input_id: str = "adhoc", iteration: int = 0
    ) -> dict[str, Any]:
        tracer = RunTracer(
            impl=IMPL,
            protocol=_protocol(),
            model=_model_name(),
            input_id=input_id,
            iteration=iteration,
        )

        # --- Step 1: 分解 -------------------------------------------------
        step = prompts.DECOMPOSE
        user_text = step.render_user(problem=problem)
        with tracer.step(step.name, 0, step.system, user_text) as record:
            text, tin, tout = await self._send_step(step, user_text)
            record.output, record.input_tokens, record.output_tokens = text, tin, tout
        subtasks = [
            str(s)
            for s in parse_json_output(text).get("subtasks", [])
            if str(s).strip()
        ] or [problem]

        # --- Step 2: 個別回答 ---------------------------------------------
        step = prompts.SOLVE
        user_text = step.render_user(
            problem=problem,
            subtasks=json.dumps({"subtasks": subtasks}, ensure_ascii=False),
        )
        with tracer.step(step.name, 1, step.system, user_text) as record:
            text, tin, tout = await self._send_step(step, user_text)
            record.output, record.input_tokens, record.output_tokens = text, tin, tout
        answers = parse_json_output(text).get("answers")
        if not isinstance(answers, list) or not answers:
            answers = [{"subtask": s, "answer": ""} for s in subtasks]

        # --- Step 3: 統合 + 自己検証 --------------------------------------
        step = prompts.SYNTHESIZE
        user_text = step.render_user(
            problem=problem,
            answers=json.dumps({"answers": answers}, ensure_ascii=False),
        )
        with tracer.step(step.name, 2, step.system, user_text) as record:
            text, tin, tout = await self._send_step(step, user_text)
            record.output, record.input_tokens, record.output_tokens = text, tin, tout
        final = parse_json_output(text)

        tracer.finish()

        return {
            "run_id": tracer.run_id,
            "impl": IMPL,
            "final_answer": final.get("final_answer", final.get("_raw", "")),
            "contradictions": final.get("contradictions", []),
            "confidence": final.get("confidence"),
            "steps": {
                "decompose": {"subtasks": subtasks},
                "solve": {"answers": answers},
            },
            "trace": summarize(tracer.records),
        }


def _fake_step_output(step: prompts.StepSpec) -> str:
    if step.name == prompts.STEP_DECOMPOSE:
        payload: dict[str, Any] = {
            "subtasks": ["前提の確認", "主要因の特定", "結論の検証"]
        }
    elif step.name == prompts.STEP_SOLVE:
        payload = {
            "answers": [
                {"subtask": "前提の確認", "answer": "(fake) 前提は明示されていない。"},
                {"subtask": "主要因の特定", "answer": "(fake) 主要因は入力条件。"},
                {"subtask": "結論の検証", "answer": "(fake) 矛盾は見当たらない。"},
            ]
        }
    else:
        payload = {
            "final_answer": "(fake) 3 ステップの配線は正常です。",
            "contradictions": [],
            "confidence": 0.5,
        }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Hosted Agent (Responses プロトコル)
# ---------------------------------------------------------------------------
def build_app() -> Any:
    """Responses プロトコルのアプリを構築する (起動はしない)。"""
    from azure.ai.agentserver.responses import (
        CreateResponse,
        ResponseContext,
        ResponsesAgentServerHost,
        TextResponse,
    )

    app = ResponsesAgentServerHost()
    runner = ThreeStepRunner()

    @app.response_handler
    async def handler(  # noqa: D401 - フレームワーク規定のシグネチャ
        request: CreateResponse,
        context: ResponseContext,
        _cancellation_signal: asyncio.Event,
    ):
        user_input = await context.get_input_text() or ""
        result = await runner.run(user_input)
        return TextResponse(
            context, request, text=json.dumps(result, ensure_ascii=False)
        )

    return app


def serve() -> None:
    build_app().run()


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
async def _run_local(problem: str, fake: bool) -> None:
    runner = ThreeStepRunner(fake=fake)
    try:
        print(json.dumps(await runner.run(problem), ensure_ascii=False, indent=2))
    finally:
        await runner.aclose()


async def _run_bench(iterations: int, fake: bool) -> None:
    inputs_path = Path(__file__).resolve().parent / "shared" / "inputs.json"
    if not inputs_path.is_file():
        inputs_path = Path(__file__).resolve().parents[2] / "shared" / "inputs.json"
    dataset = json.loads(inputs_path.read_text(encoding="utf-8"))["inputs"]

    runner = ThreeStepRunner(fake=fake)
    try:
        for iteration in range(iterations):
            for item in dataset:
                try:
                    await runner.run(
                        item["text"], input_id=item["id"], iteration=iteration
                    )
                    print(f"[{item['id']}#{iteration}] ok")
                except Exception as exc:  # noqa: BLE001
                    print(f"[{item['id']}#{iteration}] ERR {type(exc).__name__}: {exc}")
    finally:
        await runner.aclose()


def main() -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Copilot SDK 3-step reasoning hosted agent"
    )
    parser.add_argument("--local", action="store_true", help="1 回だけ実行")
    parser.add_argument("--bench", action="store_true", help="inputs.json を反復実行")
    parser.add_argument("--fake", action="store_true", help="CLI/Foundry に接続しない構造検証")
    parser.add_argument(
        "--problem",
        default="APIのページネーションをオフセット方式とカーソル方式のどちらにすべきか。",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=int(os.environ.get("HARNESS_ITERATIONS", "1")),
    )
    args = parser.parse_args()

    if args.fake:
        os.environ["HARNESS_PROTOCOL"] = "local-fake"

    if args.bench:
        asyncio.run(_run_bench(args.iterations, args.fake))
        return 0
    if args.local or args.fake:
        asyncio.run(_run_local(args.problem, args.fake))
        return 0

    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
