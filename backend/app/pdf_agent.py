"""PDF agent: one document, a few fields. It locates anchors, reads whole pages and records values with
quotes. Page offsets, page extraction and OCR are hidden inside its tools."""
import logging
from collections import Counter

from app import agentkit, config, docprep, tables
from app.ctx import date_note
from app.docs import page_ranges
from app.fields import all_fields, record
from app.logs import clip

log = logging.getLogger(__name__)

PAGES_PER_CALL, PAGE_CHARS, ANCHORS = 2, 4500, 6
REMOVED = "[{} read; text removed to save space]"


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


_S, _I = {"type": "string"}, {"type": "integer"}
PDF_TOOLS = [
    _fn("locate", "Find where in the document something is discussed. Returns the best matching places as printed "
        "page, section and a short passage. Use it to choose pages, then read them.", {"query": _S}, ["query"]),
    _fn("list_notes", "List the numbered notes of a section (e.g. the financial statements) with their printed "
        "pages: '17 BORROWINGS — p.470'. Use it to go straight to the note that holds a figure.",
        {"section": _S}, ["section"]),
    _fn("read_pages", "Read whole printed pages from-to (at most 2). Use the page numbers printed in the document.",
        {"from": _I, "to": _I, "section": _S}, ["from", "to"]),
    _fn("record_value", "Record a field's value from a page you read in full. For a figure in a table, give row "
        "(the row's label) and column (its heading, e.g. the date): the number is read from the table. Otherwise "
        "give value and a quote copied from the page.",
        {"field": _I, "value": _S, "unit": _S, "period": _S, "quote": _S, "row": _S, "column": _S, "page": _I,
         "section": _S}, ["field", "page"]),
    _fn("report", "Finish: say what you found, which fields are missing, and where they might be.",
        {"found": _S, "missing": _S, "notes": _S}, ["notes"]),
]


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    return int(v) if isinstance(v, str) and v.strip().lstrip("-").isdigit() else None


def _prompt(ctx, doc, prep, fields: list[tuple[int, dict]], goal: str) -> str:
    counts = Counter(n["section"] for n in prep.notes)
    noted = {sec for sec, k in counts.items() if k >= docprep.NOTED}  # a few stray capitals don't count
    contents = "\n".join(f"  {e['title']} .... {e['printed']}" + (" (numbered notes: list_notes)"
                                                                if e["title"] in noted else "")
                         for e in prep.contents) or \
        "  (no contents page: use locate)"
    eg_field = fields[0][1]["name"].lower() if fields else "revenue"
    lines = "\n".join(f"[{n}] {f['name']} — {f['entity'] or 'any'}, {f['period']}"
                      + (f" — hint: {f['hint']}" if f["hint"] else "") for n, f in fields)
    return (
        f"You find figures in one document. {date_note(ctx.today)}\nGoal: {goal}\n"
        f"Document {doc['key']}: {doc['about'] or doc['name']} ({prep.pages} pages)"
        + (f"; {doc['notes']}" if doc.get("notes") else "") + f"\nContents (printed page numbers):\n{contents}\n"
        f"Fields to find:\n{lines}\n"
        "How to work: find the page, then read it. For a figure in financial statements, list_notes of that "
        "section and pick the note (e.g. borrowings, capital management); otherwise call locate with the field's words "
        f'(e.g. locate(query="{eg_field} {fields[0][1]["period"] if fields else ""}")), read the best pages in full '
        "with read_pages, then record_value with its printed page number and, for a table, the row and column (else a quote). "
        "Only values for the field's entity and period. Don't page through the document blindly. "
        "When done, or when the values aren't in this document, call report.")


