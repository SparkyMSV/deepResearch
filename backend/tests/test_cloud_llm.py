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
    cloud_llm._cool.clear()
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
    assert used(conn, "qwen/qwen3.8-27b") == 1  # a 429 is a cooldown, not daily exhaustion


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


def test_cooldown_skips_then_recovers(setup, monkeypatch):
    conn, fake = setup
    clock = [100.0]
    monkeypatch.setattr(cloud_llm.time, "monotonic", lambda: clock[0])
    fake.fail = {"qwen/qwen3.8-27b", "gemini-3.1-flash-lite"}
    with pytest.raises(cloud_llm.QuotaExhausted) as e:
        asyncio.run(cloud_llm.cloud("checkin", MSGS, conn))
    assert e.value.retry_after == 60
    fake.calls.clear()
    fake.fail = set()
    with pytest.raises(cloud_llm.QuotaExhausted):  # both still cooling: no calls made
        asyncio.run(cloud_llm.cloud("checkin", MSGS, conn))
    assert fake.calls == []
    clock[0] += 61
    assert asyncio.run(cloud_llm.cloud("checkin", MSGS, conn)) == "from:qwen/qwen3.8-27b"


def test_daily_exhaustion_has_no_retry_after(setup):
    conn, _ = setup
    day = cloud_llm._day()
    conn.executemany("insert into quota(day, model, used) values(?,?,?)",
                     [(day, "qwen/qwen3.8-27b", 1000), (day, "gemini-3.1-flash-lite", 500)])
    with pytest.raises(cloud_llm.QuotaExhausted) as e:
        asyncio.run(cloud_llm.cloud("checkin", MSGS, conn))
    assert e.value.retry_after is None


def test_none_content_is_empty(setup):
    conn, fake = setup
    msg = type("M", (), {"content": None})

    async def create(model, messages):
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    fake.create = create
    assert asyncio.run(cloud_llm.cloud("checkin", MSGS, conn)) == ""


def test_client_cached_per_provider(setup):
    conn, fake = setup
    made = []
    cloud_llm.set_client_factory(lambda p: made.append(p) or fake)
    asyncio.run(cloud_llm.cloud("checkin", MSGS, conn))
    asyncio.run(cloud_llm.cloud("checkin", MSGS, conn))
    assert made == ["groq"]


@pytest.mark.parametrize("err", [
    openai.InternalServerError("high demand", response=httpx.Response(503, request=httpx.Request("POST", "http://x")),
                               body=None),
    openai.APITimeoutError(request=httpx.Request("POST", "http://x")),
])
def test_overload_or_timeout_falls_back_then_waits(setup, err):
    conn, fake = setup

    async def flaky(model, messages):
        fake.calls.append(model)
        raise err

    fake.create = flaky
    with pytest.raises(cloud_llm.QuotaExhausted) as e:  # every model busy: the task engine waits and retries
        asyncio.run(cloud_llm.cloud("pdf", MSGS, conn))
    assert fake.calls == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
    assert e.value.retry_after == cloud_llm.COOLDOWN_SECONDS


def test_bad_key_still_raises(setup):
    conn, fake = setup

    async def denied(model, messages):
        raise openai.AuthenticationError("bad key", response=httpx.Response(
            401, request=httpx.Request("POST", "http://x")), body=None)

    fake.create = denied
    with pytest.raises(openai.AuthenticationError):
        asyncio.run(cloud_llm.cloud("pdf", MSGS, conn))
