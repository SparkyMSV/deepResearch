# Task engine redesign: a harness for the local model

Status: draft for approval. Replaces the sub-task loop, PDF reading and check-ins described in
`2026-11-24-deep-research-agent-design.md`. Chat routing, quick `search:`, streaming, the UI shell,
logging, quota/fallback handling and delete are unchanged.

## Intent

This project is a **harness for the local model** (Qwen 3.5 4B on MLX) to explore the web and PDFs and
get results. Everything else (the larger model, the rules, the UI) exists to make that exploration work
well. The larger model sets precise targets and reviews progress. Qwen does the exploring: it chooses
what to search, which results to open, which document and pages to read, and it pulls out the values.

## Why

Live runs on "make a report on Navi bonds investability with a credit view" (conversations 12–16)
failed for harness reasons, not because a small model can't explore:

- **Vague targets.** Sub-tasks were one-sentence topics ("analyze financial performance"), so neither
  "what next?" nor "is this enough?" had a checkable answer.
- **Wide-open choices with little context.** "Pick a PDF or write a query" swung Qwen between ignoring
  good PDFs (run 15) and reading every PDF offered, including Nazara Technologies' and a tutoring
  charity's annual reports (run 16).
- **No cheap filtering.** Wrong-company PDFs reached Qwen although their URL/title/snippet already
  showed the wrong company.
- **Stale periods.** The planner asked for FY2023-24 in November 2026; it is never told today's date.
- **Hidden documents.** Qwen never saw inside a PDF; Gemini read them, so Qwen couldn't steer within one.

## Goals

1. The larger model's plan is a checklist of **fields**: a figure or short fact, for a named entity and
   period, with likely **documents** and starting searches.
2. Qwen explores through a small set of **actions**, one decision per call, each with a compact view of
   exactly what it needs: the field in focus, what's been tried, and a short menu.
3. Qwen reads PDFs itself: it sees the contents, picks pages, reads them page by page, and extracts
   values. indra-ocr turns pages into text; no PDF ever goes to a cloud model.
4. **Guardrails, not decisions**, in code: no repeat searches or pages, a free company-name screen on
   URL/title/snippet, verbatim quotes for every value, budgets.
5. The larger model reviews a fields table at check-ins and can steer with hints, new searches or new
   documents. It writes the report from that table.
6. Stays a general research agent: works for any topic, with or without documents.

## Non-goals

- Changing chat routing, quick `search:` (keeps its current rerank), or the report's markdown style.
- Multi-user, cost dashboards, editing the plan from the UI.
- A general OCR redesign beyond what Qwen-driven reading needs (indra-ocr in `auto` mode stays).

## Anti-goals

- A value whose quote isn't verbatim on the page it cites, or from the wrong entity/period, reaching the
  report as accepted.
- Qwen re-reading the same pages or re-running the same search.
- The larger model quietly doing Qwen's job (reading pages, choosing results).
- A UI that hides which figures are missing.

## Approaches considered

| | Approach | Verdict |
|---|---|---|
| A | **Qwen explores web and PDFs through a harness; larger model plans, reviews, reports** | **Chosen by the user** |
| B | Qwen picks document and section; Gemini reads pages and extracts | Rejected: Qwen steers but doesn't read |
| C | Gemini reads PDFs, Qwen explores only the web (previous draft) | Rejected: sidelines the local model |

Accepted cost of A: research is slower (every page goes through the 4B model on the Mac), in exchange
for far fewer cloud calls and a truly local explorer.

## Design

### 1. Plan (larger model, one call)

Input: the user's objective and **today's date** (`YYYY-MM-DD`). Prompt rules: prefer the latest
complete period available as of today; for figures prefer official documents (annual report, audited
results, filings, rating rationales, government statistics); qualitative questions become short-fact
fields; don't plan fields that need private data.

