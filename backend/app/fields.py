"""Field values: the status line the agent sees, and the rules for recording a value (verbatim quote,
documents before web pages, accepted values final)."""
import logging
import re

from app.db import insert
from app.pdf import norm

log = logging.getLogger(__name__)

MIN_QUOTE = 12


def cite(f: dict) -> str:
    """'D1 p.152 (printed 150)' for documents (PDF page first: what a PDF viewer shows), else the URL."""
    if not f.get("source_key"):
        return f.get("source_url") or ""
    printed = f" (printed {f['printed_page']})" if f.get("printed_page") and f["printed_page"] != f.get("page") else ""
    return f"{f['source_key']} p.{f['page']}{printed}"


def all_fields(conn, subtask_id: int) -> list[dict]:
    """Every field of a sub-task in a stable order: the agent's numbering [1], [2], …"""
    return [dict(r) for r in conn.execute("select * from fields where subtask_id=? order by id", (subtask_id,))]


def _mark(f: dict) -> str:
    if f["status"] == "unavailable":
        return "not public" if f["accepted"] else "not public?"
    return "✓" if f["status"] == "found" else "–"


def status_line(conn, subtask_id: int) -> str:
    rows = all_fields(conn, subtask_id)
    filled = sum(f["status"] == "found" for f in rows)
    return f"filled {filled}/{len(rows)}: " + " ".join(f"[{i}] {_mark(f)}" for i, f in enumerate(rows, 1))


def _why_not(value: str, quote: str, page: str) -> str | None:
    """The reason a quote doesn't support a value, or None when it does (same rules as _supported)."""
    parts = [norm(p) for p in re.split(r"\.\.\.|…", quote or "") if norm(p)]
    if not parts or sum(len(p) for p in parts) < MIN_QUOTE:
        return f"quote too short (at least {MIN_QUOTE} characters of the page's own words)"
    ungroup = lambda s: re.sub(r"(?<=\d),(?=\d)", "", s)  # 1,18,210.52 = 118,210.52 = 118210.52
    if any(ungroup(p) not in ungroup(page) for p in parts):
        return ("quote not found word for word on the pages you read last (for a table, quote the row label and "
                "the value joined by ' ... ')")
    digits = re.sub(r"\D", "", value)
    if digits and digits not in re.sub(r"\D", "", " ".join(parts)):
        return "the quote doesn't contain the value's digits"
    return None


def record(ctx, sub, field_no: int, value: str, unit: str, period: str, quote: str, source: dict) -> str:
    """The record_value tool's rules. Returns 'recorded …' or 'rejected: <reason>'."""
    rows = all_fields(ctx.conn, sub["id"])
    if not isinstance(field_no, int) or not 1 <= field_no <= len(rows):
        return f"rejected: no field {field_no}; fields are 1-{len(rows)}"
    f = rows[field_no - 1]
    if f["accepted"]:
        return f"rejected: already accepted [{field_no}] {f['name']} = {f['value']}"
    if f["status"] == "found" and f["source_kind"] == "doc" and source["kind"] == "web":
        return f"rejected: a document value is already recorded for [{field_no}]; web pages don't replace it"
    if not str(value or "").strip():
        return "rejected: empty value"
    if (why := _why_not(str(value), quote, norm(source.get("text") or ""))):
        return f"rejected: {why}"
    ctx.set_field(f, status="found", accepted=0, value=str(value), unit=str(unit or ""),
                  reported_period=str(period or ""), source_url=source["url"], source_key=source["key"],
                  page=source["page"], quote=norm(quote), source_kind=source["kind"])
    insert(ctx.conn, "evidence", subtask_id=sub["id"], loop=sub.get("loop") or 0, url=source["url"],
           title=source["title"], passage=norm(quote), published=None, fetched_at=ctx.today, page=source["page"])
    where = f"{source['key']} p.{source['page']}" if source["key"] else source["url"]
    log.info("recorded %r = %s %s (%s) from %s", f["name"], value, unit or "", period, where)
    return f"recorded [{field_no}] {f['name']} = {value} {unit or ''}".rstrip() + f" ({where})"
