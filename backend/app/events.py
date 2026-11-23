"""In-process per-conversation fan-out for SSE."""
import asyncio

_subs: dict[int, set[asyncio.Queue]] = {}


def subscribe(conv_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subs.setdefault(conv_id, set()).add(q)
    return q


def unsubscribe(conv_id: int, q: asyncio.Queue) -> None:
    subs = _subs.get(conv_id, set())
    subs.discard(q)
    if not subs:
        _subs.pop(conv_id, None)


def publish(conv_id: int, event: dict) -> None:
    for q in list(_subs.get(conv_id, ())):
        q.put_nowait(event)