```json
{
  "documents": [
    {"key": "D1", "name": "Navi Finserv Limited annual report FY2025-26", "issuer": "Navi Finserv Limited",
     "type": "annual report", "period": "FY2025-26", "search": "Navi Finserv annual report 2025-26 pdf",
     "domains": ["navi.com", "bseindia.com", "nseindia.com"]}
  ],
  "subtasks": [
    {"objective": "Asset quality of Navi Finserv",
     "fields": [
       {"name": "Gross NPA %", "entity": "Navi Finserv Limited", "period": "FY2025-26", "kind": "figure",
        "sources": ["D1"], "search": "Navi Finserv gross NPA March 2026",
        "hint": "Usually in the asset quality table of the directors' report or notes to accounts"}
     ]}
  ]
}
```

- `kind`: `figure` (number + unit) or `fact` (one or two sentences).
- `hint` (optional): where such a value usually appears. Shown to Qwen verbatim.
- `domains` (optional): sites likely to host official copies; used only for ranking.
- Caps: `DR_MAX_SUBTASKS` (existing, default 8) sub-tasks; 8 fields per sub-task; 40 per task;
  6 documents. Extras are dropped and logged.
- Bad JSON: one retry with the parse error appended, then fall back to one sub-task with a single
  `fact` field holding the whole objective.

### 2. Document phase (task-level, before sub-tasks start)

For each planned document:

1. Search DDG with its `search`; if no PDF results, once more with `"<issuer> <type> <period> pdf"`.
2. **Free screen** on what DDG returns (URL, title, snippet), before downloading anything: drop a
   candidate if none of the issuer's distinctive words (name minus Limited/Ltd/Pvt/Inc/etc.) appear in
   its URL, title or snippet. Order survivors by: issuer words found, wanted period's years found (file
   names like `2023-24.pdf` or `FY26` count), host in `domains`; a different year lowers the score.
3. For up to 3 survivors, best first: download, indra-ocr page 1 (and 2 if page 1 has under 300 chars),
   and ask **Qwen** to verify: "Expected: issuer, type, period. Here is the first page. Same document?
   `{"match": bool, "about": "≤12 words", "covers": "≤12 words"}`". First match → `verified`; others
   → `rejected`. None → `not_found`.

PDFs met later in web results go through verification only (no hard screen). *(Builder call)* For these
the question is "is this PDF about <entity> and <period>?", not "is it the official document", so rating
rationales, broker notes and filings about the company count. A non-match is dropped. A match becomes a new task-level document (D7, D8…) shown in the UI and
offered to every running sub-task. A task-level dict of URL → in-flight verification future stops two
sub-tasks verifying the same PDF at once (single asyncio loop, so no locks).

Sub-tasks start after the document phase, so all see the same verified list from their first step.

### 3. PDF store (task-level)

- Each verified PDF is downloaded once per task to a temp file (deleted when the task ends).
- Page text is OCR'd on demand with indra-ocr and cached per (document, page) for the task, so a page
  read by one sub-task is free for the next.
- **Contents view**, built once per document: from the first 8 pages, keep lines that look like contents
  entries (text followed by a page number) and cap at ~60 lines; if fewer than 5 such lines, use the
  first 40 lines of pages 1–8 instead. *(Builder call: no OCR of whole documents just to build contents.)*
- **Page offset**: printed page numbers often differ from PDF page indexes. When Qwen opens a contents
  entry, the harness looks for the entry's title text on PDF pages printed±15 and opens the first hit;
  if none, it opens the printed number and says so in the view.
- Short documents (≤10 pages, e.g. rating rationales) skip the contents view: Qwen reads from page 1.

### 4. Qwen's actions

Each step is one Qwen call with a compact view and a tiny JSON reply. A sub-task focuses on one open
field at a time, starting with the first; every reading step still accepts values for *any* open field of
the sub-task. *(Builder call)* **Focus rotates**: after 3 steps on a field with no field filled, focus
moves to the next open field (wrapping around). When every open field has had a no-progress turn since
the last check-in, the harness escalates to a check-in.

**Step view (choose what to do):**

