# Task engine redesign: field checklists, verified documents, rule-driven rounds

Status: draft for approval. Replaces the sub-task loop described in
`2026-11-24-deep-research-agent-design.md` (sections on planning, the sub-task loop and check-ins).
Chat routing, quick search, streaming, the UI shell, logging and quota handling are unchanged.

## Why

Live runs on "make a report on Navi bonds investability with a credit view" (conversations 12–16) showed
the local Qwen 3.5 4B making nearly every judgement in a research task, and getting them wrong in both
directions:

- Run 15: sub-tasks rewrote near-identical searches ("… yield 2024 2025", "… 2024 2025 latest") while a
  554-page NCD prospectus and CARE/ICRA rating rationales holding the answers sat unread on the shared list.
- Run 16, after a prompt change: Qwen read every PDF offered, including Nazara Technologies' and a
  tutoring charity's annual reports for "Navi's debt profile", and re-read the same two Navi PDFs from
  sub-tasks they didn't fit (e.g. "RBI regulations").
- The planner asked for FY2023-24 data in November 2026 because it is never told today's date.
- Sub-tasks have vague objectives ("analyze financial performance"), so "is this sufficient?" is itself a
  judgement call, made by the 4B model between check-ins.

The fix the user chose: the larger model plans in detail and makes the judgement calls; Qwen does only
narrow, checkable work.

## Goals

1. The plan is a checklist of specific **fields** to fill (a figure or a short fact, for a named entity and
   period), plus the **documents** most likely to hold them.
2. Documents are found **once per research**, and the larger model confirms each is the right issuer,
   type and period before any sub-task reads it. Wrong-company documents never reach the shared list.
3. Each round's next step comes from rules over the open fields, not from Qwen's open-ended choice.
4. A sub-task is done when every field is filled or confirmed not publicly available.
5. Check-ins and the report work from a table of fields (value, unit, period, source, page, quote).
6. Stays a general research agent: works for any topic, with or without documents.

## Non-goals

- The OCR/retrieval redesign noted earlier (`backend/app/pdf.py` internals stay; only its prompts and
  return shape change here).
- Multi-user, cost dashboards, editing the plan from the UI.
- Changing chat routing, quick `search:`, or the report's markdown style.

## Anti-goals (failure even if it "works")

- A value in a field that is not verbatim-supported by its stored quote, or comes from the wrong entity
  or period.
- Reading a document the larger model rejected.
- Blowing the daily cloud quotas: one research should stay around 60–120 cloud calls (see Budget).
- A UI that hides which figures are missing.

## Approaches considered

| | Approach | Trade-off | Verdict |
|---|---|---|---|
| A | Keep the current loop, tune Qwen prompts | Cheapest; runs 15 and 16 show prompt tweaks swing Qwen between ignoring and over-reading PDFs | Rejected |
| B | **Larger model plans fields + documents and verifies documents; rules drive rounds; Qwen ranks and extracts; check-ins over a fields table** | Moderate extra cloud calls; deterministic, checkable progress | **Chosen** |
| C | Larger model decides every round | Best judgement; ~300+ cloud calls per research, hits Groq per-minute limits, slow | Rejected |

## Design

### 1. Plan (larger model, one call)

Input: the user's objective and **today's date** (`YYYY-MM-DD`). Prompt rules: prefer the latest
complete period available as of today; for figures prefer official documents (annual report, audited
results, regulatory filings, rating rationales, government statistics); qualitative questions become
short-fact fields; never invent fields that need private data.

Output JSON:

```json
{
  "documents": [
    {"key": "D1", "name": "Navi Finserv Limited annual report FY2025-26", "issuer": "Navi Finserv Limited",
     "type": "annual report", "period": "FY2025-26", "search": "Navi Finserv annual report 2025-26 pdf"}
  ],
  "subtasks": [
    {"objective": "Asset quality of Navi Finserv",
     "fields": [
       {"name": "Gross NPA %", "entity": "Navi Finserv Limited", "period": "FY2025-26", "kind": "figure",
        "sources": ["D1", "D3"], "search": "Navi Finserv gross NPA March 2026"}
     ]}
  ]
}
```

