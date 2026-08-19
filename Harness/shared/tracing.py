"""3 ステップ実行の計測を JSONL で残すフレームワーク非依存ヘルパー。

MAF 版 / Copilot SDK 版の両方から同一スキーマで書き出すことで、
`bench/analyze.py` が実装を区別せずに集計できるようにする。

書き出し先は環境変数 `HARNESS_LOG_DIR` で制御する。
Hosted Agent のサンドボックスでは書き込み可能なパスが限られるため、
未設定時は `$HOME/harness-logs` (無ければ一時ディレクトリ) にフォールバックする。

JSONL 1 行のスキーマ::

    {
      "schema": 1,
      "run_id": "<1 リクエストを通す UUID>",
      "impl": "maf" | "copilot-sdk",
      "protocol": "responses" | "invocations" | "local",
      "model": "gpt-5.6-terra",
      "input_id": "logic-01",
      "iteration": 0,
      "step": "decompose" | "solve" | "synthesize" | "__total__",
      "step_index": 0,
      "prompt_hash": "<sha256 先頭 16 桁>",
      "started_at": "<ISO8601 UTC>",
      "latency_ms": 1234.5,
      "input_tokens": 123 | null,
      "output_tokens": 45 | null,
      "output_chars": 512,
      "output": "<モデル生出力>",
      "ok": true,
      "error": null
    }
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
TOTAL_STEP = "__total__"

_WRITE_LOCK = threading.Lock()


def log_dir() -> Path:
    """書き込み可能なログディレクトリを解決して作成する。"""
    candidates = [
        os.environ.get("HARNESS_LOG_DIR"),
        str(Path(os.path.expanduser("~")) / "harness-logs"),
        str(Path(tempfile.gettempdir()) / "harness-logs"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return path
        except OSError:
            continue
    raise RuntimeError("No writable log directory found")


def log_path(impl: str) -> Path:
    return log_dir() / f"{impl}.jsonl"


def prompt_hash(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def ensure_utf8_stdout() -> None:
    """Windows コンソール (cp932/cp1252) で日本語出力が落ちないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def new_run_id() -> str:
    return uuid.uuid4().hex


@dataclass
class StepRecord:
    """1 ステップ分の計測結果。"""

    run_id: str
    impl: str
    protocol: str
    model: str
    input_id: str
    iteration: int
    step: str
    step_index: int
    prompt_hash: str
    started_at: str
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    output_chars: int = 0
    output: str = ""
    ok: bool = True
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = SCHEMA_VERSION
        return payload


def write_record(record: StepRecord) -> None:
    """JSONL へ 1 行追記する。失敗しても本処理は止めない。"""
    try:
        line = json.dumps(record.to_json(), ensure_ascii=False)
        with _WRITE_LOCK:
            with log_path(record.impl).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - 計測失敗で推論を落とさない
        pass


class RunTracer:
    """1 リクエスト (= 3 ステップ) 分の計測をまとめて扱う。"""

    def __init__(
        self,
        *,
        impl: str,
        protocol: str,
        model: str,
        input_id: str = "adhoc",
        iteration: int = 0,
        run_id: str | None = None,
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.impl = impl
        self.protocol = protocol
        self.model = model
        self.input_id = input_id
        self.iteration = iteration
        self.records: list[StepRecord] = []
        self._t0 = time.perf_counter()

    @contextlib.contextmanager
    def step(self, name: str, index: int, *prompt_parts: str) -> Iterator[StepRecord]:
        record = StepRecord(
            run_id=self.run_id,
            impl=self.impl,
            protocol=self.protocol,
            model=self.model,
            input_id=self.input_id,
            iteration=self.iteration,
            step=name,
            step_index=index,
            prompt_hash=prompt_hash(*prompt_parts),
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        started = time.perf_counter()
        try:
            yield record
        except Exception as exc:  # noqa: BLE001 - 記録してから再送出
            record.ok = False
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.latency_ms = (time.perf_counter() - started) * 1000
            record.output_chars = len(record.output)
            self.records.append(record)
            write_record(record)

    def finish(self, *, ok: bool = True, error: str | None = None) -> None:
        """全体レイテンシを `__total__` ステップとして記録する。"""
        total = StepRecord(
            run_id=self.run_id,
            impl=self.impl,
            protocol=self.protocol,
            model=self.model,
            input_id=self.input_id,
            iteration=self.iteration,
            step=TOTAL_STEP,
            step_index=len(STEP_INDEXES),
            prompt_hash="-",
            started_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=(time.perf_counter() - self._t0) * 1000,
            input_tokens=_sum_or_none(r.input_tokens for r in self.records),
            output_tokens=_sum_or_none(r.output_tokens for r in self.records),
            ok=ok,
            error=error,
        )
        self.records.append(total)
        write_record(total)


STEP_INDEXES = (0, 1, 2)


def summarize(records: list[StepRecord]) -> list[dict[str, Any]]:
    """レスポンス本文へ載せるための軽量サマリ。

    Hosted Agent へデプロイするとコンテナ内の JSONL は取り出せないため、
    ステップ別レイテンシは応答に同梱してクライアント側で集計できるようにする。
    出力本文は含めない (レスポンスの肥大化とログ二重化を避ける)。
    """
    return [
        {
            "step": r.step,
            "step_index": r.step_index,
            "latency_ms": round(r.latency_ms, 2),
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "output_chars": r.output_chars,
            "ok": r.ok,
            "error": r.error,
        }
        for r in records
    ]


def _sum_or_none(values: Any) -> int | None:
    collected = [v for v in values if isinstance(v, int)]
    return sum(collected) if collected else None


# --------------------------------------------------------------------------
# モデル出力のパース
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json_output(text: str) -> dict[str, Any]:
    """モデルの生出力から JSON オブジェクトを取り出す。

    プロンプトでフェンス禁止を指示していても実装によって混入するため、
    フェンス除去 → 最外側の中括弧抽出、の順にフォールバックする。
    パースできない場合は `{"_raw": text}` を返し、呼び出し側を落とさない。
    """
    if not text:
        return {"_raw": ""}

    stripped = _FENCE_RE.sub("", text).strip()
    for candidate in (stripped, _outermost_object(stripped)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"_raw": text}


def _outermost_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]
