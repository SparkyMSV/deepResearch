import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import openai

from . import config

_BASE = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
}


COOLDOWN_SECONDS = 60
_cool: dict[str, float] = {}  # model -> time.monotonic() when its 429 cooldown ends
_clients: dict[str, Any] = {}  # provider -> client, one per process


class QuotaExhausted(Exception):
    def __init__(self, role: str, retry_after: float | None = None):
        super().__init__(role)
        self.retry_after = retry_after  # seconds; None means daily quota is gone


def _default_factory(provider: str) -> Any:
    url, key = _BASE[provider]
    return openai.AsyncOpenAI(base_url=url, api_key=os.environ.get(key, ""))


_factory: Callable[[str], Any] = _default_factory


def set_client_factory(fn: Callable[[str], Any]) -> None:
    global _factory
    _factory = fn
    _clients.clear()


def _client(provider: str) -> Any:
    if provider not in _clients:
        _clients[provider] = _factory(provider)
    return _clients[provider]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day() -> str:
    return _now().astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _used(conn: sqlite3.Connection, day: str, model: str) -> int:
    row = conn.execute("select used from quota where day=? and model=?", (day, model)).fetchone()
    return row[0] if row else 0


def _set_used(conn: sqlite3.Connection, day: str, model: str, used: int) -> None:
    conn.execute(
        "insert into quota(day, model, used) values(?,?,?) "
        "on conflict(day, model) do update set used=excluded.used",
        (day, model, used),
    )
    conn.commit()


async def cloud(role: str, messages: list[dict], conn: sqlite3.Connection) -> str:
    day = _day()
    cooling = False
    for model in config.ROLES[role]:
        used = _used(conn, day, model)
        if used >= config.DAILY_LIMITS[model]:
            continue
        if _cool.get(model, 0) > time.monotonic():
            cooling = True
            continue
        _set_used(conn, day, model, used + 1)
        try:
            resp = await _client(config.PROVIDER[model]).chat.completions.create(model=model, messages=messages)
        except openai.RateLimitError:  # per-minute limit: cool down briefly; daily limit is the local counter
            _cool[model] = time.monotonic() + COOLDOWN_SECONDS
            cooling = True
            continue
        return resp.choices[0].message.content or ""
    raise QuotaExhausted(role, COOLDOWN_SECONDS if cooling else None)