```
Field: Gross NPA % · Navi Finserv Limited · FY2025-26
Hint: Usually in the asset quality table of the directors' report
Also open: Net NPA %, PCR %
Tried: search "Navi Finserv gross NPA March 2026" (3 pages read); D1 pp. 110–112
Options:
 [D1] Navi Finserv annual report FY2025-26 — covers financial statements, asset quality (240 pp) — read pp. 110–112
 [D3] CARE rating rationale Mar 2026 — covers rating, asset quality, capital (6 pp) — not read
 [S] web search
 [X] not public
Reply JSON: {"do":"D3"} or {"do":"S","query":"..."} or {"do":"X","why":"..."}
```

- Only verified documents with unread pages appear. Queries that repeat or overlap ≥80% with one tried
  are refused and Qwen is asked once more; refused twice → the harness runs the field's planner search,
  then `"<entity> <field> <period>"`, then escalates to a check-in.
- `X` marks the field `unavailable` (pending check-in review).

**Inside a document (navigator), up to 6 pages per visit:**

```
D1 Navi Finserv annual report FY2025-26 — looking for: Gross NPA %, Net NPA %, PCR % (FY2025-26)
Contents:
 [1] Directors' report ........ 12
 [2] Management discussion .... 40
 [3] Notes to accounts ........ 160
Read so far: pp. 110–112
Reply: {"open":2} for a contents entry, {"page":N} for a PDF page, or {"leave":true}
```

then for each opened page:

```
Page 112 of D1. Looking for: Gross NPA % · Net NPA % · PCR % (FY2025-26)
<page text, capped at ~6,000 chars>
Reply: {"found":[{"field":"Gross NPA %","value":"2.1","unit":"%","period":"FY2025-26","quote":"exact words"}],
        "next": 113 | "contents" | "leave"}
```

**Web search:** DDG results shown as a numbered list of URL + title + snippet, skipping URLs already
read. *(Builder call)* For web results the company-name screen only **orders** the list (results naming
the entity first); it never hides results, so topic entities like "India" or "RBI repo rate" still work.
The hard screen applies only to document candidates (section 2). Qwen replies `{"open":[1,4]}` (at most 3). Each opened page's
text (trafilatura, capped at ~6,000 chars around the first mention of a field keyword) gets the same
"found" step as a PDF page. Opened PDFs go through verification first.

**Guardrails on every reply:**
- Each `quote` must appear verbatim (whitespace-normalised) in the page text, else that value is dropped
  and logged.
- Field names in replies are matched to open fields case-insensitively; unknown names are dropped.
- Unparseable reply → one retry with "Reply with JSON only"; then the harness takes the default (leave the
  document / run the next planned search).
- A document value replaces a web value for the same field; a web value never replaces a document
  value; nothing replaces an accepted value.
- Code does not compare period strings ("31 March 2026" vs "FY2025-26" would falsely fail); the reported
  period is stored and judged at check-in.

### 5. Check-ins (larger model)

When: every `CHECKIN_EVERY` (5) steps, when the harness escalates, and when every field is filled or
marked not public (confirmation). Input: the sub-task's fields table (name, entity, period, value, unit,
source, page, quote, source kind, unavailable reasons), documents read with pages, and searches tried.

```json
{"fields": [{"name": "Gross NPA %", "action": "accept|reject|unavailable|retry",
             "hint": "Try the CARE rationale, section 'Asset quality'", "search": "optional new query"}],
 "add_fields": [{"name": "...", "entity": "...", "period": "...", "kind": "figure", "search": "...", "hint": "..."}],
 "find_document": {"name": "...", "issuer": "...", "type": "...", "period": "...", "search": "..."}}
```

- `reject` clears the value and reopens the field; `retry` reopens with an optional new `hint`/`search`
  shown to Qwen next time; `unavailable` closes it with the reason; `accept` sets `accepted`.
- `add_fields`: up to 3 per check-in within caps. `find_document`: runs section 2 for one more document.
- A field counts toward done only when `accepted` or `unavailable`. Sub-task ends `done` when all do;
  `partial` when limits hit, and found-but-unreviewed values go to the report marked "unreviewed".
