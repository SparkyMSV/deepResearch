# Local-Model Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the research task engine with a harness in which local Qwen explores the web and PDFs
field by field, while the larger model only plans a field checklist, reviews it at check-ins, and writes
the report.

**Architecture:** The plan (cloud) yields documents and fields. A task-level document phase finds and
Qwen-verifies documents into a shared PDF store (download once, OCR pages on demand, cached). Each
sub-task runs Qwen steps (choose action → visit a document page by page / web search → read page and fill
fields with verbatim quotes), with focus rotation, loops, check-ins over a fields table, and a page budget.
New modules keep each concern small: `plan.py`, `pdf.py` (rewritten as the store), `docs.py`,
`fields.py`, `explore.py`, `checkin.py`; `task.py` orchestrates.

**Tech Stack:** Python 3.13 / FastAPI / sqlite3 / mlx-lm / ddgs / trafilatura / indra-ocr + pypdfium2 /
openai client (Gemini, Groq); React 18 + Vite + vitest.

**Spec:** `docs/superpowers/specs/2026-11-24-field-checklist-redesign-design.md`

## Global Constraints

- No PDF or page image is ever sent to a cloud model; cloud roles used by tasks: `plan`, `checkin`, `report`.
- Every stored value has a `quote` that appears verbatim in its page text after normalisation
  (collapse whitespace; map ’‘ → ', “” → "); otherwise it is dropped and logged.
- Qwen: one decision per call, compact views, JSON replies parsed with `local_llm.parse_json`; all task
  Qwen calls use `priority=WORK`.
- Caps: 8 fields per sub-task, 40 per task, 6 planned documents, ≤3 doc candidates verified per document,
  ≤3 web results opened per search, ≤6 pages per document visit, page text capped at 6,000 chars,
  focus rotates after 3 no-progress steps.
- Settings: existing `DR_MAX_SUBTASKS` (8), `DR_ROUNDS_PER_LOOP` (20 steps per loop), `DR_MAX_RESTARTS`
  (2 reopening check-ins), `CHECKIN_EVERY` (5); new `DR_MAX_PAGES` (150 pages read per task;
  verification pages don't count).
- Code never string-compares periods; the reported period is stored for the check-in.
- Value precedence: document beats web; web never replaces document; nothing replaces an accepted value.
- No new dependencies. Keep the `[c t s]` logging tags; log every Qwen action and reply (`clip`ped).
- UI: existing CSS tokens; orange only for running states.
- Python code must also run on Python < 3.12 (no nested same-quote f-strings).

## Review Focus

1. Qwen names a field differently from the plan ("GNPA" for "Gross NPA %") → reading prompts number the
   fields and accept the number or a case-insensitive name; test in Task 6.
2. Curly quotes or line breaks differ between Qwen's quote and the OCR text → normalisation accepts it;
   test in Task 6.
3. A PDF download fails (403, timeout, >50 MB) in the document phase or mid-visit → candidate rejected
   or document marked exhausted for that sub-task; research continues; tests in Tasks 4 and 7.
4. A scanned page OCRs to empty text, or a PDF has no contents page → page shown as
   "(no text on this page)" and contents fall back to raw first lines; tests in Tasks 3 and 7.
5. The planner returns unknown source keys, missing periods or too many items → unknown keys dropped,
   missing period becomes "latest", caps applied; test in Task 2.

---

### Task 1: Schema, settings, API detail

**Files:**
- Modify: `backend/app/db.py` (SCHEMA), `backend/app/config.py`, `backend/app/main.py:81-96,122-140`
- Test: `backend/tests/test_db.py`, `backend/tests/test_api.py`

**Interfaces:**
- Produces: tables `documents(id, task_id, key, name, issuer, type, period, url, about, covers, pages,
  status, domains, search)` and `fields(id, task_id, subtask_id, name, entity, period, kind, sources,
  search, hint, status, accepted integer default 0, value, unit, reported_period, source_url, source_key,
  page, quote, source_kind, note)`; `sources`/`domains` stored as JSON text. `config.MAX_PAGES` (env `DR_MAX_PAGES`,
  default 150, via `_positive_int`). `GET /api/conversations/{cid}` tasks gain `documents` and `fields`
  lists (ordered by id). Delete-conversation also deletes the task's documents and fields.

- [ ] **Step 1: Failing tests** — `test_db.py::test_new_tables_exist` (insert one row into each table via
  `db.insert`, read back); `test_api.py::test_detail_includes_documents_and_fields` (insert a document and
  a field for a task, GET detail, assert `tasks[0]["documents"][0]["key"] == "D1"` and
  `tasks[0]["fields"][0]["name"] == "Gross NPA %"`); extend `test_delete_conversation_removes_everything`
  to insert one document and one field and assert both tables are empty for that task afterwards.
- [ ] **Step 2: Run** `cd backend && python3 -m pytest tests/test_db.py tests/test_api.py -q` → new tests FAIL.
- [ ] **Step 3: Implement** the two tables, `MAX_PAGES`, the detail lists and the delete statements.
- [ ] **Step 4: Run** the same command → PASS; then full `python3 -m pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat: documents and fields tables, DR_MAX_PAGES, detail API`

### Task 2: Plan (cloud)

**Files:**
- Create: `backend/app/plan.py`
- Test: `backend/tests/test_plan.py`

**Interfaces:**
- Consumes: `cloud("plan", messages, conn)`, `parse_json`, `db.insert`.
- Produces: `async def make_plan(conn, task_id: int, objective: str, today: str) -> list[int]` — inserts
  `documents` rows (status `planned`) and `subtasks` rows (status `queued`, `query` = objective) and
  `fields` rows (status `open`); returns sub-task ids. `PLAN_PROMPT` constant (contains `{today}`,
  `{n}`). `QuotaExhausted` propagates (the caller retries).

Rules: one retry with `"Your reply was not valid JSON: <error>. Reply with JSON only."` appended; then
fallback = one sub-task (objective) with one `fact` field (name = objective, entity = "", period =
"latest"). Drop `sources` keys not in the plan's documents; missing/blank period → `"latest"`; `kind`
not in (`figure`, `fact`) → `fact`; caps from Global Constraints (drop extras, `log.info` how many).
Document keys are re-issued as `D1..Dn` in plan order and `sources` remapped.

- [ ] **Step 1: Failing tests** in `test_plan.py` with a fake `cloud`:
  - `test_plan_inserts_documents_subtasks_fields` — the spec's example JSON → 1 document `D1` planned,
    1 sub-task, 1 field with `sources == '["D1"]'`, `hint` stored.
  - `test_today_in_prompt` — the system prompt contains `"2026-11-24"`.
  - `test_caps_and_cleanup` — 10 sub-tasks × 10 fields, 8 documents, a field with `sources: ["D9"]` and no
    period → 8 sub-tasks, ≤8 fields each, ≤40 total, 6 documents, unknown key dropped, period `"latest"`.
  - `test_bad_json_retries_then_falls_back` — two non-JSON replies → 2 cloud calls, 1 sub-task with 1
    `fact` field named after the objective.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_plan.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `make_plan`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: plan a field checklist with documents and today's date`

### Task 3: PDF store (rewrite `pdf.py`)

**Files:**
- Modify: `backend/app/pdf.py` (replace `read_pdf`/`PdfRead` with the store; keep `_download`,
  `_converter`, `_lock`, `MAX_BYTES`)
- Test: `backend/tests/test_pdf.py` (replace old tests; keep the fitz fixture style and `Resp` fake)

**Interfaces:**
- Produces:
  - `class Contents(NamedTuple): entries: list[tuple[str, int]]; raw: str` (`raw` is "" when entries ≥ 5)
  - `class PdfStore`: `async def pages(url) -> int` (downloads once to a temp file, browser User-Agent,
    raises on failure; caches the count); `async def text(url, n: int) -> str` (1-based; indra-ocr
    markdown via `asyncio.to_thread` under `_lock`; cached per (url, n); `""` stays `""`);
    `async def contents(url) -> Contents`; `async def locate(url, title: str, printed: int) -> tuple[int, bool]`;
    `def close()` (deletes temp files).
- Contents rule: from pages 1–8, lines matching `^(.{3,80}?)[\s.·…_-]{2,}(\d{1,4})\s*$` → (title, printed
  page); ≥5 → `entries[:60]`, raw ""; else entries `[]`, raw = first 40 non-empty lines of pages 1–8.
- Locate rule: check pages printed, printed+1, printed−1, … up to ±15 within bounds, stop at the first
  page whose normalised text contains the normalised title (case-insensitive); none → (clamped printed, False).

- [ ] **Step 1: Failing tests** (12-page fitz PDF: page 2 is a contents page with 5 lines like
  `"Directors' report ..... 4"`; the title text appears on PDF page 6, i.e. offset +2):
  - `test_downloads_once_and_caches_pages` — two `text()` calls on the same page → one download, one OCR.
  - `test_contents_entries` — 5 entries parsed, `raw == ""`.
  - `test_contents_falls_back_to_raw_lines` — PDF without contents lines → `entries == []`, `raw` non-empty.
  - `test_locate_finds_offset_page` — `locate(url, "Directors' report", 4) == (6, True)`;
    unknown title → `(4, False)`.
  - `test_download_failure_raises` — HTTP error → `pages()` raises.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_pdf.py -q` → FAIL.
- [ ] **Step 3: Implement** `PdfStore`. Delete `read_pdf`, `PdfRead` and the cloud import.
- [ ] **Step 4: Run** → PASS. (`test_task.py` will fail until Task 9; run only `test_pdf.py` here.)
- [ ] **Step 5: Commit** `feat: task-level PDF store with cached OCR pages, contents and offset locate`

### Task 4: Search helpers (screening, window)

**Files:**
- Modify: `backend/app/search.py`
- Test: `backend/tests/test_search.py`

**Interfaces:**
- Produces:
  - `def name_words(name: str) -> set[str]` — lowercase words ≥3 chars minus
    {limited, ltd, pvt, private, inc, corp, corporation, company, co, plc, llp, the, and, of}.
  - `def mentions(hit: Hit, words: set[str]) -> bool` — any word in URL + title + snippet (lowercase).
  - `def doc_score(hit: Hit, words: set[str], period: str, domains: list[str]) -> int` — +2 per issuer word
    found, +2 if any 4-digit year or `FYnn` from `period` appears, +1 if host ends with a listed domain,
    −2 if a different 4-digit year 2000–2099 appears in URL/title and none of the period's years do.
  - `def order_by_entity(hits: list[Hit], entity: str) -> list[Hit]` — stable: hits that `mentions` first.
  - `def window(text: str, keywords: list[str], n: int = 6000) -> str` — first case-insensitive keyword
    hit at i → `text[max(0, i - n // 4):][:n]`; none → `text[:n]`.
  - `ddg()` unchanged; `fetch()` unchanged.

- [ ] **Step 1: Failing tests:** `test_name_words` (`"Navi Finserv Limited"` → `{"navi","finserv"}`);
  `test_screen_drops_other_companies` (Nazara and "Wizard's Wardrobe" annual-report hits fail `mentions`
  for Navi words; the navi-tech.in URL passes via its host); `test_doc_score_orders` (`2023-24.pdf` on
  navi.com beats a `2019-20` file on another host for period "FY2023-24"); `test_order_by_entity_keeps_all`;
  `test_window_centres_on_keyword`.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_search.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: free URL/title/snippet screening and keyword windows`

### Task 5: Task context and test fakes

**Files:**
- Create: `backend/app/ctx.py`, `backend/tests/fakes.py`
- Test: `backend/tests/test_ctx.py`

**Interfaces:**
- Produces in `ctx.py`:
  - `@dataclass class TaskCtx: conn; conv_id: int; task_id: int; today: str; store: PdfStore;
    inflight: dict[str, asyncio.Future] = {}; pages_used: int = 0` with
    `def pages_left(self) -> bool` (`pages_used < config.MAX_PAGES`),
    `def doc(self, key: str) -> dict | None`, `def docs(self, status="verified") -> list[dict]`,
    `def set_doc(self, row: dict, **cols)` and `def set_field(self, row: dict, **cols)` (update DB, update
    the dict, publish `{"type": "document", "document": {...}}` / `{"type": "field", "field": {...}}`
    with the columns listed in Task 1 minus `quote`/`search`/`sources`).
  - Sub-task runtime state is the sub-task row dict plus keys `urls: set[str]`, `queries: list[str]`,
    `read: dict[str, set[int]]` (doc key → pages read), `done_docs: set[str]`, `focus: int | None`,
    `stall: dict[int, int]`, `no_progress: set[int]`. Helper `def new_state(row: dict) -> dict`.
- Produces in `tests/fakes.py`: `FakeStore` (same async API as `PdfStore`, built from
  `{url: [page texts]}`, records `text()` calls; contents from a given list) and `ScriptedQwen`
  (list of `(substring, reply_or_callable)`; first rule whose substring is in the prompt wins; records
  prompts; default reply `"{}"`).

- [ ] **Step 1: Failing test** `test_set_field_publishes_event` (monkeypatch `events.publish`; `set_field`
  on an inserted field → DB updated, one `field` event with `status`).
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat: task context with document/field events; test fakes`

### Task 6: Reading a page and filling fields

**Files:**
- Create: `backend/app/fields.py`
- Test: `backend/tests/test_fields.py`

**Interfaces:**
- Consumes: `TaskCtx`, sub-task state (Task 5), `qwen`, `parse_json`, `db.insert` (evidence).
- Produces:
  - `def norm(s: str) -> str` (whitespace collapse + quote mapping from Global Constraints).
  - `def open_fields(conn, subtask_id: int) -> list[dict]` (status `open`, by id).
  - `async def read_page(ctx, sub, text: str, source: dict, nav: bool) -> tuple[list[int], object]` —
    `source = {"url", "title", "key": str|None, "page": int|None, "kind": "doc"|"web"}`. Shows the
    page view from the spec (fields numbered `[1] Gross NPA % (Navi Finserv Limited, FY2025-26)`; empty
    text shown as `(no text on this page)`; `"next"` part of the reply format only when `nav`). Returns
    filled field ids and the `next` value (`int`, `"contents"`, `"leave"` or `None`). Increments
    `ctx.pages_used`. Unparseable reply → one retry ("Reply with JSON only"), then `([], "leave")`.
  - Filling: each `found` item's `field` may be the number or a case-insensitive name; quote must be in
    `norm(text)` (as `norm(quote)`); precedence rules from Global Constraints; on fill: `set_field(row,
    status="found", accepted=0, value=..., unit=..., reported_period=..., source_url=..., source_key=...,
    page=..., quote=..., source_kind=...)`, plus one `evidence` row (`passage=quote`, `page`, `url`,
    `title`, `published=None`, `fetched_at=ctx.today`, `loop=sub["loop"]`).

- [ ] **Step 1: Failing tests** with `ScriptedQwen`:
  - `test_fills_by_number_or_name` — reply uses `"field": 1` for one and `"gross npa %"` for another → both filled.
  - `test_quote_must_be_verbatim` — invented quote → not filled, nothing in evidence.
  - `test_curly_quotes_and_newlines_normalised` — page has `Company’s GNPA\nwas 2.1%`, quote has
    `Company's GNPA was 2.1%` → filled.
  - `test_document_beats_web_never_reverse` — web value then doc value → doc wins; doc then web → doc
    stays; accepted value → unchanged.
  - `test_unparseable_twice_leaves` — `("not json", "not json")` → `([], "leave")`, 2 Qwen calls.
  - `test_page_counts_against_budget` — `ctx.pages_used` goes 0 → 1.
- [ ] **Step 2–4:** run `python3 -m pytest tests/test_fields.py -q` → FAIL, implement, → PASS.
- [ ] **Step 5: Commit** `feat: Qwen reads a page and fills fields with verbatim quotes`

### Task 7: Documents — phase, verification, navigator

**Files:**
- Create: `backend/app/docs.py`
- Test: `backend/tests/test_docs.py`

**Interfaces:**
- Consumes: Tasks 3–6 (`PdfStore`, search helpers, `TaskCtx`, `read_page`), `ddg`, `fetch`.
- Produces:
  - `async def verify(ctx, url: str, expect: dict) -> dict | None` — `expect = {"issuer", "type",
    "period"}`; one in-flight future per URL in `ctx.inflight` (concurrent callers await the same one);
    reads page 1 (+2 if page 1 < 300 chars) via `ctx.store.text` (not counted in `pages_used`); Qwen
    reply `{"match", "about", "covers"}`; returns `{"about", "covers", "pages"}` on match, else `None`;
    any download/OCR error → `None` (logged).
  - `async def find_document(ctx, doc: dict) -> None` — search `doc["search"]`, else the fallback query
    when no PDF hits (a hit is a PDF if its URL ends `.pdf` or `fetch` marks `is_pdf`); hard screen with
    `mentions(name_words(issuer))`; sort by `doc_score`; verify up to 3; first match →
    `set_doc(status="verified", url, about, covers, pages)`; non-matching verified candidates are logged;
    none → `status="not_found"`.
  - `async def add_found_pdf(ctx, url: str, field: dict) -> dict | None` — verification with
    `{"issuer": field["entity"], "type": "any", "period": field["period"]}` and the "about this entity and
    period" wording; match → new `documents` row with the next `D<n>` key, status `verified`; returns it.
  - `async def visit(ctx, sub, key: str) -> int` — the navigator from the spec: ≤10-page docs read from
    the first unread page; otherwise contents view (`entries` numbered, or `raw` lines) → Qwen
    `{"open": i}` (→ `locate`) / `{"page": N}` (clamped; an already-read page → next unread after it) /
    `{"leave": true}`; then `read_page(nav=True)` and follow its `next`; stops after 6 pages, on
    `"leave"`, when `ctx.pages_left()` is false, or when every page is read (→ `sub["done_docs"].add(key)`).
    A failing `store` call marks the doc done for this sub-task and returns. Returns pages read.

- [ ] **Step 1: Failing tests** (`FakeStore`, `ScriptedQwen`, monkeypatched `ddg`/`fetch`):
  - `test_find_document_screens_then_verifies` — results: Nazara AR pdf, Navi AR pdf → Nazara never
    downloaded; Navi verified; document row `verified` with `about`.
  - `test_find_document_not_found_after_three_rejections` — 4 Navi-named candidates, Qwen says no →
    exactly 3 verified attempts, status `not_found`.
  - `test_verify_dedupes_concurrent_calls` — two concurrent `verify` on one URL → one page-1 read.
  - `test_verify_download_error_is_none`.
  - `test_visit_contents_then_pages` — Qwen opens contents entry 2, then `next: 7`, then `leave` →
    pages read == locate result and 7; `sub["read"]["D1"]` holds both.
  - `test_visit_short_doc_reads_from_page_one` and `test_visit_stops_at_six_pages`.
- [ ] **Step 2–4:** run → FAIL, implement, → PASS.
- [ ] **Step 5: Commit** `feat: document phase with free screening, Qwen verification and page navigator`

### Task 8: Choosing a step and web search

**Files:**
- Create: `backend/app/explore.py`
- Test: `backend/tests/test_explore.py`

**Interfaces:**
- Consumes: Tasks 4–7; `task._fresh`/`_qkey` move here unchanged (80% overlap rule) as `fresh`, `qkey`.
- Produces:
  - `async def choose(ctx, sub, field: dict) -> tuple[str, str | None]` — renders the step view from the
    spec (options: verified docs not in `sub["done_docs"]`, `[S]`, `[X]`; field `hint` shown when set);
    returns `("doc", key)`, `("search", query)`, `("unavailable", why)` or `("escalate", None)`.
    Refused/unparseable reply → ask once more with the reason; still refused → fallback queue: the field's
    planner `search`, then `f"{entity} {name} {period}"` (each only if `fresh`), else `("escalate", None)`.
  - `async def web_search(ctx, sub, query: str, field: dict) -> int` — `ddg` (thread) → drop URLs in
    `sub["urls"]` → `order_by_entity(hits, field["entity"])` → numbered list (URL, title, snippet) →
    Qwen `{"open": [..]}` (≤3; invalid → `[1]`) → for each: add `url_key` to `sub["urls"]`, `fetch`
    (thread); PDF → `add_found_pdf`; HTML → `read_page(window(text, keywords), kind="web", nav=False)`
    where keywords = words of open field names; stops when `ctx.pages_left()` is false. Appends `query`
    to `sub["queries"]`. Returns pages read.

- [ ] **Step 1: Failing tests:**
  - `test_choose_doc_option_lists_only_unfinished_docs` — a doc in `done_docs` absent from the prompt.
  - `test_choose_refuses_repeat_then_falls_back_to_planner_search` — Qwen repeats a tried query twice →
    returns the field's planner search.
  - `test_choose_escalates_when_queue_exhausted`.
  - `test_web_search_opens_chosen_results_and_skips_seen` — 4 results, one already in `sub["urls"]`,
    Qwen opens [1, 3] → 2 fetches, neither the seen URL.
  - `test_web_search_pdf_becomes_document` — opened PDF verified → new `D2` document row.
- [ ] **Step 2–4:** run → FAIL, implement, → PASS.
- [ ] **Step 5: Commit** `feat: Qwen chooses its next step; web search opens chosen results`

### Task 9: Check-ins, orchestration, report

**Files:**
- Create: `backend/app/checkin.py`
- Modify: `backend/app/task.py` (rewrite: keep `_set`, `_post`, `_retry`, `_cap`, `_ask`, the
  `on_fallback` switch notice and `_paused`; delete `_task_docs`, `_read_doc`, `_round`, `_next`,
  `_fresh`/`_qkey` (moved), `MAX_PDFS_PER_SUBTASK`, `PLAN_PROMPT`, `CHECKIN_PROMPT`)
- Test: `backend/tests/test_checkin.py`, `backend/tests/test_task.py` (rewrite around the new loop; keep
  the quota-pause, switch-notice, auth-error and cooldown tests, adapted)

**Interfaces:**
- Produces:
  - `async def review(ctx, sub, allow_reopen: bool) -> str | None` in `checkin.py` — builds the fields
    table (name, entity, period, value, unit, source key/url, page, quote, kind, status, note), documents
    read with pages, queries; `cloud("checkin")` via `task._retry`; applies actions per spec section 5;
    reopening actions (`reject`, `retry`, `add_fields`, `find_document`) dropped and logged when
    `allow_reopen` is false. Returns `"reopened"`, `"ok"`, or `None` (API error / unparseable → caller
    ends the sub-task `partial`, as today).
  - `async def run_task(conn, conv_id, task_id) -> str` — `make_plan` (today = `date.today().isoformat()`)
    → `find_document` for each planned document in order → `run_subtask` for all via `gather` → report →
    `ctx.store.close()` in `finally`.
  - `async def run_subtask(ctx, subtask_id) -> None` — the loop:
    1. All fields accepted or unavailable → `done`.
    2. No open fields (all found/unavailable, some unreviewed) → confirmation `review(allow_reopen=restarts
       < MAX_RESTARTS)`; then done if (1) holds, new loop if `"reopened"` and allowed, else `partial`.
    3. `round >= ROUNDS_PER_LOOP`, focus escalation (every open field in `no_progress`), `("escalate")`
       from `choose`, or `round % CHECKIN_EVERY == 0` (after a step) → `review`; `"reopened"` → if
       `restarts < MAX_RESTARTS`: restarts+1, loop+1, round 0, clear `no_progress`; else `partial`.
       Step limit with `"ok"` → `partial`.
    4. `not ctx.pages_left()` → final `review(allow_reopen=False)` → done or `partial`.
    5. Otherwise one step on the focus field: `choose` → `visit` / `web_search` / mark `unavailable`
       (note = why). Record a `rounds` row (label "D1 pp. 110–114" or the query; `hits` = pages read)
       and publish `round`. Progress = any field filled this step; else `stall[focus] += 1`; at 3 → add
       to `no_progress`, focus moves to the next open field (wrapping).
  - `_report_input(conn, t)` from the fields table grouped by sub-task (value, unit, `reported_period`,
    source key/url, page, `accepted`/`unreviewed`, notes) plus the verified documents list; open and
    unavailable fields under "Gaps". `REPORT_PROMPT` adds: key figures in tables; documents outrank web
    pages; mark unreviewed values.

- [ ] **Step 1: Failing tests** (`test_checkin.py`): `test_accept_reject_unavailable_retry`,
  `test_reopening_dropped_when_not_allowed`, `test_add_fields_capped_at_three`,
  `test_find_document_runs_document_phase`, `test_unparseable_returns_none`.
- [ ] **Step 2: Failing tests** (`test_task.py`, with fakes for `make_plan` output, `ScriptedQwen`,
  `FakeStore`, cloud): `test_document_first_fills_fields_and_finishes_done`,
  `test_focus_rotates_after_three_stalls`, `test_step_limit_checkin_reopens_new_loop`,
  `test_restart_cap_ends_partial`, `test_page_budget_forces_final_checkin`,
  `test_checkin_failure_ends_only_that_subtask`, `test_report_input_marks_unreviewed_and_gaps`, plus the
  adapted quota/switch/auth/cooldown tests.
- [ ] **Step 3: Run** `python3 -m pytest tests/test_checkin.py tests/test_task.py -q` → FAIL.
- [ ] **Step 4: Implement**; remove the `pdf` role from `config.ROLES` and switch the two cloud tests
  that used it (`test_overload_or_timeout_falls_back_then_waits`,
  `test_pdf_falls_back_to_groq_and_reports_the_switch`) to the `plan` role with its model list.
- [ ] **Step 5: Run** full `python3 -m pytest -q` → PASS.
- [ ] **Step 6: Commit** `feat: field-driven sub-task loop with check-ins over a fields table`

### Task 10: Chat follow-ups use the fields table

**Files:**
- Modify: `backend/app/chat.py:49-62,142`
- Test: `backend/tests/test_chat.py`

**Interfaces:**
- Produces: `def _fields(conn, conv_id: int) -> str` — latest task's fields as lines
  `"<name> (<entity>, <period>): <value><unit> [<source>, p.<page>] (<accepted|unreviewed|status>)"`,
  capped at 4,000 chars; added to the answer context next to `_report` and `_evidence`.

- [ ] **Step 1: Failing test** `test_answer_context_includes_fields`.
- [ ] **Step 2–4:** run → FAIL, implement, → PASS.
- [ ] **Step 5: Commit** `feat: chat follow-ups see the research fields table`

### Task 11: UI — documents and fields

**Files:**
- Modify: `frontend/src/Chat.jsx:8-38` (export `applyEvent`; handle `document` and `field` events by
  upserting into `task.documents` / `task.fields`), `frontend/src/TaskChecklist.jsx`, `frontend/src/theme.css`
- Test: `frontend/src/TaskChecklist.test.jsx`, `frontend/src/Chat.test.jsx`

**Interfaces:**
- Consumes: detail API lists and events from Tasks 1 and 5.
- Produces UI per spec section 9: a Documents block above sub-tasks (`✓` verified with a link to the PDF,
  `✗ not found`, `… searching` for planned); per sub-task tag shows `"<accepted+unavailable>/<total>
  fields"` when it has fields; expanded view shows a fields table (name, period, value+unit or "—",
  source "D1 p.112" or the URL host, status "✓ accepted" / "unreviewed" / "not public: <note>" /
  "searching") and the steps list below it. Tasks without fields render exactly as today.

- [ ] **Step 1: Failing tests:** `TaskChecklist.test.jsx::shows documents with status`,
  `::shows field progress and table`, `::old task without fields renders as before`;
  `Chat.test.jsx::applyEvent upserts document and field`.
- [ ] **Step 2: Run** `cd frontend && npx vitest run` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npx vitest run` → PASS and `npm run build` → OK.
- [ ] **Step 5: Commit** `feat(frontend): documents line and per-field progress in the checklist`

### Task 12: Docs and live check

**Files:**
- Modify: `README.md` ("How it works today" → the harness as built; roadmap drops the redesign item),
  `.env.example` (add `DR_MAX_PAGES=150`)

- [ ] **Step 1:** Update both files.
- [ ] **Step 2: Live check** on port 8766 with a scratch DB (`DR_DB=/tmp/.../scratch.sqlite`), objective
  `task: make a report on Navi bonds investability with a credit view`. Pass when the log shows: a plan
  with today's date and fields; at least one verified document and no document rejected by the screen
  being downloaded; `visit` rounds reading PDF pages; fields filled with quotes; check-ins with actions; a
  report. Record timings and cloud call count in the commit message.
- [ ] **Step 3: Commit** `docs: README and .env for the local-model harness`
