"""Integration probe for background Responses and SSE progress events."""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx


async def main(base_url: str, model: str) -> int:
    payload = {
        "trip_type": "private",
        "model": model,
        "origin": "東京",
        "destination": "タイ（バンコク中心）",
        "start_date": "2027-08-08",
        "end_date": "2027-08-11",
        "adults": 2,
        "children": 2,
        "budget": "家族4人で総額60万円程度",
        "interests": "家族向け、タイ料理、寺院、ナイトマーケット、プール",
        "notes": "子どもは8歳と11歳。午後に休憩。",
    }
    event_names: list[str] = []
    progress_types: list[str] = []
    result: dict | None = None

    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post(f"{base_url}/api/jobs", json=payload)
        response.raise_for_status()
        job = response.json()
        job_id = job["job_id"]

        async with client.stream(
            "GET",
            f"{base_url}/api/jobs/{job_id}/events",
        ) as stream:
            stream.raise_for_status()
            event_name = "message"
            async for line in stream.aiter_lines():
                if line.startswith("event: "):
                    event_name = line.removeprefix("event: ")
                    event_names.append(event_name)
                elif line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                    if event_name == "progress":
                        progress_types.append(data["event"]["type"])
                    elif event_name == "result":
                        result = data
                    elif event_name == "failure":
                        raise RuntimeError(data["message"])
                elif not line and event_name == "done":
                    break

    checks = {
        "job_id": job_id,
        "response_id": result["summary"]["response_id"] if result else None,
        "metadata_event": "metadata" in event_names,
        "progress_event": "progress" in event_names,
        "result_event": "result" in event_names,
        "done_event": "done" in event_names,
        "web_search_event": any("web_search_call" in item for item in progress_types),
        "completed_event": "response.completed" in progress_types,
        "required_sections": (
            result is not None
            and {
                "flights",
                "hotels",
                "itinerary",
                "budget",
                "checklist",
            }.issubset(
                {section["key"] for section in result["result"]["sections"]}
            )
        ),
        "event_count": len(progress_types),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(
        value
        for key, value in checks.items()
        if key not in {"job_id", "response_id", "event_count"}
    ) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8092")
    parser.add_argument("--model", default="gpt-5.6-luna")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.url.rstrip("/"), args.model)))
