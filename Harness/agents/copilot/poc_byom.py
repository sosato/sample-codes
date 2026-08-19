"""R1 検証用 PoC: GitHub Copilot SDK の BYOM で Foundry の `gpt-5.6-terra` が使えるか。

背景
----
Copilot SDK はプロンプト内容を暗号化して送るため、**暗号化コンテンツの復号に
対応したモデルでしか BYOM が成立しない**。非対応モデルでは
``400 Encrypted content is not supported`` になるか、サイレントタイムアウトする。
`gpt-5.6-terra` の可否は公開情報から判断できないため、実装着手前にここで判定する。

このスクリプトは複数のプロバイダー構成を順に試し、最初に成功した構成を報告する。

試行する構成
------------
1. ``type="openai"``  + Foundry プロジェクトの ``/openai/v1/`` ルート
2. ``type="azure"``   + Azure OpenAI ホスト (``*.openai.azure.com``) + api_version
3. ``type="azure"``   + 同上 (api_version 省略 = GA versionless v1 ルート)

使い方
------
::

    az login
    $env:FOUNDRY_PROJECT_ENDPOINT = "https://<account>.services.ai.azure.com/api/projects/<project>"
    $env:AZURE_AI_MODEL_DEPLOYMENT_NAME = "gpt-5.6-terra"
    # 2/3 を試す場合のみ
    $env:AZURE_OPENAI_ENDPOINT = "https://<account>.openai.azure.com"

    python agents/copilot/poc_byom.py

終了コード: 0 = いずれかの構成で疎通成功 / 1 = 全構成失敗 / 2 = 前提不足
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
for _candidate in (_HERE.parent, *_HERE.parents[1:3]):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from shared.tracing import ensure_utf8_stdout  # noqa: E402

TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_MODEL = "gpt-5.6-terra"
PROBE = "Reply with exactly the word: PONG"
TIMEOUT_SECONDS = float(os.environ.get("POC_TIMEOUT_SECONDS", "90"))


def _model() -> str:
    return os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", DEFAULT_MODEL)


def _bearer_token() -> str:
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential().get_token(TOKEN_SCOPE).token


def _candidates(token: str) -> list[tuple[str, dict[str, Any]]]:
    """試行するプロバイダー構成を組み立てる。"""
    out: list[tuple[str, dict[str, Any]]] = []

    project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if project:
        out.append(
            (
                "foundry-project-openai-v1",
                {
                    "type": "openai",
                    "base_url": f"{project}/openai/v1/",
                    "bearer_token": token,
                    "wire_api": "completions",
                },
            )
        )

    aoai = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    if aoai:
        out.append(
            (
                "azure-openai-with-apiversion",
                {
                    "type": "azure",
                    "base_url": aoai,
                    "bearer_token": token,
                    "wire_api": "completions",
                    "azure": {"api_version": "2025-04-01-preview"},
                },
            )
        )
        out.append(
            (
                "azure-openai-versionless",
                {
                    "type": "azure",
                    "base_url": aoai,
                    "bearer_token": token,
                    "wire_api": "completions",
                },
            )
        )
    return out


def _base_directory() -> str:
    """Copilot CLI の状態書き込み先を確保する。

    `mode="empty"` は暗黙の `~/.copilot` フォールバックを行わず、
    `base_directory` / `session_fs` / URI 接続のいずれかを必須とする
    (``ValueError: CopilotClient(mode='empty') requires base_directory...``)。
    """
    configured = os.environ.get("COPILOT_HOME")
    candidates = [
        configured,
        str(Path(os.path.expanduser("~")) / ".copilot-harness"),
        str(Path(tempfile.gettempdir()) / "copilot-harness"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            return str(path)
        except OSError:
            continue
    raise RuntimeError("No writable base directory for the Copilot CLI")


async def _probe(label: str, provider: dict[str, Any]) -> tuple[bool, str, float]:
    """1 構成で 1 発だけ推論して結果を返す。"""
    from copilot import CopilotClient, ToolSet
    from copilot.session import PermissionHandler
    from copilot.session_events import AssistantMessageData, SessionIdleData

    started = time.perf_counter()
    chunks: list[str] = []
    done = asyncio.Event()

    try:
        async with CopilotClient(
            mode=os.environ.get("COPILOT_CLIENT_MODE", "empty"),
            base_directory=_base_directory(),
        ) as client:
            async with await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                model=_model(),
                provider=provider,
                # `mode="empty"` は各セッションが使うツールの明示を要求する。
                # 純粋な推論比較なのでツールは 1 つも足さない。
                available_tools=ToolSet(),
            ) as session:

                def on_event(event: Any) -> None:
                    match event.data:
                        case AssistantMessageData() as data:
                            chunks.append(str(data.content))
                        case SessionIdleData():
                            done.set()

                session.on(on_event)
                await session.send(PROBE)
                await asyncio.wait_for(done.wait(), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        elapsed = (time.perf_counter() - started) * 1000
        return False, f"TIMEOUT after {TIMEOUT_SECONDS}s (暗号化コンテンツ非対応の疑い)", elapsed
    except Exception as exc:  # noqa: BLE001 - 失敗理由の分類がこの PoC の目的
        elapsed = (time.perf_counter() - started) * 1000
        return False, f"{type(exc).__name__}: {exc}", elapsed

    elapsed = (time.perf_counter() - started) * 1000
    text = "".join(chunks).strip()
    return bool(text), text or "(empty response)", elapsed


async def main_async() -> int:
    if not os.environ.get("FOUNDRY_PROJECT_ENDPOINT") and not os.environ.get(
        "AZURE_OPENAI_ENDPOINT"
    ):
        print(
            "ERROR: FOUNDRY_PROJECT_ENDPOINT か AZURE_OPENAI_ENDPOINT の"
            "いずれかを設定してください。"
        )
        return 2

    try:
        token = _bearer_token()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: トークン取得に失敗しました ({exc}). `az login` を確認してください。")
        return 2

    results: list[dict[str, Any]] = []
    success = False
    for label, provider in _candidates(token):
        redacted = {**provider, "bearer_token": "***"}
        print(f"\n--- {label} ---")
        print(json.dumps(redacted, ensure_ascii=False, indent=2))
        ok, detail, elapsed = await _probe(label, provider)
        print(f"result: {'OK' if ok else 'FAIL'}  ({elapsed:.0f} ms)")
        print(f"detail: {detail[:500]}")
        results.append(
            {"config": label, "ok": ok, "detail": detail[:2000], "latency_ms": elapsed}
        )
        if ok:
            success = True
            break

    out = Path(__file__).resolve().parent / "poc_byom_result.json"
    out.write_text(
        json.dumps(
            {"model": _model(), "results": results}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(f"\nSaved: {out}")

    if not success:
        print(
            "\n[R1 判定] 全構成で失敗。REPORT.md の分岐に従い、"
            "(a) api_version/wire_api 調整、(b) gpt-5 系の対応モデルを追加デプロイして"
            "「モデル制約」自体を差分として報告、(c) GitHub 既定モデル構成、を検討してください。"
        )
    return 0 if success else 1


def main() -> int:
    ensure_utf8_stdout()
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
