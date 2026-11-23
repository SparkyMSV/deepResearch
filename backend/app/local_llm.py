"""Single gateway to the local MLX Qwen model: one job at a time, chat first."""
import asyncio
import concurrent.futures
import itertools
import json
import re
import weakref
from typing import Callable

from app import config

CHAT, WORK = 0, 1

_counter = itertools.count()
_queues: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple]" = weakref.WeakKeyDictionary()
_model = None  # (model, tokenizer), loaded once per process
# own thread so page fetches and OCR in the default executor can't delay chat turns
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")


def _mlx_backend(messages: list[dict], max_tokens: int) -> str:
    global _model
    from mlx_lm import generate, load

    if _model is None:
        _model = load(config.LOCAL_MODEL)
    m, t = _model
    prompt = t.apply_chat_template(messages, add_generation_prompt=True, tokenize=False, enable_thinking=False)
    return generate(m, t, prompt=prompt, max_tokens=max_tokens)


_backend: Callable[[list[dict], int], str] = _mlx_backend


def set_backend(fn: Callable[[list[dict], int], str]) -> None:
    global _backend
    _backend = fn


async def _worker(q: asyncio.PriorityQueue) -> None:
    while True:
        _, _, messages, max_tokens, fut = await q.get()
        if fut.done():
            continue
        try:
            result = await asyncio.get_running_loop().run_in_executor(_pool, _backend, messages, max_tokens)
        except Exception as e:  # deliver to the caller, keep the worker alive
            if not fut.done():
                fut.set_exception(e)
        else:
            if not fut.done():  # caller may have been cancelled mid-generation
                fut.set_result(result)


async def qwen(messages: list[dict], priority: int = WORK, max_tokens: int = 800) -> str:
    loop = asyncio.get_running_loop()
    if loop not in _queues:  # queue + worker are per event loop
        q = asyncio.PriorityQueue()
        # keep the task referenced: asyncio only holds weak refs to tasks
        _queues[loop] = (q, loop.create_task(_worker(q)))
    q = _queues[loop][0]
    fut = loop.create_future()
    await q.put((priority, next(_counter), messages, max_tokens, fut))
    return await fut


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
