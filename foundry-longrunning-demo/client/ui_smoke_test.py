"""Headless smoke test for the guided Textual demo controls."""

from __future__ import annotations

import asyncio

from app import ResponsesCuiApp
from textual.widgets import Static


class FakeConversation:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.current_turn = None
        self._events: asyncio.Queue = asyncio.Queue()

    def send(self, text: str):
        self.messages.append(text)
        return None

    async def next_event(self):
        return await self._events.get()

    async def cancel_current(self) -> None:
        return None

    async def close(self) -> None:
        return None


async def main() -> None:
    conversation = FakeConversation()
    app = ResponsesCuiApp(conversation)
    async with app.run_test() as pilot:
        await pilot.click("#demo-paris")
        await pilot.pause()
        assert conversation.messages
        assert "バンコク" in conversation.messages[-1]
        assert app.query_one("#status", Static).render().plain == "準備完了"

    print("UI smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
