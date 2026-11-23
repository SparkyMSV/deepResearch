import asyncio
import json

import httpx
import openai

from app import config, events, task
from app.cloud_llm import QuotaExhausted
from app.db import Evidence, Hit, connect, insert


class Env:
    """Scripted fakes for search, passages, Qwen, cloud and read_pdf."""

    def __init__(self, monkeypatch, checkins=None, plan=None, hits=None):
        self.conn = connect(":memory:")
        self.conv = insert(self.conn, "conversations", title="c")
        self.task_id = insert(self.conn, "tasks", conversation_id=self.conv, objective="obj", status="queued")
        self.events, self.searches, self.checkin_at, self.prompts, self.pdf_calls = [], [], [], {}, []
        self.checkins = list(checkins or [])
        self.plan = plan or {"subtasks": [{"objective": "s", "query": "q0", "needs_document": ""}]}
        self.hits = hits or (lambda q: [Hit("t", f"http://x/{q}", "snip", published=None, fetched_at="2026-11-24")])
        monkeypatch.setattr(task, "QUOTA_RETRY_SECONDS", 0)
        monkeypatch.setattr(events, "publish", lambda conv_id, e: self.events.append(e))
        monkeypatch.setattr(task, "search", self.search)
        monkeypatch.setattr(task, "passages", self.passages)
        monkeypatch.setattr(task, "qwen", self.qwen)
        monkeypatch.setattr(task, "cloud", self.cloud)
        monkeypatch.setattr(task, "read_pdf", self.read_pdf)

    async def search(self, query, objective, priority=None):
        self.searches.append(query)
        return self.hits(query)

    async def passages(self, hit, objective):
        return [f"fact from {hit.url}"]

    async def qwen(self, messages, priority=None, max_tokens=800):
        return json.dumps({"query": self.searches[-1] if self.searches else "q"})

    async def read_pdf(self, url, objective, subtask_id, loop, conn):
        self.pdf_calls.append(url)
        return [Evidence(subtask_id, loop, url, "doc.pdf", "Revenue 120", "FY24", "2026-11-24", page=9)]

    async def cloud(self, role, messages, conn):
        self.prompts.setdefault(role, []).append(messages[-1]["content"])
        if role == "plan":
            return json.dumps(self.plan)
        if role == "report":
            return "# Report"
        self.checkin_at.append(len(self.searches))
        c = self.checkins.pop(0) if self.checkins else {"decision": "continue"}
        if isinstance(c, Exception):
            raise c
        return json.dumps(c)

    def subtask(self, objective="s", query="q0", needs_document=""):
        return insert(self.conn, "subtasks", task_id=self.task_id, objective=objective, query=query,
                      needs_document=needs_document, status="queued")

    def run_subtask(self, sid):
        asyncio.run(task.run_subtask(self.conn, self.conv, sid))
        return self.conn.execute("select * from subtasks where id=?", (sid,)).fetchone()

    def rounds(self, sid):
        return self.conn.execute("select * from rounds where subtask_id=? order by id", (sid,)).fetchall()


def test_plan_capped_at_8(monkeypatch):
    plan = {"subtasks": [{"objective": f"o{i}", "query": f"q{i}", "needs_document": ""} for i in range(11)]}
    env = Env(monkeypatch, plan=plan)
    monkeypatch.setattr(config, "MAX_SUBTASKS", 8)

    async def noop(conn, conv_id, subtask_id):
        pass

    monkeypatch.setattr(task, "run_subtask", noop)
    asyncio.run(task.run_task(env.conn, env.conv, env.task_id))
    assert env.conn.execute("select count(*) from subtasks").fetchone()[0] == 8


def test_limits_respected(monkeypatch):
    env = Env(monkeypatch)
    monkeypatch.setattr(config, "ROUNDS_PER_LOOP", 7)
    monkeypatch.setattr(config, "MAX_RESTARTS", 0)
    sid = env.subtask()
    st = env.run_subtask(sid)
    assert env.checkin_at == [5, 7]
    assert st["status"] == "partial" and len(env.rounds(sid)) == 7


def test_auth_error_fails_task(monkeypatch):
    env = Env(monkeypatch)
    resp = httpx.Response(401, request=httpx.Request("POST", "http://x"))

    async def bad_cloud(role, messages, conn):
        raise openai.AuthenticationError("bad key", response=resp, body=None)

    monkeypatch.setattr(task, "cloud", bad_cloud)
    out = asyncio.run(task.run_task(env.conn, env.conv, env.task_id))
    assert not out
    assert env.conn.execute("select status from tasks").fetchone()[0] == "failed"
    msgs = [e["message"] for e in env.events if e["type"] == "message"]
    assert any(m["kind"] == "progress" and ".env" in m["content"] for m in msgs)


def test_sufficient_stops_at_round_5(monkeypatch):
    env = Env(monkeypatch, checkins=[{"decision": "sufficient"}])
    sid = env.subtask()
    st = env.run_subtask(sid)
    assert st["status"] == "done" and len(env.rounds(sid)) == 5


def test_continue_forever_ends_partial(monkeypatch):
    env = Env(monkeypatch)
    monkeypatch.setattr(config, "ROUNDS_PER_LOOP", 20)
    monkeypatch.setattr(config, "MAX_RESTARTS", 2)
    sid = env.subtask()
    st = env.run_subtask(sid)
    rounds = env.rounds(sid)
    assert st["status"] == "partial" and len(rounds) == 60
    assert {r["loop"] for r in rounds} == {0, 1, 2}


def test_restart_marks_off_target(monkeypatch):
    restart = {"decision": "restart", "query": "fresh", "objective": "s2", "off_target_loops": [0]}
    env = Env(monkeypatch, checkins=[restart, {"decision": "sufficient"}])
    report = asyncio.run(task.run_task(env.conn, env.conv, env.task_id))
    assert report == "# Report"
    rows = env.conn.execute("select loop, off_target from evidence").fetchall()
    assert rows and all(r["off_target"] == (r["loop"] == 0) for r in rows)
    prompt = env.prompts["report"][0]
    assert "fact from http://x/fresh" in prompt and "fact from http://x/q0" not in prompt
    assert env.conn.execute("select status from tasks").fetchone()[0] == "done"
    assert any(e["type"] == "message" and e["message"]["kind"] == "report" for e in env.events)


def test_quota_pause_resumes(monkeypatch):
    env = Env(monkeypatch, checkins=[QuotaExhausted("checkin"), {"decision": "sufficient"}])
    sid = env.subtask()
    st = env.run_subtask(sid)
    statuses = [e["subtask"]["status"] for e in env.events if e["type"] == "subtask"]
    assert "paused_quota" in statuses
    assert st["status"] == "done"


def test_zero_hits_round_continues(monkeypatch):
    env = Env(monkeypatch, checkins=[{"decision": "sufficient"}], hits=lambda q: [])
    sid = env.subtask()
    env.run_subtask(sid)
    rounds = env.rounds(sid)
    assert len(rounds) == 5 and all(r["hits"] == 0 for r in rounds)


def test_pdf_hit_routed_once(monkeypatch):
    pdf_hit = lambda q: [Hit("doc", "http://x/doc.pdf", "", is_pdf=True)]
    env = Env(monkeypatch, checkins=[{"decision": "sufficient"}], hits=pdf_hit)
    sid = env.subtask()
    env.run_subtask(sid)
    assert env.pdf_calls == ["http://x/doc.pdf"]
    assert env.conn.execute("select page from evidence").fetchall()[0][0] == 9
