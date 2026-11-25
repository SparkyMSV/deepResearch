"""Local models over MTPLX's OpenAI-compatible servers: Bonsai 2 27B for research, Qwen 3.5 4B for chat.

Each server runs one request at a time and refuses (HTTP 507) a request the Mac can't fit in memory;
both that and a stopped server are waited out here, not treated as failures.
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import openai

from app import config
from app.logs import clip

log = logging.getLogger(__name__)

CHAT, WORK = 0, 1
FIRST_WAIT, MAX_WAIT_STEP = 10, 120


@dataclass
class Reply:
    content: str
    tool_calls: list[dict] = field(default_factory=list)  # {"id", "name", "args": dict}
    prompt_tokens: int = 0
    reasoning: str = ""  # the thinking, when thinking was on


class LocalModelError(Exception):
    """The local model server answered with an error other than a memory refusal (e.g. prompt too long)."""


class LocalUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # "memory" | "down"


def _default_factory(role: str) -> Any:
    url = config.CHAT_URL if role == "chat" else config.RESEARCH_URL
    return openai.AsyncOpenAI(base_url=url, api_key="local", timeout=900, max_retries=0)


_factory: Callable[[str], Any] = _default_factory
_clients: dict[str, Any] = {}
_models: dict[str, str] = {}


def set_client_factory(fn: Callable[[str], Any]) -> None:
    global _factory
    _factory = fn
    _clients.clear()
    _models.clear()


def _client(role: str) -> Any:
    if role not in _clients:
        _clients[role] = _factory(role)
    return _clients[role]


def _args(raw: str) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {"_raw": raw}
    except ValueError:
        return {"_raw": raw}


async def _once(role: str, messages: list[dict], tools, max_tokens: int, think: bool, on_token) -> Reply:
    c = _client(role)
    if role not in _models:
        _models[role] = (await c.models.list()).data[0].id
    kw = dict(model=_models[role], messages=messages, max_tokens=max_tokens,
              extra_body={"chat_template_kwargs": {"enable_thinking": think}})
    if tools:
        kw["tools"] = tools
    if on_token is None:
        r = await c.chat.completions.create(**kw)
        m = r.choices[0].message
        why = getattr(m, "reasoning_content", None) or (getattr(m, "model_extra", None) or {}).get("reasoning_content") or ""
        if think and why:
            log.info("local %s thinking: %s", role, clip(why, 500))
        calls = [{"id": t.id, "name": t.function.name, "args": _args(t.function.arguments)} for t in m.tool_calls or []]
        return Reply(m.content or "", calls, getattr(r.usage, "prompt_tokens", 0) or 0, why if think else "")
    out, used = [], 0
    async for chunk in await c.chat.completions.create(stream=True, stream_options={"include_usage": True}, **kw):
        if chunk.usage:
            used = chunk.usage.prompt_tokens or 0
        if chunk.choices and (t := chunk.choices[0].delta.content):
            out.append(t)
            on_token(t)
    return Reply("".join(out), [], used)


async def local(role: str, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800,
                think: bool = False, on_token: Callable[[str], None] | None = None,
                on_wait: Callable[[str], None] | None = None, max_wait: float | None = None) -> Reply:
    """One local-model call. role: "research" (Bonsai) or "chat" (4B). Thinking is off unless think=True.
    A memory refusal or a stopped server is waited out (on_wait(reason) once per pause), up to max_wait
    seconds (None: forever), then LocalUnavailable."""
    waited, delay, told = 0.0, FIRST_WAIT, False
    t0 = time.monotonic()
    while True:
        try:
            r = await _once(role, messages, tools, max_tokens, think, on_token)
            log.info("local %s: %d prompt tokens, %d chars, %d tool call(s) in %.1fs", role, r.prompt_tokens,
                     len(r.content), len(r.tool_calls), time.monotonic() - t0)
            log.debug("local %s reply: %s %s", role, clip(r.content, 300), clip(r.tool_calls, 300))
            return r
        except openai.APIConnectionError:
            reason = "down"
        except openai.APIStatusError as e:
            if e.status_code != 507:
                raise LocalModelError(f"local {role} model error {e.status_code}: {str(e)[:300]}") from e
            reason = "memory"
        if max_wait is not None and waited >= max_wait:
            raise LocalUnavailable(reason)
        log.warning("local %s unavailable (%s); retrying in %ds", role, reason, delay)
        if on_wait and not told:
            on_wait(reason)
            told = True
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 2, MAX_WAIT_STEP)


async def qwen(messages: list[dict], priority: int = WORK, max_tokens: int = 800,
               on_token: Callable[[str], None] | None = None, on_wait: Callable[[str], None] | None = None) -> str:
    """Plain text from a local model: CHAT goes to the chat model (gives up after ~30 s unavailable),
    WORK to the research model."""
    role = "chat" if priority == CHAT else "research"
    r = await local(role, messages, max_tokens=max_tokens, on_token=on_token, on_wait=on_wait,
                    max_wait=30 if role == "chat" else None)
    return r.content


def parse_json(text: str) -> dict | None:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = text.replace("```json", "").replace("```", "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += (text[i] == "{") - (text[i] == "}")
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except ValueError:
                    break
        start = text.find("{", start + 1)
    return None
