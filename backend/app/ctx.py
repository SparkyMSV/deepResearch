"""Per-research state shared by the harness steps, plus live document/field events for the UI."""
import asyncio
from dataclasses import dataclass, field

from app import config, events
from app.db import update

_DOC_COLS = ("id", "task_id", "key", "name", "issuer", "type", "period", "url", "about", "covers", "pages", "status")
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


def new_state(row: dict) -> dict:
    """A sub-task row plus its runtime exploration state (never written to the DB)."""
    return {**row, "urls": set(), "queries": [], "read": {}, "done_docs": set(), "focus": None,
            "stall": {}, "no_progress": set()}
