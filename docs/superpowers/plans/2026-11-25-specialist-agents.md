# Specialist Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Bonsai's single all-tools loop with a main agent that delegates to a PDF agent (locate anchors,
read whole pages) and a web agent (search with history and remarks), on top of background document preparation.

**Architecture:** `docprep.py` prepares each verified PDF (description from the first pages, contents, per-section
offsets, a BM25 page index). `pdf_agent.py` and `web_agent.py` each run a short tool loop with their own prompt and
tools and return a report. `agent.py` becomes the main agent (delegate / mark not public / done) plus self-review.
Reviewers can no longer set "not public".

**Tech Stack:** Python 3.13, sqlite3, MTPLX OpenAI-compatible server (Bonsai), pypdfium2 + indra-ocr, ddgs, React/vitest.

**Spec:** `docs/superpowers/specs/2026-11-25-specialist-agents-design.md` (changes §3 of
`2026-11-25-bonsai-agent-harness-design.md`).

## Global Constraints

- Settings: `DR_MAX_DELEGATIONS` (6), `DR_PDF_STEPS` (12), `DR_WEB_STEPS` (12); `DR_MAX_STEPS` retired (log note if set); `DR_MAX_PAGES`, `DR_CONTEXT_TOKENS` (12000) kept.
- `read_pages`: printed pages, max 2 per call, ≤4,500 chars per page, whole pages only; quotes accepted only from pages read in full in this call (harness cache), never from `locate` snippets.
- `locate(query)`: top 6 anchors (printed page, section, 1–2 line passage); BM25 over ~600-char passages with ~100 overlap from the text layer; empty index → plain term search.
- Offsets: one per contents entry, applied from that entry to the next; ambiguous printed page → choices returned, agent passes `section`; front matter before the first entry is out of scope.
- `fields.page` = PDF page, new `fields.printed_page`; citations "D1 p.152 (printed 150)".
- Not-public guard: field reported missing by a web agent and, if any verified document exists, by a PDF agent, in this sub-task (across restarts).
- Reviewers (self-review, final review) cannot set unavailable; final review prompt forbids judging from its own beliefs.
- Search history research-wide, in memory; repeats (≥80% words) refused research-wide.
- Thinking off except self-review; all agents use the research server; one research at a time.
- No new dependencies. Each tool call/result logged (clipped) with `[c t s]` tags.

## Review Focus

1. A PDF with no contents page and no page-number footers → offset 0 flagged unverified; `read_pages` still works by printed=PDF; test in Task 2.
2. Sections restarting numbering → ambiguous printed page returns choices; with `section` it resolves; test in Task 2.
3. A scanned PDF (no text layer) → empty index, `locate` says so and suggests contents/reading; test in Task 3.
4. PDF agent reads past 12K tokens → old page texts replaced by placeholders, recording from them still works; test in Task 4.
5. Main agent tries `mark_not_public` before both specialists reported the field missing → refused with the reason; test in Task 6.

---

### Task 1: Schema, settings, description from the first pages

**Files:** Modify `backend/app/db.py`, `backend/app/config.py`, `backend/app/docs.py`; Test `backend/tests/test_db.py`, `backend/tests/test_docs.py`

**Interfaces — Produces:** `documents` columns `offset_ok integer`, `contents text`, `notes text`; `fields.printed_page integer`; `config.MAX_DELEGATIONS`, `PDF_STEPS`, `WEB_STEPS`; `docs._verify` reads pages 1.. until ≥3,000 chars (max 4 pages) and its prompt says a first page may be a cover letter / filing intimation and to describe the enclosed document.

- [ ] Step 1: failing tests — `test_new_columns`, `test_settings_specialists`, `test_verify_reads_past_a_cover_letter` (page 1 "Submission of Annual Report", page 2 the report; prompt contains page 2 text and "cover letter").
- [ ] Step 2–4: FAIL → implement → PASS (full suite).
- [ ] Step 5: commit `feat: schema and settings for specialist agents; documents described past cover letters`.

### Task 2: Document preparation — contents, per-section offsets, page mapping

**Files:** Create `backend/app/docprep.py`; Test `backend/tests/test_docprep.py`

**Interfaces — Produces:**
- `@dataclass Prepared: pages: int; contents: list[dict]  # {"title","printed","pdf"}; offset_ok: bool; index: "PageIndex"; scanned: int`
- `async def prepare(store, url) -> Prepared` (cached per url on the store): contents from first 12 pages (`store.contents`), each entry located on PDF pages printed−5…+25 by title in the page's first lines; no contents → footer/header page numbers on ~10 sampled pages; else offset 0, `offset_ok=False`.
- `def to_pdf(prep, printed: int, section: str | None = None) -> int | list[str]` — PDF page, or the list of section titles when ambiguous; raises `ValueError` for out-of-range / front matter.
- `def to_printed(prep, pdf_page: int) -> int | None`.
- `docs.find_document` / `add_found_pdf` call `prepare` after verification and store `pages, contents, offset_ok, notes` on the row.

- [ ] Step 1: failing tests (fitz-built PDFs as in `test_pdf.py`): `test_offset_from_contents`, `test_sections_restarting_numbering_are_ambiguous`, `test_footer_numbers_when_no_contents`, `test_unverified_offset_zero`, `test_to_printed_inverse`.
- [ ] Step 2–4: FAIL → implement → PASS.
- [ ] Step 5: commit `feat: background document preparation with per-section page offsets`.

### Task 3: Page index and `locate`

**Files:** Modify `backend/app/docprep.py`; Test `backend/tests/test_docprep.py`