- The confirmation check-in always runs and its actions always apply. Past the restart cap, a reject
  reopens the field but the sub-task ends `partial`.

### 6. Limits and budget

- `DR_ROUNDS_PER_LOOP` (default 20): max steps per loop (a search, or a document visit). *(Builder
  call, same as today)* A check-in that reopens work (`reject`/`retry`/`add_fields`/`find_document`)
  starts a new loop with a fresh step count, so a sub-task has at most (1 + `DR_MAX_RESTARTS`) loops,
  about 60 steps worst case. Hitting the step limit triggers a check-in.
- `DR_MAX_RESTARTS` (default 2): check-ins that reopen work. Every reopening check-in counts. When hit,
  that check-in's accept/unavailable actions apply, its reopening actions are dropped (logged), the
  sub-task ends `partial`. The existing `round`/`loop`/`restarts` columns keep their meaning.
- New `DR_MAX_PAGES` (default 150): pages Qwen may read per task across PDFs and web pages. When
  exhausted, sub-tasks go straight to a final check-in.
- Cloud calls per research: 1 plan + ~2–4 check-ins per sub-task + 1 report ≈ 20–35. The `pdf` model
  role is no longer used for tasks.
- Local cost: roughly 3–15 s per Qwen call; a research with 7 sub-tasks may take 30–60 minutes. Chat
  still jumps the queue (existing priority).
- Names of existing settings stay so `.env` files keep working; README updated.

### 7. Report (larger model)

Input: objective, today's date, the fields table by sub-task (values, units, periods, sources, pages,
review status), verified documents list. Present key figures in tables; "Gaps" lists open and
unavailable fields with reasons; documents outrank web pages when they disagree; existing citation and
no-invention rules stay. Input cap 60,000 chars.

### 8. Data model

New tables (old tables stay; old tasks render as before):

```sql
documents(id, task_id, key, name, issuer, type, period, url, about, covers, pages, status)
  -- status: planned | verified | rejected | not_found
fields(id, task_id, subtask_id, name, entity, period, kind, sources, search, hint,
       status, accepted, value, unit, source_url, source_key, page, quote, source_kind, note)
  -- status: open | found | unavailable
```

`rounds.query` holds the step label ("D1 pp. 110–114" or the search). *(Builder call)* `evidence` stays
as provenance and as chat's follow-up context: every value Qwen finds (accepted or not) is stored there
with its quote, source and page; pages where nothing was found are not stored. Chat follow-ups also get
the task's fields table alongside the report.

### 9. UI

Above the sub-tasks, a **Documents** line per document: name and status (✓ verified, opens the PDF ·
✗ not found · … searching · rejected candidates in the expanded view). Each sub-task row shows
"4/6 fields"; expanded:

```
Gross NPA %   FY2025-26   2.1%   D1 p.112   ✓ accepted
Net NPA %     FY2025-26   0.9%   D1 p.112   unreviewed
PCR %         FY2025-26   —      not public: not disclosed by the company
Steps: 1. D1 pp. 110–112 · 2. search "Navi Finserv net NPA March 2026" · …
```

New SSE events `document` and `field`, same pattern as `subtask`. Existing tokens; orange only for
running states.

## What stays as is

Chat routing and quick search; SSE streaming and the busy indicator; URL skip-lists; the 80%
near-duplicate rule; Gemini → Groq fallback with user notices and pause (now used by plan, check-ins
and report); logs with `[c t s]` tags (new lines for each Qwen action and its reply, document verdicts,
field fills, check-in actions); delete-conversation; restart marking work interrupted.

## Left to the builder

- Exact prompt wording (short; one decision per call).
- Contents-line detection heuristics and the keyword window for long web pages, within the rules above.
- Whether verification and contents building run concurrently across documents (Qwen is serial anyway).
- Table/CSS details within the existing tokens; tests following the `Env`-style fakes in
  `backend/tests/test_task.py`.
