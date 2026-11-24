# Specialist agents: a main agent delegating to a PDF agent and a web agent

Status: draft for approval. Changes the sub-task loop of `2026-11-25-bonsai-agent-harness-design.md`
(§3 "The agent loop"); everything else there stays (MTPLX servers, plan, documents, self-review, final
review, report, limits, UI).

## Why

Live runs (Bharti Telecom, Nexus Select Trust) showed one Bonsai loop doing everything at once: picking
pages with the wrong tools, wandering through notes and auditor pages, mixing web results and PDF pages
in one context, and losing track. Each job needs different tools, a different prompt and different
memory. The user's direction:

- A **main agent** decides, per field (or group of fields), *once*: look in a document, or search the web.
- A **PDF agent** and a **web agent** do the finding with their own tools and prompts, then return.
- PDF handling (contents page, page offset, page extraction, OCR) is **prepared in the background and
  hidden behind tools**; the model sees only what each tool is for.
- The web agent sees its **objective**, the **search history** with every link each query retrieved,
  and a short **remark per query on whether it helped**.

Two failures from the Nexus run are fixed here too:
- Documents were described from page 1 only, which for exchange filings is a cover letter ("Annual Report
  *submission letter*"), so the model skipped the actual annual report.
- Cloud reviewers marked fields "not public" from their own (wrong) beliefs ("no entity by this name",
  "an SPV NPA pool"), not from anything the research saw.

## Design

### 1. Document preparation (background, before any agent sees a document)

Runs once per verified document (planned or found later), as part of verification:

1. **Description:** text of the first pages until ~3,000 characters (skipping near-empty pages); the
   verification prompt says the first page may be a cover letter or filing intimation and asks for what the
   *enclosed* document is. `about` and `covers` describe the document itself.
2. **Contents:** contents entries (title, printed page) from the first 12 pages' text (text layer first,
   indra-ocr when a page has no text layer).
3. **Offset:** for up to 5 contents entries, find the PDF page whose text starts with (or contains in its
   first lines) the entry's title, searching printed page −5…+25; offset = most common
   (PDF page − printed page) among matches. No contents or no matches → look for printed page numbers in
   page footers/headers of ~10 sampled pages; still nothing → offset 0, flagged "unverified".
4. **Page index (for retrieval):** every page's text-layer text is split into passages of ~600 characters
   (overlapping by ~100), each tagged with its PDF page, printed page and section (the contents entry it
   falls under), and indexed for keyword-relevance ranking (BM25, pure Python, no new dependency). Pages
   with no text layer are left out of the index (noted on the document as "N pages scanned, not indexed")
   rather than OCR'd up front; `read_pages` still OCRs them on demand.
5. **Stored on the document row:** `pages`, `offset`, `offset_ok`, `contents` (JSON), `about`, `covers`;
   the page index is kept with the task's PDF store (in memory, rebuilt if needed).

All of this is code (pypdfium2 text layer, indra-ocr when needed); no model involved except the existing
description call.

### 2. Main agent (one per sub-task)

Context (fixed opening, cached): rules, date note, the sub-task's fields with status and hints, the
documents (key, about, covers, pages, whether a contents page exists), and the specialist reports so far
(short, appended).

Tools:

| Tool | Does |
|---|---|
| `explore_document(doc, fields, goal)` | hands the listed fields to the PDF agent for that document; returns its report |
| `search_web(fields, goal)` | hands the listed fields to the web agent; returns its report |
| `mark_not_public(field, reason)` | only after at least one specialist looked for that field and reported it missing |
| `done(note)` | sub-task finished |

Budget: `DR_MAX_DELEGATIONS` (default 6) specialist calls per loop. Self-review (Bonsai) and restarts as
before.

### 3. PDF agent (one call = one document, a few fields)

Context: its goal from the main agent, the fields it must find (entity, period, hint), the document's
description and its contents with **printed** page numbers. Fresh context each call.

Working pattern taught in the prompt: **locate, then read.** Use `locate` (or the contents) to find anchor
pages for a field, read those whole pages with `read_pages`, record values with quotes from them, and only
then move on; don't page through the document blindly. `record_value` only accepts quotes from pages read
in full (not from `locate` snippets).

Tools (the model sees only what they do; offset, extraction and OCR are hidden):

| Tool | Does |
|---|---|
| `locate(query)` | **anchors first**: the top 6 places in the document matching a question or terms (e.g. "total borrowings March 2026", "interest coverage"), each as printed page, section and the 1–2 line passage that matched. Retrieval only points; it never stands in for reading |
| `read_pages(from, to)` | the **whole** text of printed pages `from`–`to` (max 2 pages per call), each headed `[page N]`, so values are read and quoted in their full context, never from a retrieved chunk alone |
| `record_value(field, value, unit, period, quote, page)` | records from a page it read (verbatim quote rules unchanged; `page` must be one it read) |
| `report(found, missing, notes)` | ends the call; summary for the main agent |

