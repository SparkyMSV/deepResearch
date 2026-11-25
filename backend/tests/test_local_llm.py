import asyncio
import json
from types import SimpleNamespace as NS

import httpx
import openai
import pytest

from app import config, local_llm
from app.local_llm import CHAT, WORK, LocalUnavailable, local, parse_json, qwen

REQ = httpx.Request("POST", "http://x")


class Fake:
    """Stands in for openai.AsyncOpenAI: scripted replies or exceptions, records calls."""

    def __init__(self, role, script):
        self.role, self.script, self.calls = role, list(script), []
        self.chat = NS(completions=NS(create=self.create))
        self.models = NS(list=self.list)

    async def list(self):
        return NS(data=[NS(id=f"{self.role}-model")])

    async def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, Exception):
            raise item
        if kw.get("stream"):
            async def gen():
                for t in item:
                    yield NS(usage=None, choices=[NS(delta=NS(content=t))])
                yield NS(usage=NS(prompt_tokens=7), choices=[])
            return gen()
        calls = []
        if isinstance(item, dict):
            calls = [NS(id="c1", function=NS(name=item["name"], arguments=item["args"]))]
            item = ""
        return NS(choices=[NS(message=NS(content=item, tool_calls=calls))], usage=NS(prompt_tokens=11))


@pytest.fixture
def fakes(monkeypatch):
    made = {}

    def setup(**scripts):
        local_llm.set_client_factory(lambda role: made.setdefault(role, Fake(role, scripts.get(role, []))))
        return made

    slept = []

    async def no_sleep(s):
        slept.append(s)

    monkeypatch.setattr(local_llm.asyncio, "sleep", no_sleep)
    yield setup, slept
    local_llm.set_client_factory(local_llm._default_factory)


def mem_error():
    return openai.InternalServerError("insufficient memory", response=httpx.Response(507, request=REQ), body=None)


def test_routes_roles_to_servers(fakes):
    setup, _ = fakes
    made = setup(chat=["hi"], research=["deep"])
    assert asyncio.run(local("chat", [{"role": "user", "content": "x"}])).content == "hi"
    assert asyncio.run(local("research", [{"role": "user", "content": "x"}])).content == "deep"
    assert made["chat"].calls[0]["model"] == "chat-model" and made["research"].calls[0]["model"] == "research-model"
    assert config.RESEARCH_URL.endswith(":8002/v1") and config.CHAT_URL.endswith(":8002/v1")


def test_thinking_off_unless_asked(fakes):
    setup, _ = fakes
    made = setup(research=["a", "b"])
    asyncio.run(local("research", [{"role": "user", "content": "x"}]))
    asyncio.run(local("research", [{"role": "user", "content": "x"}], think=True))
    flags = [c["extra_body"]["chat_template_kwargs"]["enable_thinking"] for c in made["research"].calls]
    assert flags == [False, True]


def test_tool_calls_decoded(fakes):
    setup, _ = fakes
    setup(research=[{"name": "pdf_page", "args": '{"doc":"D1","page":3}'}, {"name": "done", "args": "not json"}])
    r = asyncio.run(local("research", [{"role": "user", "content": "x"}], tools=[{"type": "function"}]))
    assert r.tool_calls == [{"id": "c1", "name": "pdf_page", "args": {"doc": "D1", "page": 3}}] and r.prompt_tokens == 11
    r = asyncio.run(local("research", [{"role": "user", "content": "x"}]))
    assert r.tool_calls[0]["args"] == {"_raw": "not json"}


def test_memory_refusal_waits_and_retries(fakes):
    setup, slept = fakes
    setup(research=[mem_error(), mem_error(), "fine"])
    waits = []
    r = asyncio.run(local("research", [{"role": "user", "content": "x"}], on_wait=waits.append))
    assert r.content == "fine" and waits == ["memory"] and slept == [10, 20]


def test_chat_gives_up_after_max_wait(fakes):
    setup, _ = fakes
    setup(chat=[openai.APIConnectionError(request=REQ)] * 5)
    with pytest.raises(LocalUnavailable) as e:
        asyncio.run(local("chat", [{"role": "user", "content": "x"}], max_wait=30))
    assert e.value.reason == "down"


def test_other_errors_raise(fakes):
    setup, _ = fakes
    setup(research=[openai.BadRequestError("bad", response=httpx.Response(400, request=REQ), body=None)])
    with pytest.raises(local_llm.LocalModelError):
        asyncio.run(local("research", [{"role": "user", "content": "x"}]))


def test_qwen_wrapper_and_streaming(fakes):
    setup, _ = fakes
    setup(chat=[["Hel", "lo"]], research=["work"])
    seen = []
    assert asyncio.run(qwen([{"role": "user", "content": "x"}], priority=CHAT, on_token=seen.append)) == "Hello"
    assert seen == ["Hel", "lo"]
    assert asyncio.run(qwen([{"role": "user", "content": "x"}], priority=WORK)) == "work"


def test_parse_json():
    assert parse_json('<think>x</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json("no json") is None
