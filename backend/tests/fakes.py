"""Shared fakes: an in-memory PDF store and a scripted Qwen."""
import json

from app.local_llm import Reply

from app.pdf import Contents, norm


class FakeStore:
    def __init__(self, docs: dict[str, list[str]], contents: dict[str, list[tuple[str, int]]] | None = None,
                 broken: set[str] = frozenset()):
        self.docs, self.toc, self.broken, self.reads = docs, contents or {}, set(broken), []

    async def pages(self, url):
        if url in self.broken or url not in self.docs:
            raise OSError(f"download failed: {url}")
        return len(self.docs[url])

    async def text(self, url, n):
        await self.pages(url)
        self.reads.append((url, n))
        return self.docs[url][n - 1]

    async def text_layer(self, url):
        await self.pages(url)
        return {} if url in getattr(self, "scanned", set()) else {i: t for i, t in enumerate(self.docs[url], 1)}

    async def spread(self, url):
        return url in getattr(self, "spreads", set())

    async def contents(self, url):
        entries = self.toc.get(url, [])
        return Contents(entries, "" if len(entries) >= 5 else "\n".join(self.docs[url][0].splitlines()[:40]))

    async def find(self, url, terms):
        await self.pages(url)
        out = {}
        for i, t in enumerate(self.docs[url], 1):
            for term in terms:
                if term in " ".join(t.lower().split()):
                    out.setdefault(term, []).append(i)
        return out

    async def locate(self, url, title, printed):
        n = len(self.docs[url])
        for d in range(16):
            for p in ((printed,) if d == 0 else (printed + d, printed - d)):
                if 1 <= p <= n and norm(title).lower() in norm(self.docs[url][p - 1]).lower():
                    return p, True
        return min(max(printed, 1), n), False

    def close(self):
        pass


class ScriptedQwen:
    """First rule whose substring is in the prompt answers; a rule's reply may be a list (consumed in order),
    a callable(prompt) or a dict/str."""

    def __init__(self, *rules):
        self.rules, self.prompts = [list(r) for r in rules], []

    async def __call__(self, messages, priority=None, max_tokens=800, on_token=None, on_wait=None):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        for rule in self.rules:
            if rule[0] in prompt:
                reply = rule[1]
                if isinstance(reply, list):
                    reply = reply.pop(0) if len(reply) > 1 else reply[0]
                if callable(reply):
                    reply = reply(prompt)
                return reply if isinstance(reply, str) else json.dumps(reply)
        return "{}"


class Script:
    """Fake local(): scripted Replies (or callables taking on_wait), recording what was sent."""

    def __init__(self, *replies, default=None):
        self.replies, self.sent, self.plans, self.default = list(replies), [], [], default

    async def __call__(self, role, messages, tools=None, max_tokens=800, think=False, on_token=None, on_wait=None,
                       max_wait=None):
        import json as _json
        from app.agentkit import PLAN_ASK, REPLAN_ASK
        entry = {"role": role, "messages": _json.loads(_json.dumps(messages)), "think": think, "tools": tools}
        if messages and messages[-1].get("content") in (PLAN_ASK, REPLAN_ASK):  # an agent's planning call
            self.plans.append(entry)
            return Reply("PLAN: look in the borrowings note", [], 10)
        self.sent.append(entry)
        r = self.replies.pop(0) if self.replies else self.default
        if callable(r):
            r = r(on_wait)
        return r


def call(name, i=0, **args):
    return {"id": f"c{name}{i}", "name": name, "args": args}


def tool_out(sent: dict) -> str:
    """The latest tool result an agent saw in a request (planning messages may follow it)."""
    return next((m["content"] for m in reversed(sent["messages"]) if m.get("role") == "tool"),
                sent["messages"][-1]["content"])