async def explore(ctx, sub, doc_key: str, field_nos: list[int], goal: str, on_round) -> str:
    """Run the PDF agent on one document for the given field numbers; returns its report for the main agent."""
    doc = ctx.doc(str(doc_key).upper())
    if not doc or doc["status"] != "verified":
        return f"error: no verified document {doc_key}"
    prep = await docprep.prepare(ctx.store, doc["url"])
    rows = all_fields(ctx.conn, sub["id"])
    fields = [(n, rows[n - 1]) for n in field_nos if isinstance(n, int) and 1 <= n <= len(rows)]
    if not fields:
        return "error: no valid fields to look for"
    read: dict[int, dict] = {}  # PDF page -> {"printed","text"} read in this call (quotes are checked against these)
    recorded: list[str] = []

    async def handle(name: str, a: dict) -> str:
        if name == "locate":
            q = str(a.get("query") or "").strip()
            hits = [h for h in (prep.index.locate(q, ANCHORS + 4) if prep.index else [])
                    if h["printed"] is not None or not prep.contents][:ANCHORS]  # front matter can't be read
            on_round(f"{doc['key']} locate {clip(q, 40)}", 0)
            if not hits:
                return ("no matches" + (" (scanned pages aren't searchable; use the contents and read_pages)"
                                        if prep.scanned else "; try other words or the contents"))
            return "\n".join(f"p.{h['printed'] if h['printed'] is not None else 'PDF ' + str(h['pdf'])}"
                             f" ({h['section'] or 'no section'}): …{h['passage']}…" for h in hits)
        if name == "list_notes":
            sec = str(a.get("section") or "")
            ns = docprep.notes(prep, sec)
            on_round(f"{doc['key']} notes {clip(sec, 30)}", 0)
            if not ns:
                have = sorted({n["section"] for n in prep.notes if n["section"]})
                return (f"no notes found for {sec!r}; sections with notes: {'; '.join(have)}" if have
                        else "no numbered notes found in this document; use locate")
            return "\n".join(f"{no} {title} — p.{pr}" for no, title, pr in ns)
        if name == "read_pages":
            lo, hi = _int(a.get("from")), _int(a.get("to"))
            if lo is None:
                return "error: read_pages needs printed page numbers"
            hi = lo if hi is None or hi < lo else min(hi, lo + PAGES_PER_CALL - 1)
            out, pages, now = [], 0, set()
            for p in range(lo, hi + 1):
                try:
                    pdf = docprep.to_pdf(prep, p, a.get("section"))
                except ValueError as e:
                    out.append(f"page {p}: {e}")
                    continue
                if isinstance(pdf, list):
                    out.append(f"page {p} exists in several sections: {'; '.join(pdf)}. Call again with section.")
                    continue
                if pdf in now:  # spreads: both printed pages are on this PDF page
                    continue
                if pdf in read:
                    out.append(f"already read page {p} in this call; its text is above")
                    continue
                if not ctx.pages_left():
                    out.append("Page budget used up; record what you have and call report.")
                    break
                full = await ctx.store.text(doc["url"], pdf)  # kept whole for table lookups and quote checks
                text = full[:PAGE_CHARS * (2 if prep.spread else 1)]
                pp = docprep.to_printed(prep, pdf)
                label = f"pages {pp}-{pp + 1}" if prep.spread and pp and pp % 2 == 0 else f"page {p}"
                read[pdf] = {"printed": p, "text": full, "label": label}
                now.add(pdf)
                sub["read"].setdefault(doc["key"], set()).add(pdf)
                ctx.pages_used += 1
                pages += 1
                out.append(f"[{label}]\n{text.strip() or '(no text on this page)'}")
            if pages:
                on_round(f"{doc['key']} {page_ranges([v['printed'] for v in read.values() if lo <= v['printed'] <= hi])}",
                         pages)
            return "\n\n".join(out)
        if name == "record_value":
            n, p = _int(a.get("field")), _int(a.get("page"))
            try:
                pdf = docprep.to_pdf(prep, p, a.get("section")) if p is not None else None
            except ValueError:
                pdf = None
            if isinstance(pdf, list):  # ambiguous without a section: the page read in this call, if only one
                hits = [k for k, v in read.items() if v["printed"] == p]
                pdf = hits[0] if len(hits) == 1 else None
            if pdf not in read:
                return f"rejected: read the page first with read_pages (page {p} hasn't been read in full)"
            src = {"url": doc["url"], "title": doc["name"], "key": doc["key"], "page": pdf, "kind": "doc",
                   "text": read[pdf]["text"]}
            value, period, quote = str(a.get("value") or ""), str(a.get("period") or ""), str(a.get("quote") or "")
            if a.get("row"):  # the harness reads the number, so it can't be mistyped
                c = tables.cell(read[pdf]["text"], str(a["row"]), str(a.get("column") or ""))
                if c.error and not quote:
                    on_round(f"rejected: {c.error}"[:80], 0)
                    return f"rejected: {c.error}"
                if not c.error:
                    value, period, quote = c.value, period or c.column, c.quote
            out = record(ctx, sub, n, value, str(a.get("unit") or ""), period, quote, src)
            if out.startswith("recorded"):
                f = all_fields(ctx.conn, sub["id"])[n - 1]
                ctx.set_field(f, printed_page=p)
                (sub.get("recheck") or set()).discard(f["id"])
                recorded.append(f"[{n}] {f['name']} = {f['value']} {f['unit'] or ''}".rstrip() + f" (p.{p})")
                on_round(out.split(" (", 1)[0].replace(f"[{n}] ", ""), 0)
            else:
                on_round(out[:80], 0)
            return out
        if name == "report":
            return "ok"
        return f"error: unknown tool {name!r}"

    def compact(msgs: list[dict]) -> list[dict]:
        """Drop the text of all but the last read_pages result; the pages stay recordable from the cache."""
        tool_ids = {tc["id"]: tc["function"]["name"] for m in msgs for tc in m.get("tool_calls") or []}
        reads = [i for i, m in enumerate(msgs) if m.get("role") == "tool" and tool_ids.get(m["tool_call_id"]) == "read_pages"]
        for i in reads[:-1]:
            labels = [v["label"] for v in read.values() if f"[{v['label']}]" in msgs[i]["content"]]
            msgs[i] = {**msgs[i], "content": "; ".join(REMOVED.format(lb) for lb in labels) or "[text removed]"}
        return msgs

    msgs = [{"role": "system", "content": _prompt(ctx, doc, prep, fields, goal)}, {"role": "user", "content": "Begin."}]
    why, args = await agentkit.loop(ctx, sub, msgs, PDF_TOOLS, handle, config.PDF_STEPS, compact, ("report",))
    now = all_fields(ctx.conn, sub["id"])
    missing = [(n, f) for n, f in fields if now[n - 1]["status"] != "found"]
    sub.setdefault("missing_pdf", set()).update(f["id"] for _, f in missing)
    for n, f in fields:
        sub.setdefault("looked", {}).setdefault(f["id"], set()).add(
            f"{doc['key']} pp. {', '.join(str(v['printed']) for v in read.values())}" if read
            else f"{doc['key']} (nothing read)")
    notes = str((args or {}).get("notes") or "").strip()
    ended = {"finished": "", "steps": " (stopped: step budget used)", "stalled": " (stopped: no tool use)",
             "error": " (stopped: model error)"}[why]
    report = (f"PDF agent on {doc['key']}{ended}: recorded: {', '.join(recorded) or 'nothing'}; missing: "
              + (", ".join(f"[{n}] {f['name']}" for n, f in missing) or "none")
              + (f"; notes: {notes}" if notes else "")
              + f"; pages read: {', '.join(str(v['printed']) for v in read.values()) or 'none'}")
    log.info("%s", report)
    return report
