"""The tool loop shared by the main, PDF and web agents: memory pauses, max 3 tool calls per turn, a nudge
for text-only replies, server-error recovery and compaction before the context window fills."""
import json
import logging

from app import config
from app.local_llm import LocalModelError, local
from app.logs import clip

log = logging.getLogger(__name__)

MAX_CALLS_PER_TURN, REPLY_TOKENS = 3, 1000
NUDGE = "Use a tool."


def pauser(ctx, sub):
    """on_wait for local(): show a paused status and say why, once per pause; paused['on'] tells the caller
    to restore the status afterwards."""
    paused = {"on": False, "was": sub.get("status")}

    def wait(reason: str) -> None:
        paused.update(on=True, was=sub.get("status"))
        ctx.set_subtask(sub, status="paused_memory")
        ctx.post("The Mac is short of memory, so research is paused until it frees up." if reason == "memory" else
                 "The local research model (MTPLX) isn't answering; research is paused until it is running.")
    return paused, wait


def chars(msgs: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in msgs)


async def loop(ctx, sub, msgs: list[dict], tools: list[dict], handle, max_steps: int, compact,
               finish: tuple[str, ...], stop=None) -> tuple[str, dict | None]:
    """Run tools until a finishing tool is called (returns ("finished", its args)), the step budget is used
    ("steps"), the model stops using tools ("stalled") or the server errors twice ("error").
    handle(name, args) -> result text. compact(msgs) -> shorter msgs for the same task."""
    paused, wait = pauser(ctx, sub)
    steps = idle = tokens = sent = errors = 0
    while True:
        if stop and stop():
            return "closed", None
        if steps >= max_steps:
            return "steps", None
        # last request's size plus what was appended since (~3 chars per token); no count from the server: all
        est = (tokens + chars(msgs[sent:]) // 3) if tokens else chars(msgs) // 3
        if est > config.CONTEXT_TOKENS:
            log.info("compacting at ~%d tokens", est)
            msgs[:] = compact(msgs)
        sent = len(msgs)
        try:
            r = await local("research", msgs, tools=tools, max_tokens=REPLY_TOKENS, on_wait=wait)
            errors = 0
        except LocalModelError as e:  # e.g. prompt over the context window: compact once, then give up
            errors += 1
            log.warning("research model error (%d): %s", errors, e)
            if errors >= 2:
                return "error", None
            msgs[:] = compact(msgs)
            tokens = sent = 0
            continue
        if paused["on"]:
            paused["on"] = False
            ctx.set_subtask(sub, status=paused["was"])
        tokens = r.prompt_tokens
        if not r.tool_calls:
            idle += 1
            if idle >= 2:
                return "stalled", None
            msgs += [{"role": "assistant", "content": r.content or ""}, {"role": "user", "content": NUDGE}]
            continue
        idle = 0
        msgs.append({"role": "assistant", "content": r.content or "", "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
            for c in r.tool_calls]})
        done = None
        for i, c in enumerate(r.tool_calls):
            if i >= MAX_CALLS_PER_TURN:
                result = f"skipped: max {MAX_CALLS_PER_TURN} tool calls per turn"
            elif "_raw" in c["args"]:
                result = f"error: arguments for {c['name']} were not valid JSON"
                steps += 1
            else:
                try:
                    result = await handle(c["name"], c["args"])
                except TypeError as e:
                    result = f"error: bad arguments for {c['name']}: {e}"
                except LocalModelError:
                    raise
                except Exception as e:  # download/OCR/network failure: report it, keep going
                    log.warning("tool %s failed: %r", c["name"], e)
                    result = f"error: {c['name']} failed ({type(e).__name__})"
                steps += 1
                if c["name"] in finish and done is None:
                    done = c["args"]
            log.info("tool %s %s -> %s", c["name"], clip(json.dumps(c["args"], ensure_ascii=False), 300), clip(result, 200))
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if done is not None:
            return "finished", done