`read_pages` maps printed → PDF pages with the stored offset, extracts those pages, runs indra-ocr (cached
per page for the research) and returns text capped at 6,000 characters per page. Out-of-range or
already-read pages return a short message. `locate` falls back to a plain text-layer term search when the index is empty for a page range.
Budget: `DR_PDF_STEPS` (default 12) tool calls,
`DR_MAX_PAGES` shared across the research as today.

The report returned to the main agent: values recorded (field, value, printed page), fields still missing
with the agent's note on where they might be (e.g. "not in standalone statements; try consolidated notes"),
pages read.

### 4. Web agent (one call = a search goal, a few fields)

Context: the objective (goal + fields with entity/period/hint) and the research's **search history**
(shared across all web-agent calls of the research), each entry:

```
query: "Nexus Select Trust total debt March 2026"
links: [1] title — url  [2] … (every link the query returned)
opened: 2, 5
remark: helpful — recorded Total Debt (₹6,100 crore) from [2]
```

The remark is written by the harness from what happened (values recorded from which link, or
"not helpful — opened 2, nothing recorded"), plus the agent's own one-line note when it gives one.
History shown is capped to the last 10 queries (older ones summarised as one line each).

Tools:

| Tool | Does |
|---|---|
| `search(query, note)` | runs the search (several engines, merged); refuses repeats; adds a history entry |
| `open(n)` | opens link n of the latest search; web page text (window around field keywords); a PDF is verified and prepared (§1) and returned to the main agent as a new document instead of being read here |
| `record_value(field, value, unit, period, quote)` | from a page it opened |
| `report(found, missing, notes)` | ends the call |

Budget: `DR_WEB_STEPS` (default 12) tool calls.

### 5. Reviews can't invent "not public"

- Self-review (Bonsai) and the cloud final review: for found values, accept / reject / retry; for fields
  marked not public by the main agent, accept / retry. They can no longer *set* a field to not public.
- The final-review prompt says: judge only from the values, quotes and notes shown; don't use your own
  beliefs about whether an entity exists or what it is.

### 6. What stays / goes

Stays: plan, document finding and screening, PDF store and cache, verbatim-quote rules (incl. "label ...
value" for tables, HTML-safe normalisation), value precedence and review levels, self-review, consistency
checks, final review, report, research queue, MTPLX client and memory pauses, UI (rounds now show
delegations and specialist steps, e.g. `→ PDF D1: Total Debt`, `D1 pp. 228–229`, `search "…"`).

Future option: embedding-based retrieval for `locate` (MTPLX can serve an embedding model) if keyword
ranking proves too literal; it costs extra memory on a 24 GB Mac, so it isn't in this build.

Goes: the single all-tools loop (`agent.run_loop` with the combined `tools.TOOLS`), the agent-facing
`pdf_contents`/`pdf_find`/`pdf_page` tools (replaced by the PDF agent's tools), `mark_not_public` in the
reviewers.

## Builder calls (after the builder check)

1. **Page numbers.** The offset is a small table, not one number: one offset per contents entry (from §1),
   applied to printed pages from that entry up to the next one, so sections that restart numbering
   (standalone vs consolidated, BRSR) map correctly. When a printed page number is ambiguous (two sections
   both have a "page 12"), `read_pages` answers with the choices and the agent repeats the call with
   `section`. Front matter before the first contents entry is out of scope for `read_pages`. Storage:
   `fields.page` keeps the **PDF page** (what a PDF viewer shows; existing reports and UI stay right) and a
   new `fields.printed_page` holds the printed number; citations read "D1 p.152 (printed 150)".
2. **PDF agent context.** `read_pages` returns at most 2 pages per call, capped at 4,500 characters each.
   Past 12K tokens the conversation is compacted: older page texts are replaced by
   "[page N read; text removed to save space]". `record_value` checks quotes against the harness's cache
   of pages read in this call, not the conversation, so recording from an earlier page still works.
3. **Web agent meets a PDF.** The call continues; the PDF is prepared in the background, and the report
   lists it ("new document D7: …"). The main agent decides whether to explore it (one of its delegations).
4. **Not-public guard.** The main agent may mark a field not public only if, in this sub-task (across
   restarts), the field was reported missing by a web agent **and**, when any verified document exists, by
   at least one PDF agent.
5. **Never-found fields.** The final review doesn't touch fields still open; the report's Gaps section
   says "not found (looked in: D1 pp. …, N web searches)". Rejected values say "rejected: <reason>".
6. **Search history** is research-wide (all web-agent calls of the research) and kept in memory (tasks
   don't survive a backend restart anyway). Repeats (≥80% shared words) are refused research-wide.
7. **Settings.** `DR_MAX_STEPS` is retired in favour of `DR_MAX_DELEGATIONS` (6), `DR_PDF_STEPS` (12) and
   `DR_WEB_STEPS` (12); worst case per loop ≈ 6 × 12 specialist steps.

## Left to the builder

- Exact prompts (short; examples with real values from the current fields).
- Title-matching tolerance for the offset (normalised text, first lines of the page).
- How specialist reports are worded for the main agent (short, structured).
- Test layout using the existing fakes.