- `kind`: `figure` (number + unit) or `fact` (one or two sentences, e.g. "RBI risk weight on unsecured
  consumer loans since Nov 2023").
- Caps: `DR_MAX_SUBTASKS` sub-tasks (existing, default 8); at most 8 fields per sub-task and 40 per task;
  at most 6 documents. Extra items are dropped and logged.
- Bad JSON: one retry with the parse error appended; then fall back to one sub-task whose single `fact`
  field is the whole objective (mirrors today's fallback).
- `documents` may be empty (topics with no natural documents).

### 2. Document phase (task-level, before sub-tasks start)

For each planned document, in plan order:

1. Search DDG with its `search` string; if no PDF results, once more with `"<issuer> <type> <period> pdf"`.
2. For up to 3 PDF candidates (in rerank order), run the PDF triage call (below) with the document's
   expected issuer/type/period. Triage returns a verdict; the first `match` is accepted.
3. Accepted → `documents` row `verified`; candidates that didn't match → `rejected` (kept for the log
   and UI, never offered or read). No match → the planned document is `not_found`.

PDFs met later by web-search rounds go through the same verification before they can be read or shared
(one triage call, which the read needed anyway). *(Builder call)* The expectation is the open field whose
search produced the hit: its entity as issuer, its period, type "any". A match becomes a task-level
`documents` row with the next key (D7, D8…), shown in the UI and offered to every running sub-task.
Everything runs on one asyncio loop, so no locks are needed; to stop two sub-tasks verifying the same URL
at once, keep a task-level dict of URL → in-flight verification future and await an existing one.

All sub-tasks start only after the document phase ends, so every sub-task sees the same verified list
from its first round. Duplicate URLs across planned documents are verified once.

### 3. PDF reading (pdf.py, prompts and return shape only)

Still: download (browser User-Agent) → indra-ocr the first 8 pages to text → larger model triage →
indra-ocr only the chosen pages → larger model extraction. Only text is sent to models.

- **Triage** takes `expect` (issuer/type/period) and `wanted` (list of open fields: name, entity, period)
  and returns `{"match": bool, "about": "...", "covers": "...", "pages": [[s,e]...]}`. `match=false`
  short-circuits: no pages read. `skip` (pages already read) stays.
- **Extraction** takes the same `wanted` list and returns per field:
  `{"field": <id>, "value": "...", "unit": "...", "period": "...", "page": N, "quote": "exact text"}`, or
  nothing for fields not on these pages.
- Each `quote` must appear verbatim (whitespace-normalised) in the OCR text of its page, else the value
  is dropped and logged. Same rule as web passages today.
- Returned also: pages read, `about`, `covers`.

### 4. Sub-task rounds (rules, no Qwen choice)

State per sub-task: its fields (open / found / unavailable), pages read per document, URLs read,
queries tried. Each round picks the **first applicable** step:

1. **Read a verified document**: one listed in an open field's `sources` (then any other verified
   document whose issuer matches an open field's entity) that still has unread relevant pages for the
   open fields (triage returns no new pages → that document is exhausted for this sub-task).
2. **Planned search**: an open field's planner `search` not yet tried.
3. **Rule search**: `"<entity> <field name> <period>"`, then the same with `pdf`, per open field, skipping
   near-duplicates (existing 80% word-overlap rule).
4. **Nothing left** → immediate check-in.

A web-search round: DDG → fetch every result (skipping URLs already read) → Qwen rerank against the open
fields → Qwen copies verbatim passages (existing) → **Qwen fills fields from passages**: given one
passage and the open fields, return `{"field": id, "value", "unit", "period", "quote"}` or nothing; the
quote must be verbatim in the passage. PDF hits go through verification (section 2) and, if matched,
are read in the same round.

A field filled from the web keeps `source_kind=web`. A later document value for the same field replaces
a web value (documents outrank web pages); a later web value never replaces a document value; nothing
replaces an `accepted` value.

*(Builder call)* Wrong period/entity: the extraction and fill prompts state each field's required entity
and period and tell the model to skip values for others. Code does **not** string-compare periods
("31 March 2026" vs "FY2025-26" would falsely fail). Every value is stored with the period the model
reported, and the check-in (which sees both) accepts or rejects it. So a wrong value can sit in the table
until the next check-in, but it can never reach the report as accepted.

Qwen's jobs in a task are now only: rerank, copy passages, fill fields from a passage. It writes no
queries and picks no documents.

### 5. Check-ins (larger model)

When: every `CHECKIN_EVERY` (5) rounds, when step 4 is reached, and when all fields are filled
(confirmation). Input: the sub-task's fields table (name, entity, period, value, unit, source, page,
quote, source kind), documents read with pages, and queries tried. Output JSON:

```json
{"fields": [{"id": 12, "action": "accept|reject|unavailable|retry", "search": "new query", "note": "..."}],
 "add_fields": [{"name": "...", "entity": "...", "period": "...", "kind": "figure", "search": "..."}],
 "find_document": {"name": "...", "issuer": "...", "type": "...", "period": "...", "search": "..."}}
```

