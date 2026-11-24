"""HTTP API + SSE. Run: uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import config, events, logs
from .chat import _bg as _chat_bg, handle_message
from .db import connect, insert
from .task import _post

log = logging.getLogger(__name__)
_bg: set[asyncio.Task] = set()  # strong refs so background tasks aren't GC'd
ACTIVE = "('running','queued','checking_in','paused_quota')"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # one shared connection, used only on the event-loop thread (routes are async)
    logs.setup()
    app.state.conn = conn = connect(config.DB_PATH)
    log.info("db %s; search region %s, %d results; limits: %d sub-tasks, %d rounds/loop, %d restarts",
             config.DB_PATH, config.SEARCH_REGION, config.SEARCH_MAX_RESULTS, config.MAX_SUBTASKS,
             config.ROUNDS_PER_LOOP, config.MAX_RESTARTS)
    for t in ("tasks", "subtasks"):
        n = conn.execute(f"update {t} set status='interrupted' where status in {ACTIVE}").rowcount
        if n:
            log.warning("startup: marked %d unfinished %s interrupted", n, t)
    conn.commit()
    yield
    pending = [*_bg, *_chat_bg]  # message handlers and research tasks still using conn
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    conn.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"]
)


class Login(BaseModel):
    email: str
    password: str


class Msg(BaseModel):
    text: str


@app.post("/api/login")
async def login(body: Login):
    # shortcut: demo creds, no token check on other routes; add real auth before exposing beyond localhost
    if body.email == config.DEMO_USER and body.password == config.DEMO_PASSWORD:
        return {"token": "demo"}
    raise HTTPException(401, "bad credentials")


def _rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args)]


@app.get("/api/conversations")
async def list_conversations():
    return _rows(app.state.conn, "select id,title,created_at from conversations order by id desc")


@app.post("/api/conversations")
async def create_conversation():
    return {"id": insert(app.state.conn, "conversations", title="New research")}


@app.get("/api/conversations/{cid}")
async def get_conversation(cid: int):
    conn = app.state.conn
    conv = conn.execute("select id,title from conversations where id=?", (cid,)).fetchone()
    if not conv:
        raise HTTPException(404, "not found")
    tasks = _rows(conn, "select * from tasks where conversation_id=? order by id", cid)
    for t in tasks:
        t["subtasks"] = _rows(conn, "select * from subtasks where task_id=? order by id", t["id"])
        for s in t["subtasks"]:
            s["rounds"] = _rows(conn, "select * from rounds where subtask_id=? order by id", s["id"])
    return {
        **dict(conv),
        "messages": _rows(conn, "select * from messages where conversation_id=? order by id", cid),
        "tasks": tasks,
    }


async def _run(conn, cid: int, text: str):
    try:
        await handle_message(conn, cid, text)
    except Exception as e:
        log.exception("handle_message failed")
        _post(conn, cid, None, f"Sorry, something went wrong: {type(e).__name__}.", "text")


@app.post("/api/conversations/{cid}/messages", status_code=202)
async def post_message(cid: int, body: Msg):
    conn = app.state.conn
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty message")
    if not conn.execute("select 1 from conversations where id=?", (cid,)).fetchone():
        raise HTTPException(404, "not found")
    # handle_message stores the user message and sets the title itself
    t = asyncio.create_task(_run(conn, cid, text))
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return {"accepted": True}


@app.get("/api/conversations/{cid}/events")
async def stream(cid: int):
    async def gen():
        q = events.subscribe(cid)
        try:
            while True:
                yield {"data": json.dumps(await q.get())}
        finally:
            events.unsubscribe(cid, q)

    return EventSourceResponse(gen(), ping=15)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
