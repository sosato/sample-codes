"""bench の JSONL を集計して REPORT.md 用の比較表 (Markdown) を生成する。

入力:
  * ``logs/bench.jsonl``   — run_compare.py が書くクライアント側計測
  * ``logs/*.jsonl``       — エージェントが書くサーバ側ステップ計測 (ローカル実行時)

使い方::

    python bench/analyze.py                       # 標準出力へ Markdown
    python bench/analyze.py --out REPORT_data.md  # ファイルへ

標準ライブラリのみ。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.tracing import ensure_utf8_stdout  # noqa: E402

LOGS = ROOT / "logs"
STEP_ORDER = ["decompose", "solve", "synthesize", "__total__"]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _fmt(value: float) -> str:
    return "-" if value != value else f"{value:,.0f}"  # NaN チェック


def _mdtable(headers: list[str], rows: Iterable[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------
def section_e2e(bench: list[dict[str, Any]]) -> str:
    warm = [r for r in bench if r.get("phase") == "warm" and r.get("ok")]
    by_impl: dict[str, list[float]] = defaultdict(list)
    for row in warm:
        by_impl[row["impl"]].append(float(row["e2e_latency_ms"]))

    body = []
    for impl in sorted(by_impl):
        values = by_impl[impl]
        body.append(
            [
                impl,
                str(len(values)),
                _fmt(statistics.median(values)),
                _fmt(_pct(values, 0.95)),
                _fmt(min(values)),
                _fmt(max(values)),
            ]
        )
    if not body:
        return "_(warm サンプルなし)_"
    return _mdtable(
        ["impl", "n", "P50 (ms)", "P95 (ms)", "min", "max"], body
    )


def section_cold(bench: list[dict[str, Any]]) -> str:
    cold = [r for r in bench if r.get("phase") == "cold"]
    if not cold:
        return "_(コールドスタート計測なし: `run_compare.py --cold` を実行)_"
    body = [
        [
            r["impl"],
            _fmt(float(r["e2e_latency_ms"])),
            str(r.get("http_status")),
            "OK" if r.get("ok") else "FAIL",
        ]
        for r in cold
    ]
    return _mdtable(["impl", "E2E (ms)", "http", "結果"], body)


def section_steps(bench: list[dict[str, Any]], server: list[dict[str, Any]]) -> str:
    """ステップ別レイテンシ。応答同梱 trace を優先し、無ければサーバ側 JSONL を使う。"""
    values: dict[tuple[str, str], list[float]] = defaultdict(list)
    tokens_in: dict[tuple[str, str], list[int]] = defaultdict(list)
    tokens_out: dict[tuple[str, str], list[int]] = defaultdict(list)

    for row in bench:
        if not row.get("ok"):
            continue
        for entry in row.get("trace", []) or []:
            key = (row["impl"], entry.get("step", "?"))
            values[key].append(float(entry.get("latency_ms", 0.0)))
            if isinstance(entry.get("input_tokens"), int):
                tokens_in[key].append(entry["input_tokens"])
            if isinstance(entry.get("output_tokens"), int):
                tokens_out[key].append(entry["output_tokens"])

    if not values:
        for row in server:
            key = (row.get("impl", "?"), row.get("step", "?"))
            values[key].append(float(row.get("latency_ms", 0.0)))
            if isinstance(row.get("input_tokens"), int):
                tokens_in[key].append(row["input_tokens"])
            if isinstance(row.get("output_tokens"), int):
                tokens_out[key].append(row["output_tokens"])

    if not values:
        return "_(ステップ別データなし)_"

    body = []
    for impl in sorted({k[0] for k in values}):
        for step in STEP_ORDER:
            key = (impl, step)
            if key not in values:
                continue
            lat = values[key]
            body.append(
                [
                    impl,
                    step,
                    str(len(lat)),
                    _fmt(statistics.median(lat)),
                    _fmt(_pct(lat, 0.95)),
                    _fmt(statistics.mean(tokens_in[key])) if tokens_in[key] else "n/a",
                    _fmt(statistics.mean(tokens_out[key])) if tokens_out[key] else "n/a",
                ]
            )
    return _mdtable(
        ["impl", "step", "n", "P50 (ms)", "P95 (ms)", "avg in-tok", "avg out-tok"], body
    )


def section_stability(bench: list[dict[str, Any]]) -> str:
    """同一入力を繰り返したときの出力ぶれ。"""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in bench:
        if row.get("phase") == "warm" and row.get("ok"):
            grouped[(row["impl"], row["input_id"])].append(row)

    if not grouped:
        return "_(warm サンプルなし)_"

    body = []
    for (impl, input_id), rows in sorted(grouped.items()):
        chars = [int(r.get("final_answer_chars", 0)) for r in rows]
        subtasks = [int(r.get("subtask_count", 0)) for r in rows]
        confidences = [
            float(r["confidence"])
            for r in rows
            if isinstance(r.get("confidence"), (int, float))
        ]
        unique = len({r.get("final_answer", "") for r in rows})
        body.append(
            [
                impl,
                input_id,
                str(len(rows)),
                _fmt(statistics.mean(chars)),
                f"{statistics.pstdev(chars):,.0f}" if len(chars) > 1 else "-",
                str(sorted(set(subtasks))),
                f"{statistics.mean(confidences):.2f}" if confidences else "n/a",
                f"{unique}/{len(rows)}",
            ]
        )
    return _mdtable(
        [
            "impl",
            "input",
            "n",
            "avg chars",
            "sd chars",
            "subtask 数",
            "avg confidence",
            "ユニーク回答数",
        ],
        body,
    )


def section_errors(bench: list[dict[str, Any]]) -> str:
    failures = [r for r in bench if not r.get("ok")]
    if not failures:
        return "_(失敗サンプルなし)_"
    body = [
        [
            r.get("impl", "?"),
            r.get("input_id", "?"),
            str(r.get("http_status")),
            (r.get("error") or "")[:120].replace("\n", " "),
        ]
        for r in failures[:40]
    ]
    return _mdtable(["impl", "input", "http", "error (先頭 120 字)"], body)


def build_report(bench: list[dict[str, Any]], server: list[dict[str, Any]]) -> str:
    parts = [
        "<!-- generated by bench/analyze.py -->",
        "",
        "### 6.2-a エンドツーエンド レイテンシ (warm)",
        "",
        section_e2e(bench),
        "",
        "### 6.2-b コールドスタート",
        "",
        section_cold(bench),
        "",
        "### 6.2-c ステップ別レイテンシ / トークン",
        "",
        section_steps(bench, server),
        "",
        "### 6.2-d 出力の安定性 (同一入力の反復)",
        "",
        section_stability(bench),
        "",
        "### 6.2-e 失敗サンプル",
        "",
        section_errors(bench),
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description="bench JSONL を Markdown へ集計")
    parser.add_argument("--logs", default=str(LOGS))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    logs = Path(args.logs)
    bench = _read_jsonl(logs / "bench.jsonl")
    server: list[dict[str, Any]] = []
    for path in sorted(logs.glob("*.jsonl")):
        if path.name == "bench.jsonl":
            continue
        server.extend(_read_jsonl(path))

    report = build_report(bench, server)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Saved -> {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
