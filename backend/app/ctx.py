"""Per-research state shared by the harness steps, plus live document/field events for the UI."""
import asyncio
from dataclasses import dataclass, field

from app import config, events
from app.db import insert, update

_DOC_COLS = ("id", "task_id", "key", "name", "issuer", "type", "period", "url", "about", "covers", "pages", "status")
_SUB_COLS = ("id", "task_id", "objective", "status", "round", "loop", "restarts")
_FIELD_COLS = ("id", "task_id", "subtask_id", "name", "entity", "period", "kind", "hint", "status", "accepted",
               "value", "unit", "reported_period", "source_url", "source_key", "page", "source_kind", "note")


@dataclass
class TaskCtx:
    conn: object
    conv_id: int
    task_id: int
    today: str
    store: object  # PdfStore
    inflight: dict[str, asyncio.Future] = field(default_factory=dict)  # PDF url -> verification in progress
    pages_used: int = 0
    search_history: list = field(default_factory=list)  # research-wide web searches, for the web agent

    def pages_left(self) -> bool:
        return self.pages_used < config.MAX_PAGES

    def doc(self, key: str) -> dict | None:
        r = self.conn.execute("select * from documents where task_id=? and key=?", (self.task_id, key)).fetchone()
        return dict(r) if r else None

    def docs(self, status: str = "verified") -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "select * from documents where task_id=? and status=? order by id", (self.task_id, status))]

    def _set(self, table: str, kind: str, cols_out: tuple, row: dict, cols: dict) -> None:
        if not cols:
            return
        update(self.conn, table, row["id"], **cols)
        row.update(cols)
        events.publish(self.conv_id, {"type": kind, kind: {k: row.get(k) for k in cols_out}})

    def set_doc(self, row: dict, **cols) -> None:
        self._set("documents", "document", _DOC_COLS, row, cols)

    def set_field(self, row: dict, **cols) -> None:
        self._set("fields", "field", _FIELD_COLS, row, cols)

    def set_subtask(self, row: dict, **cols) -> None:
        self._set("subtasks", "subtask", _SUB_COLS, row, cols)

    def post(self, content: str, kind: str = "progress") -> None:
        """A message in the research's conversation (shown in chat)."""
        msg = {"role": "assistant", "content": content, "kind": kind, "task_id": self.task_id}
        msg["id"] = insert(self.conn, "messages", conversation_id=self.conv_id, **msg)
        events.publish(self.conv_id, {"type": "message", "message": msg})


def date_note(today: str) -> str:
    """Spell out which periods are already over; both small and large models got this wrong."""
    y, m = int(today[:4]), int(today[5:7])
    fy = y if m >= 4 else y - 1  # Indian financial year April-March
    return (f"Today is {today}. Calendar year {y - 1} has ended, and FY{fy - 1}-{str(fy)[2:]} "
            f"(April {fy - 1} to March {fy}) has ended; their annual reports are usually out by now.")


def new_state(row: dict) -> dict:
    """A sub-task row plus its runtime exploration state (never written to the DB)."""
    return {**row, "urls": set(), "queries": [], "read": {}, "done_docs": set(), "focus": None,
            "stall": {}, "no_progress": set()}
