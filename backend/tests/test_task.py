import asyncio
import json

import httpx
import openai

from app import agent, docs, events, plan, task
from app.cloud_llm import QuotaExhausted
from app.db import connect, insert
from app.local_llm import Reply
from tests.fakes import FakeStore

PLAN = {"documents": [], "subtasks": [{"objective": "Asset quality", "fields": [
    {"name": "Gross NPA %", "entity": "Navi Finserv", "period": "FY2025-26", "kind": "figure"}]}]}
MAIN = [Reply("", [{"id": "w", "name": "search_web", "args": {"fields": [1], "goal": "g"}}], 100),
        Reply("", [{"id": "m", "name": "mark_not_public", "args": {"field": 1, "reason": "not disclosed"}}], 100),
        Reply("", [{"id": "d", "name": "done", "args": {}}], 100)]
ACCEPT = Reply('{"fields":[{"field":1,"action":"accept"}]}', [], 100)


class Env:
    def __init__(self, monkeypatch, cloud_replies=()):
        self.conn = connect(":memory:")
        self.conv = insert(self.conn, "conversations", title="c")
        self.events, self.cloud_replies, self.prompts = [], list(cloud_replies), {}
        self.turn = 0
        monkeypatch.setattr(task, "QUOTA_RETRY_SECONDS", 0)
        monkeypatch.setattr(events, "publish", lambda conv_id, e: self.events.append(e))
        monkeypatch.setattr(task, "cloud", self.cloud)
        monkeypatch.setattr(task, "PdfStore", lambda: FakeStore({}))
        monkeypatch.setattr(agent, "local", self.local)
        monkeypatch.setattr(agent.agentkit, "local", self.local)

        async def web(ctx, sub, fields, goal, on_round):  # the web agent looked and found nothing
            sub.setdefault("missing_web", set()).update(agent.all_fields(ctx.conn, sub["id"])[n - 1]["id"] for n in fields)
            return "Web agent: recorded: nothing; missing: [1]"

        monkeypatch.setattr(agent.web_agent, "search", web)
        monkeypatch.setattr(docs, "ddg", lambda q, n=None: [])

        async def plan_cloud(role, messages, conn):
            return json.dumps(PLAN)

        monkeypatch.setattr(plan, "cloud", plan_cloud)

    def new_task(self, objective="obj"):
        return insert(self.conn, "tasks", conversation_id=self.conv, objective=objective, status="queued")

    async def local(self, role, messages, tools=None, think=False, **kw):
        await asyncio.sleep(0)
        if think:  # self-review
            return ACCEPT
        r = MAIN[self.turn % len(MAIN)]
        self.turn += 1
        return r

    async def cloud(self, role, messages, conn):
        self.prompts.setdefault(role, []).append(messages[-1]["content"])
        if role == "report":
            return "# Report"
        c = self.cloud_replies.pop(0) if self.cloud_replies else {"fields": [{"field": 1, "action": "accept"}]}
        if isinstance(c, Exception):
            raise c
        return json.dumps(c)

    def notes(self):
        return [e["message"]["content"] for e in self.events if e["type"] == "message"]


def test_run_task_end_to_end_with_fakes(monkeypatch):
    env = Env(monkeypatch)
    tid = env.new_task()
    assert asyncio.run(task.run_task(env.conn, env.conv, tid)) == "# Report"
    f = env.conn.execute("select status, accepted from fields").fetchone()
    assert tuple(f) == ("unavailable", 2)  # Bonsai said not public, self-review accepted, final review confirmed
    assert env.conn.execute("select status from subtasks").fetchone()[0] == "done"
    assert "Today: " in env.prompts["report"][0] and "[1] Gross NPA %" in env.prompts["checkin"][0]


def test_second_research_waits_queued(monkeypatch):
    env = Env(monkeypatch)
    a, b = env.new_task("first"), env.new_task("second")

    async def both():
        return await asyncio.gather(task.run_task(env.conn, env.conv, a), task.run_task(env.conn, env.conv, b))

    assert asyncio.run(both()) == ["# Report", "# Report"]
    statuses = [(e["task"]["id"], e["task"]["status"]) for e in env.events if e["type"] == "task"]
    assert (b, "queued") in statuses and statuses.index((a, "done")) < statuses.index((b, "running"))
    assert any("Another research is running" in n for n in env.notes())


def test_report_input_labels_review_levels(monkeypatch):
    env = Env(monkeypatch)
    tid = env.new_task()
    sid = insert(env.conn, "subtasks", task_id=tid, objective="s", status="done")
    for name, acc in (("A", 2), ("B", 1), ("C", 0)):
        insert(env.conn, "fields", task_id=tid, subtask_id=sid, name=name, entity="E", period="FY26", status="found",
               value="1", unit="%", source_key="D1", page=4, quote="q", accepted=acc)
    insert(env.conn, "fields", task_id=tid, subtask_id=sid, name="D", entity="E", period="FY26", status="unavailable",
           note="not disclosed", accepted=2)
    body = task._report_input(env.conn, dict(env.conn.execute("select * from tasks").fetchone()), "2026-11-25")
    assert "confirmed" in body and "checked locally" in body and "unreviewed" in body
    assert "Gaps:\n- D (E, FY26): unavailable - not disclosed" in body


def test_auth_error_fails_task(monkeypatch):
    env = Env(monkeypatch)
    resp = httpx.Response(401, request=httpx.Request("POST", "http://x"))

    async def bad(role, messages, conn):
        raise openai.AuthenticationError("bad key", response=resp, body=None)

    monkeypatch.setattr(plan, "cloud", bad)
    tid = env.new_task()
    assert asyncio.run(task.run_task(env.conn, env.conv, tid)) == ""
    assert env.conn.execute("select status from tasks").fetchone()[0] == "failed"
    assert any(".env" in n for n in env.notes())


def test_quota_pause_in_final_review_resumes_and_tells_once(monkeypatch):
    env = Env(monkeypatch, cloud_replies=[QuotaExhausted("checkin"), {"fields": [{"field": 1, "action": "accept"}]}])
    tid = env.new_task()
    assert asyncio.run(task.run_task(env.conn, env.conv, tid)) == "# Report"
    assert "paused_quota" in [e["task"]["status"] for e in env.events if e["type"] == "task"]
    assert sum("research is paused" in n for n in env.notes()) == 1


def test_switch_to_groq_is_told_once_per_task(monkeypatch):
    env = Env(monkeypatch)
    orig = env.cloud

    async def via_groq(role, messages, conn):
        if note := task.on_fallback.get():
            note(["gemini-3.5-flash-lite"], "qwen/qwen3.8-27b")
        return await orig(role, messages, conn)

    monkeypatch.setattr(task, "cloud", via_groq)
    asyncio.run(task.run_task(env.conn, env.conv, env.new_task()))
    assert env.notes().count("Gemini models are facing issues, so I switched to Groq (qwen/qwen3.8-27b).") == 1


def test_local_model_error_ends_only_the_subtask(monkeypatch):
    from app.local_llm import LocalModelError
    env = Env(monkeypatch)

    async def broken(role, messages, **kw):
        raise LocalModelError("local research model error 400: prompt too long")

    monkeypatch.setattr(agent, "local", broken)
    monkeypatch.setattr(agent.agentkit, "local", broken)
    assert asyncio.run(task.run_task(env.conn, env.conv, env.new_task())) == "# Report"
    assert env.conn.execute("select status from subtasks").fetchone()[0] == "partial"
    assert not any(".env" in n for n in env.notes())
