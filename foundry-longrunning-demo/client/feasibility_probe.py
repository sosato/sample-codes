"""Run non-interactive feasibility checks against the deployed sample agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncOpenAI

from conversation import Conversation, ConversationEvent, TurnSnapshot

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"
_FOUNDRY_RESPONSES_API_VERSION = "v1"


def _openai_base_url(responses_url: str) -> str:
    parsed = urlsplit(responses_url)
    path = parsed.path.rstrip("/").removesuffix("/responses")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _openai_default_query(responses_url: str) -> dict[str, str]:
    query = dict(parse_qsl(urlsplit(responses_url).query, keep_blank_values=True))
    query.setdefault("api-version", _FOUNDRY_RESPONSES_API_VERSION)
    return query


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Deployed Responses endpoint.")
    parser.add_argument("--out", required=True, type=Path, help="Result JSON path.")
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser


async def _wait_for(
    conversation: Conversation,
    predicate,
    *,
    timeout: float,
) -> tuple[TurnSnapshot, list[dict[str, Any]]]:
    deadline = monotonic() + timeout
    events: list[dict[str, Any]] = []
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for the expected agent state.")
        event: ConversationEvent = await asyncio.wait_for(
            conversation.next_event(),
            timeout=remaining,
        )
        event_type = event.protocol_event.type if event.protocol_event else None
        events.append(
            {
                "turn_id": event.turn.id,
                "response_id": event.turn.response_id,
                "status": event.turn.status,
                "connection": event.turn.connection,
                "steerable": event.turn.steerable,
                "approval": event.turn.approval.action
                if event.turn.approval
                else None,
                "event_type": event_type,
            }
        )
        if predicate(event.turn):
            return event.turn, events


async def _hitl_check(client: AsyncOpenAI, timeout: float) -> dict[str, Any]:
    conversation = Conversation(client, reconnect_timeout=timeout)
    try:
        conversation.send(
            "Plan and book a two-night business trip to Paris. "
            "Use the recommended nonstop flight and the highest-rated hotel."
        )
        approval_turn, first_events = await _wait_for(
            conversation,
            lambda turn: turn.connection == "terminal" and turn.approval is not None,
            timeout=timeout,
        )
        approval = approval_turn.approval
        assert approval is not None
        conversation.approve_current()
        final_turn, second_events = await _wait_for(
            conversation,
            lambda turn: turn.connection == "terminal"
            and turn.id != approval_turn.id,
            timeout=timeout,
        )
        confirmation_present = "TRIP-" in final_turn.output_text
        return {
            "passed": final_turn.status == "completed" and confirmation_present,
            "approval_action": approval.action,
            "approval_arguments": approval.arguments,
            "approval_response_id": approval_turn.response_id,
            "final_response_id": final_turn.response_id,
            "final_status": final_turn.status,
            "confirmation_present": confirmation_present,
            "output_excerpt": final_turn.output_text[-800:],
            "event_count": len(first_events) + len(second_events),
        }
    finally:
        await conversation.close()


async def _steering_check(client: AsyncOpenAI, timeout: float) -> dict[str, Any]:
    conversation = Conversation(
        client,
        token_delay=0.15,
        reconnect_timeout=timeout,
    )
    try:
        first = conversation.send(
            "Create a detailed five-city Japan itinerary with transport, hotels, "
            "daily activities, and a budget."
        )
        active_turn, first_events = await _wait_for(
            conversation,
            lambda turn: turn.id == first.id
            and turn.connection == "streaming"
            and turn.steerable,
            timeout=timeout,
        )
        second = conversation.send(
            "Change direction: make it a concise two-day Kyoto-only itinerary."
        )
        _, later_events = await _wait_for(
            conversation,
            lambda turn: turn.id == second.id and turn.connection == "terminal",
            timeout=timeout,
        )
        snapshots = conversation.turns
        first_final = next(turn for turn in snapshots if turn.id == active_turn.id)
        second_final = next(turn for turn in snapshots if turn.id == second.id)
        redirected = (
            "Kyoto" in second_final.output_text
            or "京都" in second_final.output_text
        )
        return {
            "passed": second_final.status == "completed" and redirected,
            "first_response_id": first_final.response_id,
            "first_status": first_final.status,
            "second_response_id": second_final.response_id,
            "second_status": second_final.status,
            "steerable_advertised": active_turn.steerable,
            "redirected_output_present": redirected,
            "output_excerpt": second_final.output_text[-800:],
            "event_count": len(first_events) + len(later_events),
        }
    finally:
        await conversation.close()


async def _crash_recovery_check(
    client: AsyncOpenAI,
    timeout: float,
) -> dict[str, Any]:
    conversation = Conversation(
        client,
        reconnect_delay=1.0,
        reconnect_timeout=timeout,
    )
    try:
        turn = conversation.send(
            "Call simulate_crash exactly once, recover, and report the result."
        )
        final_turn, events = await _wait_for(
            conversation,
            lambda current: current.id == turn.id
            and current.connection == "terminal",
            timeout=timeout,
        )
        recovery_states = sum(
            event["connection"] == "recovering" for event in events
        )
        recovery_text = (
            "Crash recovery succeeded" in final_turn.output_text
            or "復旧" in final_turn.output_text
        )
        return {
            "passed": (
                final_turn.status == "completed"
                and recovery_states > 0
                and recovery_text
            ),
            "response_id": final_turn.response_id,
            "status": final_turn.status,
            "recovering_state_count": recovery_states,
            "recovery_text_present": recovery_text,
            "output_excerpt": final_turn.output_text[-800:],
            "event_count": len(events),
        }
    finally:
        await conversation.close()


async def _main(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        _AZURE_AI_SCOPE,
    )

    async def api_key() -> str:
        return await asyncio.to_thread(token_provider)

    started = monotonic()
    async with AsyncOpenAI(
        base_url=_openai_base_url(args.url),
        api_key=api_key,
        default_query=_openai_default_query(args.url),
        max_retries=0,
    ) as client:
        results = {
            "endpoint": args.url,
            "human_in_the_loop": await _hitl_check(client, args.timeout),
            "steering": await _steering_check(client, args.timeout),
            "crash_recovery_and_reconnect": await _crash_recovery_check(
                client,
                args.timeout,
            ),
        }

    results["duration_seconds"] = round(monotonic() - started, 2)
    results["passed"] = all(
        check["passed"]
        for key, check in results.items()
        if isinstance(check, dict) and "passed" in check
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parser().parse_args())))
