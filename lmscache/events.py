"""A minimal event bus: anything that changes state calls notify(); SSE clients get a nudge."""

from __future__ import annotations

import asyncio


class Bus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def notify(self, kind: str = "state") -> None:
        """Safe to call from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._push, kind)

    def _push(self, kind: str) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(kind)
            except asyncio.QueueFull:
                pass


bus = Bus()
