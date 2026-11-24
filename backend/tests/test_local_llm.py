import asyncio
import time

from app.local_llm import CHAT, WORK, parse_json, qwen, set_backend


def test_chat_jumps_queue():
    order = []

    def fake(messages, max_tokens):
        time.sleep(0.05)
        order.append(messages[0]["content"])
        return "x"

    set_backend(fake)

    async def run():
        jobs = [asyncio.create_task(qwen([{"role": "user", "content": f"work{i}"}], WORK)) for i in (1, 2, 3)]
        await asyncio.sleep(0.02)  # work1 is running
        jobs.append(asyncio.create_task(qwen([{"role": "user", "content": "chat"}], CHAT)))
        await asyncio.gather(*jobs)

    asyncio.run(run())
    assert order == ["work1", "chat", "work2", "work3"]


def test_parse_json():
    assert parse_json('<think>x</think>```json\n{"a":1}\n```') == {"a": 1}
    assert parse_json('ok then {"a":{"b":2}} trailing') == {"a": {"b": 2}}
    assert parse_json("no json") is None


def test_cancelled_caller_does_not_kill_worker():
    def slow_then_fast(messages, max_tokens):
        time.sleep(0.2 if messages[0]["content"] == "slow" else 0)
        return "done"

    set_backend(slow_then_fast)

    async def run():
        try:
            await asyncio.wait_for(qwen([{"role": "user", "content": "slow"}]), 0.05)
        except asyncio.TimeoutError:
            pass
        return await asyncio.wait_for(qwen([{"role": "user", "content": "next"}]), 2)

    assert asyncio.run(run()) == "done"


def test_streams_tokens_to_on_token():
    def fake(messages, max_tokens, on_token=None):
        for t in ("Hel", "lo", "!"):
            on_token(t)
        return "Hello!"

    set_backend(fake)
    got = []

    async def run():
        return await qwen([{"role": "user", "content": "hi"}], priority=CHAT, on_token=got.append)

    assert asyncio.run(run()) == "Hello!"
    assert got == ["Hel", "lo", "!"]
