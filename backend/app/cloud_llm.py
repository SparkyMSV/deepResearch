import contextvars
import os
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import openai

from . import config
from .logs import clip

_BASE = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
}


log = logging.getLogger(__name__)
COOLDOWN_SECONDS = 60
_cool: dict[str, float] = {}  # model -> time.monotonic() when its 429 cooldown ends
_clients: dict[str, Any] = {}  # provider -> client, one per process
# set by the task engine: called (failed models, model used) when a call succeeds on a fallback after errors
on_fallback: contextvars.ContextVar[Callable[[list[str], str], None] | None] = \
    contextvars.ContextVar("on_fallback", default=None)


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
    failed: list[str] = []  # models that errored on this call
    for model in config.ROLES[role]:
        used = _used(conn, day, model)
        limit = config.DAILY_LIMITS[model]
        if used >= limit:
            log.info("cloud %s: skip %s, daily quota used %d/%d", role, model, used, limit)
            continue
        if _cool.get(model, 0) > time.monotonic():
            log.info("cloud %s: skip %s, cooling down %.0fs more", role, model, _cool[model] - time.monotonic())
            cooling = True
            continue
        _set_used(conn, day, model, used + 1)
        size = sum(len(m["content"]) for m in messages)
        log.info("cloud %s -> %s (quota %d/%d), prompt %d chars", role, model, used + 1, limit, size)
        log.debug("cloud %s prompt: %s", role, clip(messages[-1]["content"], 400))
        t0 = time.monotonic()
        try:
            resp = await _client(config.PROVIDER[model]).chat.completions.create(model=model, messages=messages)
        # 429 per-minute limit, 5xx overload ("high demand"), timeout or network blip: all temporary, so cool
        # this model down briefly and try the next one; the daily limit is the local counter
        except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError) as e:
            _cool[model] = time.monotonic() + COOLDOWN_SECONDS
            cooling = True
            log.warning("cloud %s: %s temporarily unavailable after %.1fs (%s), cooling down %ds", role, model,
                        time.monotonic() - t0, getattr(e, "status_code", None) or type(e).__name__, COOLDOWN_SECONDS)
            failed.append(model)
            continue
        except openai.APIStatusError as e:
            if e.status_code != 413 or model == config.ROLES[role][-1]:
                log.error("cloud %s: %s failed after %.1fs: %r", role, model, time.monotonic() - t0, e)
                raise
            log.warning("cloud %s: prompt too large for %s (413), trying the next model", role, model)
            failed.append(model)
            continue
        except Exception as e:
            log.error("cloud %s: %s failed after %.1fs: %r", role, model, time.monotonic() - t0, e)
            raise
        out = resp.choices[0].message.content or ""
        log.info("cloud %s <- %s: %d chars in %.1fs", role, model, len(out), time.monotonic() - t0)
        if failed and (note := on_fallback.get()):
            note(failed, model)
        log.debug("cloud %s reply: %s", role, clip(out, 400))
        return out
    log.warning("cloud %s: no model available (%s)", role, "cooling down" if cooling else "daily quota exhausted")
    raise QuotaExhausted(role, COOLDOWN_SECONDS if cooling else None)
