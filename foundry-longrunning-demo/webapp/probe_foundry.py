"""Run a lightweight Web Search probe against each selectable model."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from main import MODEL_OPTIONS, TripPlanner, TripRequest


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser()
    command.add_argument(
        "--models",
        nargs="+",
        default=list(MODEL_OPTIONS),
        choices=list(MODEL_OPTIONS),
    )
    command.add_argument("--out", type=Path, default=Path("model-probe-results.json"))
    command.add_argument(
        "--delay",
        type=float,
        default=65.0,
        help="Seconds to wait between model probes.",
    )
    return command


async def main(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    planner = TripPlanner()
    results: dict[str, dict] = {}
    try:
        for index, model in enumerate(args.models):
            if index:
                await asyncio.sleep(args.delay)
            request = TripRequest(
                trip_type="private",
                model=model,
                origin="東京",
                destination="バンコク",
                start_date=date(2027, 8, 8),
                end_date=date(2027, 8, 11),
                adults=2,
                children=2,
                budget="60万円",
                interests="家族向け、寺院、タイ料理、ホテルのプール",
                notes="8歳と11歳の子ども。暑さに配慮して午後に休憩。",
            )
            response = await planner.plan(request)
            results[model] = {
                "passed": bool(response.answer and response.citations),
                "response_id": response.response_id,
                "duration_seconds": response.duration_seconds,
                "citation_count": len(response.citations),
                "answer_excerpt": response.answer[:500],
            }
    finally:
        await planner.close()

    output = {
        "passed": all(result["passed"] for result in results.values()),
        "models": results,
    }
    args.out.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parser().parse_args())))
