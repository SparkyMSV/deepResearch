"""Single gateway to the local MLX Qwen model: one job at a time, chat first."""
import asyncio
import concurrent.futures
import itertools
import json
import logging
import re
import time
import weakref
from typing import Callable

from app import config
from app.logs import clip

log = logging.getLogger(__name__)

CHAT, WORK = 0, 1

_counter = itertools.count()
_queues: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple]" = weakref.WeakKeyDictionary()
_model = None  # (model, tokenizer), loaded once per process
# own thread so page fetches and OCR in the default executor can't delay chat turns
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")


def _mlx_backend(messages: list[dict], max_tokens: int, on_token: Callable[[str], None] | None = None) -> str:
    global _model
    from mlx_lm import generate, load, stream_generate

    if _model is None:
        _model = load(config.LOCAL_MODEL)
    m, t = _model
    prompt = t.apply_chat_template(messages, add_generation_prompt=True, tokenize=False, enable_thinking=False)
    if on_token is None:
        return generate(m, t, prompt=prompt, max_tokens=max_tokens)
    out = []
    for r in stream_generate(m, t, prompt=prompt, max_tokens=max_tokens):
        out.append(r.text)
        on_token(r.text)
    return "".join(out)


_backend: Callable[..., str] = _mlx_backend


def set_backend(fn: Callable[..., str]) -> None:
    global _backend
    _backend = fn


async def _worker(q: asyncio.PriorityQueue) -> None:
    while True:
        _, _, messages, max_tokens, on_token, timing, fut = await q.get()
        if fut.done():
            continue
        timing["start"] = time.monotonic()
        loop = asyncio.get_running_loop()
        # tokens arrive on the MLX thread; hop back to the loop before touching asyncio state
        args = (messages, max_tokens) if on_token is None else \
            (messages, max_tokens, lambda t: loop.call_soon_threadsafe(on_token, t))
        try:
            result = await loop.run_in_executor(_pool, _backend, *args)
        except Exception as e:  # deliver to the caller, keep the worker alive
            if not fut.done():
                fut.set_exception(e)
        else:
            if not fut.done():  # caller may have been cancelled mid-generation
                fut.set_result(result)


async def qwen(messages: list[dict], priority: int = WORK, max_tokens: int = 800,
               on_token: Callable[[str], None] | None = None) -> str:
    loop = asyncio.get_running_loop()
    if loop not in _queues:  # queue + worker are per event loop
        q = asyncio.PriorityQueue()
        # keep the task referenced: asyncio only holds weak refs to tasks
        _queues[loop] = (q, loop.create_task(_worker(q)))
    q = _queues[loop][0]
    fut = loop.create_future()
    timing = {"queued": time.monotonic()}  # the worker stamps "start"; logged here to keep the caller's ids
    kind = "chat" if priority == CHAT else "work"
    log.debug("qwen %s queued (%d ahead), prompt %d chars: %s", kind, q.qsize(),
              sum(len(m["content"]) for m in messages), clip(messages[-1]["content"], 300))
    await q.put((priority, next(_counter), messages, max_tokens, on_token, timing, fut))
    try:
        out = await fut
    except Exception as e:
        log.warning("qwen %s failed: %r", kind, e)
        raise
    end = time.monotonic()
    start = timing.get("start", end)
    log.info("qwen %s: waited %.1fs, generated %d chars in %.1fs%s", kind, start - timing["queued"],
             len(out), end - start, " (streamed)" if on_token else "")
    log.debug("qwen %s reply: %s", kind, clip(out, 400))
    return out


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
