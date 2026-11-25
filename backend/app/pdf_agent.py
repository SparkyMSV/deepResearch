"""PDF agent: one document, a few fields. It locates anchors, reads whole pages and records values with
quotes. Page offsets, page extraction and OCR are hidden inside its tools."""
import logging
from collections import Counter

from app import agentkit, config, docprep, tables
from app.ctx import date_note, is_fiscal
from app.docs import page_ranges
from app.fields import _why_not, all_fields, record
from app.logs import clip
from app.pdf import norm

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
    lines = "\n".join(f"[{n}] {f['name']} — {f['entity'] or 'any'}, {f['period']}"
                      + (f" — hint: {f['hint']}" if f["hint"] else "") for n, f in fields)
    return (
        f"You are a researcher working through one document. {date_note(ctx.today, is_fiscal(f['period'] for _, f in fields))}\nGoal: {goal}\n"
        f"Document {doc['key']}: {doc['about'] or doc['name']} ({prep.pages} pages)"
        + (f"; {doc['notes']}" if doc.get("notes") else "") + f"\nContents (printed page numbers):\n{contents}\n"
        f"Fields to find:\n{lines}\n"
        "How to work: first think what each field means, what this document would call it (its own wording or "
        "abbreviation) and, from the contents above, which sections should hold it. A locate hit inside those "
        "sections is likely right; one elsewhere (another part, a glossary, a different entity) is likely a false "
        "positive. Narrow down: list_notes for a section with numbered notes, or locate with the words the document itself would "
        "use; read the best pages in full with read_pages; record_value with the printed page and, for a table, the "
        "row and column (else a quote). After a miss try the document's own wording or another section, not the "
        "same words again. Only values for the field's entity and period. Don't page through the document blindly. "
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
    read: dict[int, dict] = {}  # PDF page -> {"printed","text","label"} read in this call (quotes checked against these)
    shown: set[int] = set()  # pages whose text is still in the conversation (compaction removes it)
    cap = PAGE_CHARS * (2 if prep.spread else 1)  # a spread page holds two printed pages
    recorded: list[str] = []
    tried: list[str] = []  # rejected recordings, for the main agent: the value may be right and worth a retry

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
            asked = lo if hi is None or hi < lo else hi
            hi = min(asked, lo + PAGES_PER_CALL - 1)
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
                if pdf in read and pdf in shown:
                    out.append(f"already read page {p} in this call; its text is above")
                    continue
                if pdf in read:  # its text was removed to save space: show it again (no new page read)
                    shown.add(pdf)
                    now.add(pdf)
                    out.append(f"[{read[pdf]['label']}] (shown again)\n{read[pdf]['text'][:cap]}")
                    continue
                if not ctx.pages_left():
                    out.append("Page budget used up; record what you have and call report.")
                    break
                full = await ctx.store.text(doc["url"], pdf)  # kept whole for table lookups and quote checks
                text = full[:cap]
                pp = docprep.to_printed(prep, pdf)
                label = f"pages {pp}-{pp + 1}" if prep.spread and pp and pp % 2 == 0 else f"page {p}"
                read[pdf] = {"printed": p, "text": full, "label": label}
                now.add(pdf)
                shown.add(pdf)
                sub["read"].setdefault(doc["key"], set()).add(pdf)
                ctx.pages_used += 1
                pages += 1
                out.append(f"[{label}]\n{text.strip() or '(no text on this page)'}")
            if asked > hi:
                out.append(f"(at most {PAGES_PER_CALL} pages per call; next: read_pages(from={hi + 1}, to={asked}))")
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
            value, period, quote = str(a.get("value") or ""), str(a.get("period") or ""), str(a.get("quote") or "")
            # a row label (given as row, column or quote) plus the value: check that row holds it, quote it. The
            # cited page first, then the other pages read in this call (small models often cite the wrong one).
            labels = [str(a[k]) for k in ("row", "column", "quote") if a.get(k)]
            hit, cited = None, pdf
            for k in ([pdf] if pdf in read else []) + [k for k in reversed(read) if k != pdf]:
                hit = next((c for lb in labels if value and not (c := tables.row_value(read[k]["text"], lb, value)).error),
                           None)
                if hit or (value and quote and not _why_not(value, quote, norm(read[k]["text"]))):
                    if k != cited:  # found on another page read in this call: cite that one
                        pdf, p = k, read[k]["printed"]
                    break
            if pdf not in read:
                return f"rejected: read the page first with read_pages (page {p} hasn't been read in full)"
            src = {"url": doc["url"], "title": doc["name"], "key": doc["key"], "page": pdf, "kind": "doc",
                   "text": read[pdf]["text"]}
            if hit:
                value, period, quote = hit.value, period or hit.column, hit.quote
            elif a.get("row"):  # the harness reads the number, so it can't be mistyped
                c = tables.cell(read[pdf]["text"], str(a["row"]), str(a.get("column") or ""))
                if c.error and not quote:
                    why = tables.row_value(read[pdf]["text"], str(a["row"]), value).error if value else c.error
                    on_round(f"rejected: {why}"[:80], 0)
                    tried.append(f"[{n}] {value or '?'} (p.{p}): rejected: {why}")
                    return f"rejected: {why}"
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
                tried.append(f"[{n}] {value or '?'} (p.{p}): {out}")
            return out
        if name == "report":
            return "ok"
        return f"error: unknown tool {name!r}"

    def compact(msgs: list[dict]) -> list[dict]:
        """Drop the text of all but the last read_pages result; the pages stay recordable from the cache."""
        tool_ids = {tc["id"]: tc["function"]["name"] for m in msgs for tc in m.get("tool_calls") or []}
        reads = [i for i, m in enumerate(msgs) if m.get("role") == "tool" and tool_ids.get(m["tool_call_id"]) == "read_pages"]
        for i in reads[:-1]:
            gone = [k for k, v in read.items() if f"[{v['label']}]" in msgs[i]["content"]]
            shown.difference_update(gone)
            labels = [read[k]["label"] for k in gone]
            msgs[i] = {**msgs[i], "content": "; ".join(REMOVED.format(lb) for lb in labels) or "[text removed]"}
        return msgs

    earlier = sub.setdefault("pdf_calls", {}).setdefault(doc["key"], [])
    system = _prompt(ctx, doc, prep, fields, goal) + (
        "\nEarlier on this document (don't repeat what didn't work):\n" + "\n".join(earlier[-3:]) if earlier else "")
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "Begin."}]
    why, args = await agentkit.loop(ctx, sub, msgs, PDF_TOOLS, handle, config.PDF_STEPS, compact, ("report",))
    now = all_fields(ctx.conn, sub["id"])
    missing = [(n, f) for n, f in fields if now[n - 1]["status"] != "found"]
    sub.setdefault("missing_pdf", set()).update(f["id"] for _, f in missing)
    for n, f in fields:
        sub.setdefault("looked", {}).setdefault(f["id"], set()).add(
            f"{doc['key']} pp. {', '.join(str(v['printed']) for v in read.values())}" if read
            else f"{doc['key']} (nothing read)")
    notes = str((args or {}).get("notes") or "").strip()
    earlier.append(f"- read pp. {', '.join(str(v['printed']) for v in read.values()) or 'none'}; recorded "
                   f"{', '.join(recorded) or 'nothing'}" + (f"; notes: {clip(notes, 200)}" if notes else ""))
    ended = {"finished": "", "steps": " (stopped: step budget used)", "stalled": " (stopped: no tool use)",
             "error": " (stopped: model error)"}[why]
    report = (f"PDF agent on {doc['key']}{ended}: recorded: {', '.join(recorded) or 'nothing'}; missing: "
              + (", ".join(f"[{n}] {f['name']}" for n, f in missing) or "none")
              + (f"; notes: {notes}" if notes else "")
              + f"; pages read: {', '.join(str(v['printed']) for v in read.values()) or 'none'}"
              + (f"; tried: {'; '.join(clip(t, 140) for t in tried[-3:])}" if tried else ""))
    log.info("%s", report)
    return report