**Interfaces — Produces:** `class PageIndex` with `@classmethod build(pages: dict[int, str], prep) -> PageIndex` (text-layer text per PDF page; passages ~600 chars, overlap ~100; BM25 k1=1.5, b=0.75; lowercase word tokens) and `def locate(self, query: str, k: int = 6) -> list[dict]  # {"printed","pdf","section","passage"}` (one best passage per page, pages ranked by best passage). `pdf.PdfStore.text_layer(url) -> dict[int, str]` (pypdfium2, no OCR).

- [ ] Step 1: failing tests: `test_locate_ranks_the_page_with_the_figure_first`, `test_locate_one_anchor_per_page`, `test_scanned_pages_not_indexed` (empty text layer → `scanned` count, `locate` returns []).
- [ ] Step 2–4: FAIL → implement → PASS.
- [ ] Step 5: commit `feat: BM25 page index and locate anchors`.

### Task 4: PDF agent

**Files:** Create `backend/app/pdf_agent.py`; Test `backend/tests/test_pdf_agent.py`

**Interfaces — Produces:** `async def explore(ctx, sub, doc_key: str, field_nos: list[int], goal: str, on_round) -> str` — runs a fresh loop (`local("research", …, tools=PDF_TOOLS)`), ≤`PDF_STEPS` calls, ≤3 calls per turn; tools `locate(query)`, `read_pages(from, to, section=None)`, `record_value(field, value, unit, period, quote, page)`, `report(found, missing, notes)`. Pages read cached (`{printed: text}`) for quote checks; `record_value` uses `fields.record` with source page = PDF page and sets `printed_page`. Compaction past `CONTEXT_TOKENS`: earlier `read_pages` tool results replaced by "[page N read; text removed to save space]". Returns the report text (found/missing/notes/pages read); fields reported missing are added to `sub["missing_pdf"]`.

- [ ] Step 1: failing tests (scripted `local`, `FakeStore` + a prepared fake): `test_locate_then_read_then_record`, `test_record_refused_from_snippet_only`, `test_read_pages_max_two_and_reread_refused`, `test_compaction_keeps_recording_possible`, `test_report_ends_and_returns_summary`, `test_step_budget_ends_with_auto_report`.
- [ ] Step 2–4: FAIL → implement → PASS.
- [ ] Step 5: commit `feat: PDF agent — locate anchors, read whole pages, record`.

### Task 5: Web agent

**Files:** Create `backend/app/web_agent.py`; Test `backend/tests/test_web_agent.py`

**Interfaces — Produces:** `ctx.search_history: list[dict]` (`{"query","links":[(title,url)],"opened":[n],"remark":str}`, research-wide). `async def search(ctx, sub, field_nos, goal, on_round) -> str` — fresh loop, ≤`WEB_STEPS`; tools `search(query, note="")`, `open(n)`, `record_value(field, value, unit, period, quote)`, `report(found, missing, notes)`. Context shows the objective and the last 10 history entries (older summarised one line each). Remarks written by the harness: "helpful — recorded X from [n]" / "not helpful — opened …, nothing recorded" (+ agent note). PDFs opened → `docs.add_found_pdf` (prepared), call continues, report lists "new document Dn". Missing fields → `sub["missing_web"]`.

- [ ] Step 1: failing tests: `test_history_shows_links_and_remarks`, `test_repeat_refused_research_wide`, `test_open_pdf_becomes_document_and_continues`, `test_remark_helpful_after_record`.
- [ ] Step 2–4: FAIL → implement → PASS.
- [ ] Step 5: commit `feat: web agent with research-wide search history and remarks`.

### Task 6: Main agent, guard, reviews

**Files:** Modify `backend/app/agent.py` (replace `run_loop`; keep `self_review`, `run_subtask`, `_pauser`), `backend/app/final.py`, `backend/app/task.py` (`_report_input` gaps), delete old agent tools from `backend/app/tools.py` (keep `fresh/qkey`); Test `backend/tests/test_agent.py` (rewrite loop tests), `backend/tests/test_final.py`, `backend/tests/test_task.py`

**Interfaces — Produces:** main tools `explore_document(doc, fields, goal)`, `search_web(fields, goal)`, `mark_not_public(field, reason)`, `done(note)`; ≤`MAX_DELEGATIONS` delegations per loop; specialist reports appended as tool results; guard per Global Constraints (refusal text names what's missing). `sub["looked"][field_id]` accumulates "D1 pp. …" / "N web searches"; at sub-task end, open fields get note "not found (looked in: …)". Self-review: found → accept/reject/retry; unavailable → accept/retry (no setting unavailable). Final review: actions accept/reject only; prompt adds "judge only from the values, quotes and notes shown; not from your own beliefs about the entity"; open fields untouched.

- [ ] Step 1: failing tests: `test_main_delegates_and_appends_reports`, `test_delegation_budget`, `test_not_public_guard_refuses_then_allows`, `test_self_review_cannot_set_unavailable`, `test_final_review_has_no_unavailable_action`, `test_gaps_say_where_looked`.
- [ ] Step 2–4: FAIL → implement → PASS (full suite).
- [ ] Step 5: commit `feat: main agent delegates to PDF and web agents; reviewers can't invent not public`.

### Task 7: UI, README, settings docs

**Files:** Modify `frontend/src/TaskChecklist.jsx` (+test: citation shows printed page when present), `README.md`, `.env.example`

- [ ] Step 1–4: failing UI test `shows printed page in source` → implement → PASS; `npm run build` OK.
- [ ] Step 5: commit `feat: printed-page citations in the UI; README for specialist agents`.

### Task 8: Live check

- [ ] Scratch backend on 8766 (own DB) with the user's MTPLX servers; run the Nexus and Bharti Telecom requests. Pass when: documents prepared with offsets; PDF agents use `locate` then `read_pages`; values recorded with page + printed page; web agent history with remarks; no reviewer-set "not public"; reports written.
