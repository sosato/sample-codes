"""Probe the deployed hosted Trip Advisor with a private family trip."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncOpenAI

from conversation import Conversation

AI_SCOPE = "https://ai.azure.com/.default"


def base_url(responses_url: str) -> str:
    parsed = urlsplit(responses_url)
    path = parsed.path.rstrip("/").removesuffix("/responses")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


async def main(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    provider = get_bearer_token_provider(DefaultAzureCredential(), AI_SCOPE)

    async def api_key() -> str:
        return await asyncio.to_thread(provider)

    query = dict(parse_qsl(urlsplit(args.url).query, keep_blank_values=True))
    query.setdefault("api-version", "v1")
    started = monotonic()
    events = 0

    async with AsyncOpenAI(
        base_url=base_url(args.url),
        api_key=api_key,
        default_query=query,
        max_retries=0,
    ) as client:
        conversation = Conversation(client, reconnect_timeout=args.timeout)
        try:
            turn = conversation.send(
                "Private Tripです。2027年8月8日から11日まで、東京発で、"
                "大人2人と8歳・11歳の子ども2人、家族4人でバンコクへ3泊4日。"
                "予算60万円程度で、フライト、ホテル、家族向けアクティビティを"
                "Web検索し、休憩を含む日別旅程と出典を提案してください。予約は不要です。"
            )
            while True:
                event = await asyncio.wait_for(
                    conversation.next_event(),
                    timeout=args.timeout,
                )
                events += 1
                if event.turn.id == turn.id and event.turn.connection == "terminal":
                    final = event.turn
                    break
        finally:
            await conversation.close()

    output = {
        "passed": (
            final.status == "completed"
            and ("フライト" in final.output_text or "航空" in final.output_text)
            and "ホテル" in final.output_text
            and ("アクティビティ" in final.output_text or "旅程" in final.output_text)
            and ("http" in final.output_text or "出典" in final.output_text)
        ),
        "status": final.status,
        "response_id": final.response_id,
        "duration_seconds": round(monotonic() - started, 2),
        "event_count": events,
        "output_excerpt": final.output_text[:1200],
    }
    args.out.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
