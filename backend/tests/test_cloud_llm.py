import asyncio
from datetime import datetime, timezone

import httpx
import openai
import pytest

from app import cloud_llm, db

MSGS = [{"role": "user", "content": "hi"}]


def rate_limit():
    resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    return openai.RateLimitError("rl", response=resp, body=None)


class Fake:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []
        self.chat = self
        self.completions = self

    async def create(self, model, messages):
        self.calls.append(model)
        if model in self.fail:
            raise rate_limit()
        msg = type("M", (), {"content": f"from:{model}"})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


@pytest.fixture
def setup():
    conn = db.connect(":memory:")
    fake = Fake()
    cloud_llm.set_client_factory(lambda provider: fake)
    yield conn, fake
    cloud_llm._now = lambda: datetime.now(timezone.utc)


def used(conn, model):
    return conn.execute("select used from quota where model=?", (model,)).fetchone()[0]


def test_primary_used(setup):
    conn, _ = setup
    assert asyncio.run(cloud_llm.cloud("checkin", MSGS, conn)) == "from:qwen/qwen3.8-27b"
    assert used(conn, "qwen/qwen3.8-27b") == 1


def test_fallback_on_429(setup):
    conn, fake = setup
    fake.fail = {"qwen/qwen3.8-27b"}
    assert asyncio.run(cloud_llm.cloud("checkin", MSGS, conn)) == "from:gemini-3.1-flash-lite"
    assert used(conn, "qwen/qwen3.8-27b") == 1000


def test_exhausted(setup):
    conn, fake = setup
    day = cloud_llm._day()
    conn.executemany(
        "insert into quota(day, model, used) values(?,?,?)",
        [(day, "qwen/qwen3.8-27b", 1000), (day, "gemini-3.1-flash-lite", 500)],
    )
    with pytest.raises(cloud_llm.QuotaExhausted):
        asyncio.run(cloud_llm.cloud("checkin", MSGS, conn))
    assert fake.calls == []


def test_quota_day_is_la(setup):
    conn, _ = setup
    cloud_llm._now = lambda: datetime(2026, 11, 25, 7, 30, tzinfo=timezone.utc)
    asyncio.run(cloud_llm.cloud("checkin", MSGS, conn))
    assert conn.execute("select day from quota").fetchone()[0] == "2026-11-24"