- `reject`: value cleared, field reopened (e.g. wrong period). `retry` with `search`: adds that query to
  the field's queue. `unavailable`: closes the field as not publicly available, with `note`.
- `add_fields`: up to 3 per check-in, within the 8-per-sub-task cap.
- `find_document`: runs the document phase for one more document (verified the same way).
- *(Builder call)* A `found` field needs a check-in `accept` (the `accepted` flag) to count toward done.
  Sub-task ends `done` when every field is accepted or `unavailable`. When limits hit, it ends `partial`;
  found-but-unreviewed values still go to the report, marked "unreviewed".
- The confirmation check-in when all fields are filled always runs and its actions always apply, even past
  the restart cap. If it rejects anything past the cap, the field reopens but the sub-task ends `partial`.

Limits (existing settings, re-read): `DR_ROUNDS_PER_LOOP` becomes max rounds per sub-task (default 20);
`DR_MAX_RESTARTS` becomes the number of check-ins that may reopen work, i.e. return `reject`/`retry`/
`add_fields`/`find_document` (default 2). *(Builder call)* Every reopening check-in counts, including the
first. When the cap is hit, that check-in's `accept`/`unavailable` actions still apply, its reopening
actions are dropped (logged), and the sub-task ends `partial`. Names stay so `.env` files keep working;
README updated.

### 6. Report (larger model)

Input: objective, today's date, the fields table grouped by sub-task (with sources, pages, periods),
verified documents list, and remaining `fact` passages. Prompt adds: present key figures in tables; list
`unavailable` and still-open fields under "Gaps"; documents outrank web pages when they disagree. The
existing citation and no-invention rules stay. Input cap stays 60,000 chars.

### 7. Data model

New tables (old tables stay; old tasks render as before):

```sql
documents(id, task_id, key, name, issuer, type, period, url, about, covers, pages,
          status)            -- planned | verified | rejected | not_found
fields(id, task_id, subtask_id, name, entity, period, kind, sources, search,
       status,               -- open | found | unavailable
       accepted,             -- 0/1: a check-in confirmed the value
       value, unit, source_url, page, quote, source_kind, note)
```

`subtasks` keeps its columns (`query`, `needs_document` unused for new tasks). `rounds.query` holds the
round label ("read D1 Navi Finserv annual report pp. 112–118" or the search). Evidence rows stay as the
raw provenance store.

### 8. UI

The task box gains, above the sub-tasks, a **Documents** line per planned document: name and status
(✓ verified, link to the PDF / ✗ not found / … searching). Each sub-task row shows field progress
("4/6 fields") and, expanded, a small table:

```
Gross NPA %   FY2025-26   2.1%     annual report p.112      ✓
Net NPA %     FY2025-26   —        searching
PCR %         FY2025-26   not public (check-in: not disclosed)
Rounds: 1. read D1 pp. 110–118 · 2. Navi Finserv net NPA March 2026 · …
```

New SSE events: `document` and `field` (same pattern as `subtask`). Same colours and tokens; orange only
for running states.

### 9. Budget (per research, typical)

1 plan + ~6 documents × up to 3 triage calls + ~15 document reads × 2 calls + ~7 sub-tasks × 2–4
check-ins + 1 report ≈ 60–120 cloud calls. This is an estimate, not a ceiling. *(Builder call)* The hard
limit is a task-wide PDF read budget, new setting `DR_MAX_PDF_READS` (default 30). Each triage+extraction
read counts as one, including deeper reads of the same document; document-phase verification triage
doesn't count. When it runs out, round step 1 is skipped (web search continues) and it's logged once. That
bounds one research at roughly 1 + 18 + 60 + 30 + 1 ≈ 110–150 calls worst case. Daily limits: Gemini 500 + 500, Groq 1000. The existing
fallback (Gemini → Groq Qwen 27B, pause when all are down) stays. The per-sub-task PDF read cap rises
from 5 to 8 because reads are now targeted at open fields; document-phase triage calls don't count
toward it.

## What stays as is

Chat routing and quick search; SSE streaming and the busy indicator; URL skip-lists and the 80%
near-duplicate query rule; Gemini → Groq fallback with user notices and pause; logs with
`[c t s]` tags (new log lines for plan fields, document verdicts, field fills, check-in actions);
delete-conversation; restart marking work interrupted.

## Left to the builder

- Exact prompt wording (must stay short; Qwen prompts especially).
- Whether field-filling from a passage batches several passages per Qwen call.
- Table/CSS details within the existing design tokens.
- Test layout, following the existing `Env`-style fakes in `backend/tests/test_task.py`.
