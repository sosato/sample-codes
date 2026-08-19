"""両実装 (MAF / GitHub Copilot SDK) で共有する 3 ステップ推論ロジックの仕様。

このモジュールはフレームワーク非依存であり、外部依存を一切持たない。
比較の公平性を保つため、プロンプト文字列は必ずここから import して使用すること。

3 ステップの構成 (Grounding ソース無し・ツール無し):
    STEP 1  decompose  : 入力課題を最大 3 個のサブ課題へ分解する
    STEP 2  solve      : 各サブ課題へ個別に短く回答する
    STEP 3  synthesize : 統合し、自己検証したうえで最終回答を出す
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# --------------------------------------------------------------------------
# ステップ定義
# --------------------------------------------------------------------------

STEP_DECOMPOSE: Final = "decompose"
STEP_SOLVE: Final = "solve"
STEP_SYNTHESIZE: Final = "synthesize"

STEP_ORDER: Final[tuple[str, str, str]] = (
    STEP_DECOMPOSE,
    STEP_SOLVE,
    STEP_SYNTHESIZE,
)


@dataclass(frozen=True)
class StepSpec:
    """1 ステップ分のプロンプト仕様。"""

    name: str
    system: str
    user_template: str

    def render_user(self, **kwargs: str) -> str:
        return self.user_template.format(**kwargs)


# --------------------------------------------------------------------------
# 共通のベースインストラクション
# --------------------------------------------------------------------------
# Copilot SDK は既定 (mode="copilot-cli") でコーディングエージェント向けの
# システムプロンプト・ツール・メモリが有効になるため、素の推論に近づける目的で
# 「外部情報を参照しない」「ツールを使わない」を明示的に指示する。
# MAF 側は元々素の ChatAgent だが、条件を揃えるため同じ文言を使う。

_COMMON_GUARDRAILS: Final = (
    "You are a reasoning assistant running without any grounding source.\n"
    "Rules:\n"
    "- Do NOT use any tools, file system access, web search, or shell commands.\n"
    "- Do NOT ask clarifying questions. Reason from the prompt alone.\n"
    "- Do NOT invent citations, URLs, or source names.\n"
    "- Answer in the same language as the user input.\n"
    "- Be concise. Never exceed the requested output format.\n"
)

DECOMPOSE = StepSpec(
    name=STEP_DECOMPOSE,
    system=(
        _COMMON_GUARDRAILS + "\n"
        "Your task for this turn: decompose the problem.\n"
        "Output STRICTLY a JSON object and nothing else, with this shape:\n"
        '{"subtasks": ["<subtask 1>", "<subtask 2>", "<subtask 3>"]}\n'
        "Provide between 2 and 3 subtasks. No markdown fences, no commentary."
    ),
    user_template="Problem:\n{problem}",
)

SOLVE = StepSpec(
    name=STEP_SOLVE,
    system=(
        _COMMON_GUARDRAILS + "\n"
        "Your task for this turn: answer each subtask independently.\n"
        "Output STRICTLY a JSON object and nothing else, with this shape:\n"
        '{"answers": [{"subtask": "<subtask>", "answer": "<<=3 sentences>"}]}\n'
        "Keep one entry per input subtask, in the same order. "
        "No markdown fences, no commentary."
    ),
    user_template="Original problem:\n{problem}\n\nSubtasks (JSON):\n{subtasks}",
)

SYNTHESIZE = StepSpec(
    name=STEP_SYNTHESIZE,
    system=(
        _COMMON_GUARDRAILS + "\n"
        "Your task for this turn: synthesize and self-verify.\n"
        "Output STRICTLY a JSON object and nothing else, with this shape:\n"
        '{"final_answer": "<<=6 sentences>", '
        '"contradictions": ["<issue>"], '
        '"confidence": <float between 0 and 1>}\n'
        'Use an empty array for "contradictions" when you find none. '
        "No markdown fences, no commentary."
    ),
    user_template=(
        "Original problem:\n{problem}\n\nPer-subtask answers (JSON):\n{answers}"
    ),
)

STEPS: Final[dict[str, StepSpec]] = {
    STEP_DECOMPOSE: DECOMPOSE,
    STEP_SOLVE: SOLVE,
    STEP_SYNTHESIZE: SYNTHESIZE,
}

# エージェント全体としての説明 (Hosted Agent の instructions / description に使う)
AGENT_NAME: Final = "three-step-reasoner"
AGENT_DESCRIPTION: Final = (
    "Minimal 3-step reasoning agent (decompose -> solve -> synthesize) "
    "with no grounding source and no tools."
)
