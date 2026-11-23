import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import openai

from . import config

_BASE = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
}


class QuotaExhausted(Exception):
    pass


def _default_factory(provider: str) -> Any:
    url, key = _BASE[provider]
    return openai.AsyncOpenAI(base_url=url, api_key=os.environ.get(key, ""))


_factory: Callable[[str], Any] = _default_factory


def set_client_factory(fn: Callable[[str], Any]) -> None:
    global _factory
    _factory = fn


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
    for model in config.ROLES[role]:
        limit = config.DAILY_LIMITS[model]
        used = _used(conn, day, model)
        if used >= limit:
            continue
        _set_used(conn, day, model, used + 1)
        client = _factory(config.PROVIDER[model])
        try:
            resp = await client.chat.completions.create(model=model, messages=messages)
        except openai.RateLimitError:
            _set_used(conn, day, model, limit)
            continue
        return resp.choices[0].message.content
    raise QuotaExhausted(role)
