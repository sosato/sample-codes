"""A) Microsoft Agent Framework 版: 3 ステップ推論 Hosted Agent。

構成
----
`WorkflowBuilder` で 3 つの Executor を直列に繋いだワークフローを組み、
`Workflow.as_agent()` で Agent 化してから `ResponsesHostServer` で
Foundry Responses プロトコル (`/responses`) として公開する。

    decompose -> solve -> synthesize

比較の公平性のため、プロンプトは `shared.prompts` から import し、
計測は `shared.tracing` の JSONL スキーマへ揃える。

実行モード
----------
* 既定 (引数なし)       : Hosted Agent サーバとして起動 (`azd ai agent run` 経由)
* ``--local``           : サーバを立てずワークフローを直接 1 回実行 (疎通確認用)
* ``--bench``           : shared/inputs.json の全入力を N 回実行 (ローカル計測用)
* ``--fake``            : Foundry へ接続せずスタブ応答で構造だけ検証 (Azure 不要)

環境変数
--------
Hosted Agent ランタイムが自動注入するもの:
  ``FOUNDRY_PROJECT_ENDPOINT`` / ``AZURE_AI_MODEL_DEPLOYMENT_NAME``
  ``APPLICATIONINSIGHTS_CONNECTION_STRING``
本ハーネス独自:
  ``HARNESS_LOG_DIR`` (JSONL 出力先), ``HARNESS_ITERATIONS`` (--bench の反復数)

参考:
  https://learn.microsoft.com/agent-framework/hosting/foundry-hosted-agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# `shared` は deploy 時に tools/sync_shared.py でこのディレクトリ配下へ複製される。
# ローカル開発時はリポジトリ直下の正本を使う。
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_framework import (  # noqa: E402
    Message,
    WorkflowBuilder,
    WorkflowContext,
    executor,
)

from shared import prompts  # noqa: E402
from shared.tracing import (  # noqa: E402
    RunTracer,
    ensure_utf8_stdout,
    parse_json_output,
    summarize,
)

IMPL = "maf"
DEFAULT_MODEL = "gpt-5.6-terra"


# ---------------------------------------------------------------------------
# ワークフロー内で受け渡す状態
# ---------------------------------------------------------------------------
@dataclass
class Decomposed:
    run_id: str
    problem: str
    subtasks: list[str] = field(default_factory=list)


@dataclass
class Solved:
    run_id: str
    problem: str
    answers: list[dict[str, Any]] = field(default_factory=list)


# 実行中の RunTracer を run_id で引くレジストリ。
# Executor 間で受け渡すメッセージはシリアライズされうるため、
# トレーサ本体はメッセージに載せず、ここで保持する。
_TRACERS: dict[str, RunTracer] = {}


def _tracer(run_id: str) -> RunTracer:
    tracer = _TRACERS.get(run_id)
    if tracer is None:  # 保険 (通常は decompose が生成済み)
        tracer = RunTracer(
            impl=IMPL, protocol=_protocol(), model=_model_name(), run_id=run_id
        )
        _TRACERS[run_id] = tracer
    return tracer


def _model_name() -> str:
    return os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.environ.get(
        "FOUNDRY_MODEL", DEFAULT_MODEL
    )


def _protocol() -> str:
    return os.environ.get("HARNESS_PROTOCOL", "responses")


# ---------------------------------------------------------------------------
# チャットクライアント / ステップエージェント
# ---------------------------------------------------------------------------
_STEP_AGENTS: dict[str, Any] = {}
_FAKE_MODE = False


def _build_chat_client() -> Any:
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import DefaultAzureCredential

    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError(
            "FOUNDRY_PROJECT_ENDPOINT is not set. "
            "Use `azd ai agent run` locally, or pass --fake for an offline check."
        )
    return FoundryChatClient(
        project_endpoint=endpoint,
        model=_model_name(),
        credential=DefaultAzureCredential(),
    )


def _step_agent(step: prompts.StepSpec) -> Any:
    """ステップ固有の instructions を持つ ChatAgent を遅延生成してキャッシュする。"""
    agent = _STEP_AGENTS.get(step.name)
    if agent is not None:
        return agent

    if _FAKE_MODE:
        agent = _FakeStepAgent(step)
    else:
        client = _STEP_AGENTS.get("__client__")
        if client is None:
            client = _build_chat_client()
            _STEP_AGENTS["__client__"] = client
        agent = client.as_agent(
            name=f"{prompts.AGENT_NAME}-{step.name}",
            instructions=step.system,
            # ホスティング基盤が履歴を管理するため、二重保持を避ける。
            default_options={"store": False},
        )
    _STEP_AGENTS[step.name] = agent
    return agent


def _usage(response: Any) -> tuple[int | None, int | None]:
    """応答オブジェクトからトークン使用量を防御的に取り出す。

    MAF は prerelease でありプロパティ名が変わりうるため、
    複数の候補を順に試し、取れなければ None を返す。
    """
    for attr in ("usage_details", "usage", "response_usage"):
        usage = getattr(response, attr, None)
        if usage is None:
            continue
        for in_key, out_key in (
            ("input_token_count", "output_token_count"),
            ("prompt_tokens", "completion_tokens"),
            ("input_tokens", "output_tokens"),
        ):
            got_in = getattr(usage, in_key, None)
            got_out = getattr(usage, out_key, None)
            if got_in is not None or got_out is not None:
                return got_in, got_out
    return None, None


async def _run_step(
    step: prompts.StepSpec, run_id: str, step_index: int, **template_args: str
) -> dict[str, Any]:
    """1 ステップを実行し、計測してから JSON をパースして返す。"""
    user_text = step.render_user(**template_args)
    tracer = _tracer(run_id)
    agent = _step_agent(step)

    with tracer.step(step.name, step_index, step.system, user_text) as record:
        response = await agent.run(user_text)
        text = getattr(response, "text", None) or str(response)
        record.output = text
        record.input_tokens, record.output_tokens = _usage(response)

    return parse_json_output(text)


# ---------------------------------------------------------------------------
# Executor 1: 分解
# ---------------------------------------------------------------------------
@executor(id="decompose")
async def decompose(
    messages: list[Message], ctx: WorkflowContext[Decomposed]
) -> None:
    """開始 Executor。

    `Workflow.as_agent()` は入力を必ず `list[Message]` へ正規化してから
    開始 Executor に渡すため、この型注釈でなければ Agent 化に失敗する
    (``ValueError: Workflow's start executor cannot handle list[Message]``)。
    """
    problem = "\n".join(m.text for m in messages if getattr(m, "text", None)).strip()
    tracer = RunTracer(
        impl=IMPL,
        protocol=_protocol(),
        model=_model_name(),
        input_id=os.environ.get("HARNESS_INPUT_ID", "adhoc"),
        iteration=int(os.environ.get("HARNESS_ITERATION", "0")),
    )
    _TRACERS[tracer.run_id] = tracer

    parsed = await _run_step(prompts.DECOMPOSE, tracer.run_id, 0, problem=problem)
    subtasks = [str(s) for s in parsed.get("subtasks", []) if str(s).strip()]
    if not subtasks:
        # 分解に失敗しても止めず、元課題をそのまま 1 サブ課題として続行する。
        subtasks = [problem]

    await ctx.send_message(
        Decomposed(run_id=tracer.run_id, problem=problem, subtasks=subtasks)
    )


# ---------------------------------------------------------------------------
# Executor 2: 個別回答
# ---------------------------------------------------------------------------
@executor(id="solve")
async def solve(state: Decomposed, ctx: WorkflowContext[Solved]) -> None:
    parsed = await _run_step(
        prompts.SOLVE,
        state.run_id,
        1,
        problem=state.problem,
        subtasks=json.dumps({"subtasks": state.subtasks}, ensure_ascii=False),
    )
    answers = parsed.get("answers")
    if not isinstance(answers, list) or not answers:
        answers = [{"subtask": s, "answer": ""} for s in state.subtasks]

    await ctx.send_message(
        Solved(run_id=state.run_id, problem=state.problem, answers=answers)
    )


# ---------------------------------------------------------------------------
# Executor 3: 統合 + 自己検証
# ---------------------------------------------------------------------------
@executor(id="synthesize")
async def synthesize(state: Solved, ctx: WorkflowContext[None, str]) -> None:
    parsed = await _run_step(
        prompts.SYNTHESIZE,
        state.run_id,
        2,
        problem=state.problem,
        answers=json.dumps({"answers": state.answers}, ensure_ascii=False),
    )

    tracer = _TRACERS.pop(state.run_id, None)
    if tracer is not None:
        tracer.finish()

    result = {
        "run_id": state.run_id,
        "impl": IMPL,
        "final_answer": parsed.get("final_answer", parsed.get("_raw", "")),
        "contradictions": parsed.get("contradictions", []),
        "confidence": parsed.get("confidence"),
        "steps": {
            "decompose": {"subtasks": [a.get("subtask") for a in state.answers]},
            "solve": {"answers": state.answers},
        },
        "trace": summarize(tracer.records) if tracer is not None else [],
    }
    await ctx.yield_output(json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------------------
# ワークフロー構築
# ---------------------------------------------------------------------------
def build_workflow() -> Any:
    """3 ステップを直列に繋いだワークフローを構築する。"""
    return (
        WorkflowBuilder(start_executor=decompose, output_from=[synthesize])
        .add_edge(decompose, solve)
        .add_edge(solve, synthesize)
        .build()
    )


def build_agent() -> Any:
    """ワークフローを Agent 化する (Hosted Agent として公開する対象)。"""
    workflow = build_workflow()
    return workflow.as_agent(name=prompts.AGENT_NAME)


# ---------------------------------------------------------------------------
# オフライン検証用スタブ
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeStepAgent:
    """Foundry へ接続せずに 3 ステップの配線だけを検証するスタブ。"""

    def __init__(self, step: prompts.StepSpec) -> None:
        self._step = step

    async def run(self, user_text: str) -> _FakeResponse:
        await asyncio.sleep(0.01)
        if self._step.name == prompts.STEP_DECOMPOSE:
            payload = {"subtasks": ["前提の確認", "主要因の特定", "結論の検証"]}
        elif self._step.name == prompts.STEP_SOLVE:
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
        return _FakeResponse(json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
async def run_once(problem: str) -> str:
    workflow = build_workflow()
    events = await workflow.run([Message(role="user", contents=[problem])])
    outputs = list(events.get_outputs())
    return outputs[-1] if outputs else ""


async def run_bench(iterations: int) -> None:
    inputs_path = Path(__file__).resolve().parent / "shared" / "inputs.json"
    if not inputs_path.is_file():
        inputs_path = Path(__file__).resolve().parents[2] / "shared" / "inputs.json"
    dataset = json.loads(inputs_path.read_text(encoding="utf-8"))["inputs"]

    for iteration in range(iterations):
        for item in dataset:
            os.environ["HARNESS_INPUT_ID"] = item["id"]
            os.environ["HARNESS_ITERATION"] = str(iteration)
            try:
                output = await run_once(item["text"])
                print(f"[{item['id']}#{iteration}] ok  {len(output)} chars")
            except Exception as exc:  # noqa: BLE001 - 1 件の失敗で全体を止めない
                print(f"[{item['id']}#{iteration}] ERR {type(exc).__name__}: {exc}")


def main() -> int:
    global _FAKE_MODE

    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description="MAF 3-step reasoning hosted agent")
    parser.add_argument("--local", action="store_true", help="ワークフローを 1 回だけ実行")
    parser.add_argument("--bench", action="store_true", help="inputs.json を反復実行")
    parser.add_argument("--fake", action="store_true", help="Foundry へ接続しない構造検証")
    parser.add_argument(
        "--problem",
        default="APIのページネーションをオフセット方式とカーソル方式のどちらにすべきか。",
        help="--local で使う入力",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=int(os.environ.get("HARNESS_ITERATIONS", "1")),
    )
    args = parser.parse_args()

    _FAKE_MODE = args.fake
    if args.fake:
        os.environ["HARNESS_PROTOCOL"] = "local-fake"

    if args.bench:
        asyncio.run(run_bench(args.iterations))
        return 0

    if args.local or args.fake:
        print(asyncio.run(run_once(args.problem)))
        return 0

    # 既定: Hosted Agent サーバとして起動
    from agent_framework_foundry_hosting import ResponsesHostServer

    ResponsesHostServer(build_agent()).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
