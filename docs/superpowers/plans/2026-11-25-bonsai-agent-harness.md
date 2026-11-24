# Bonsai Agent Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 4B step-menu harness with a Bonsai 2 27B tool-using agent loop on MTPLX, keep chat on
the 4B (also MTPLX), and shrink cloud work to plan, one final review and the report.

**Architecture:** `local_llm.py` becomes an OpenAI-client wrapper over two MTPLX servers (research, chat)
with memory-pause retries. `tools.py` holds the agent's tools with guardrails inside them; `agent.py` runs
one sub-task as a tool loop with a fixed cached opening, compaction, budgets and Bonsai self-review;
`final.py` runs consistency checks and the cloud final review; `task.py` orchestrates and queues researches
globally. Step-menu code (`explore.choose`, `docs.visit/_pick`, `fields.read_page`, `checkin.py`) is removed.

**Tech Stack:** Python 3.13, FastAPI, sqlite3, openai client (MTPLX + Gemini/Groq), ddgs, trafilatura,
indra-ocr/pypdfium2; React 18 + Vite + vitest.

**Spec:** `docs/superpowers/specs/2026-11-25-bonsai-agent-harness-design.md` (builds on
`2026-11-24-field-checklist-redesign-design.md`).

## Global Constraints

- Local servers: research `DR_RESEARCH_URL` default `http://127.0.0.1:8001/v1`; chat `DR_CHAT_URL` default `http://127.0.0.1:8002/v1`; model ids from each server's `/v1/models`.
- Thinking off on every local call (`extra_body={"chat_template_kwargs": {"enable_thinking": False}}`) except Bonsai self-review (`True`).
- 507 `insufficient_memory` or connection refused → wait 10 s doubling to 120 s and retry; research sub-task status `paused_memory`; one chat message per pause.
- New settings: `DR_MAX_STEPS` (30 tool calls per loop), `DR_CONTEXT_TOKENS` (12000). Kept: `DR_MAX_SUBTASKS`, `DR_MAX_RESTARTS` (2), `DR_MAX_PAGES`, search/log settings. Retired: `DR_ROUNDS_PER_LOOP`, `CHECKIN_EVERY` (ignored with a log line).
- `fields.accepted`: 0 unreviewed, 1 accepted by Bonsai self-review, 2 confirmed by cloud final review. `record_value` never overwrites `accepted >= 1`; only the final review clears values.
- Verbatim-quote rule unchanged (`fields._supported`: ≥12 chars, `...`/`…` elision, markdown-safe `norm`, value digits in quote); document beats web; web never replaces document.
- Max 3 tool calls run per turn; tool results are plain text ending with the field status line `filled N/M: [1] ✓ [2] – [3] not public?`.
- One research at a time across conversations (status `queued`); sub-tasks of a research run sequentially.
- Cloud calls per research: plan, final review, report (plus fallbacks). No PDF sent to any model.
- No new dependencies. Logs keep `[c t s]` tags; every tool call and result logged (clipped).

## Review Focus

1. Bonsai calls a tool with bad arguments (unknown field number, missing quote, doc key that doesn't exist, page out of range) → the tool returns a short error string and the loop continues; test in Task 3.
2. Bonsai replies with text and no tool calls → the harness nudges once ("Use a tool, or call done"), then ends the loop; test in Task 4.
3. MTPLX server not running at research start → sub-task shows `paused_memory` with the "start MTPLX" message and resumes when it comes up; test in Task 1 (retry) and Task 4 (status).
4. A conversation grows past `DR_CONTEXT_TOKENS` mid-loop → compaction keeps field status, pages read and searches; refused repeats still refused after compaction; test in Task 4.
5. Two researches started in different conversations → the second shows `queued` and starts after the first; test in Task 7.

---

### Task 1: Local model client over MTPLX

**Files:**
- Modify: `backend/app/local_llm.py` (replace MLX backend and priority queue), `backend/app/config.py`
- Test: `backend/tests/test_local_llm.py` (rewrite)

**Interfaces:**
- Produces:
  - `config.RESEARCH_URL`, `config.CHAT_URL`, `config.MAX_STEPS=_positive_int("DR_MAX_STEPS", 30)`, `config.CONTEXT_TOKENS=_positive_int("DR_CONTEXT_TOKENS", 12000)`.
  - `@dataclass class Reply: content: str; tool_calls: list[dict]  # {"id","name","args": dict}; prompt_tokens: int`
  - `class LocalUnavailable(Exception)` with `.reason` ("memory" | "down").
  - `async def local(role: str, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800, think: bool = False, on_token=None, on_wait=None, max_wait: float | None = None) -> Reply` — role "research" or "chat"; streams when `on_token` (content only); on 507/connection error calls `on_wait(reason)` once per pause and retries with backoff (10→120 s), raising `LocalUnavailable` once total wait exceeds `max_wait` (None = forever). Tool-call `arguments` JSON-decoded; undecodable → `{"_raw": text}`.
  - `async def qwen(messages, priority=WORK, max_tokens=800, on_token=None) -> str` kept as a thin wrapper: `CHAT` → role "chat" with `max_wait=30`, `WORK` → role "research"; returns `content`. `CHAT, WORK` and `parse_json` unchanged.
  - `def set_client_factory(fn)` (like `cloud_llm`), for tests.

- [ ] **Step 1: Failing tests** — `test_routes_roles_to_servers` (chat→CHAT_URL client, research→RESEARCH_URL), `test_thinking_off_unless_asked` (extra_body has `enable_thinking` False; `think=True` → True), `test_tool_calls_decoded` (arguments `'{"field":1}'` → `{"field": 1}`; bad JSON → `{"_raw": ...}`), `test_memory_refusal_waits_and_retries` (fake raises `InternalServerError` 507 twice then succeeds; `on_wait` called once with "memory"; `asyncio.sleep` patched), `test_chat_gives_up_after_max_wait` (raises `LocalUnavailable`), `test_qwen_wrapper_returns_content`.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_local_llm.py -q` → FAIL.
- [ ] **Step 3: Implement.** Model id: first `GET /v1/models` per client, cached.
- [ ] **Step 4: Run** → PASS; full suite may fail where tests patched the old MLX backend — fix those fakes to `set_client_factory` or `monkeypatch` of `qwen`.
- [ ] **Step 5: Commit** `feat: local models over MTPLX (research and chat servers) with memory-pause retries`

### Task 2: Field status line and record rules

**Files:**
- Modify: `backend/app/fields.py` (remove `read_page` and its prompt; keep `norm` import, `_supported`, `_bare_list` deleted if unused)
- Test: `backend/tests/test_fields.py` (rewrite)

**Interfaces:**
- Produces:
  - `def status_line(conn, subtask_id: int) -> str` — `filled N/M: [1] ✓ [2] – [3] not public?` (✓ found or accepted, – open, `not public?` unavailable unreviewed, `not public` unavailable accepted ≥1).
  - `def record(ctx, sub, field_no: int, value: str, unit: str, period: str, quote: str, source: dict) -> str` — source as before (`url,title,key,page,kind,text`); returns `"recorded [n] Name = value unit (source)"` or `"rejected: <reason>"` (unknown field, accepted value, document value vs web, quote not on page / too short / value digits missing). On success: `set_field(status="found", accepted=0, …)` plus `evidence` row (as today).

- [ ] **Step 1: Failing tests** — `test_status_line`, `test_record_ok_and_evidence`, `test_record_rejects_quote_not_on_page`, `test_record_never_overwrites_accepted`, `test_record_web_never_replaces_document`, `test_record_markdown_table_quote` (reuse the table text from the current test).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: field status line and record rules for agent tools`

### Task 3: Agent tools

**Files:**
- Create: `backend/app/tools.py`
- Test: `backend/tests/test_tools.py`

**Interfaces:**
- Consumes: `search.ddg/fetch/order_by_entity/url_key/window`, `explore.fresh` (move `fresh`/`qkey` into `tools.py`; delete `explore.py` in Task 6), `docs.add_found_pdf/keyword_pages/search_terms`, `ctx.store`, `fields.record/status_line`.
- Produces:
  - `TOOLS: list[dict]` — OpenAI tool schemas for `search(query)`, `open_result(n)`, `pdf_contents(doc)`, `pdf_find(doc, terms: list[str])`, `pdf_page(doc, page)`, `record_value(field, value, unit, period, quote)`, `mark_not_public(field, reason)`, `done(note)`.
  - `async def run_tool(ctx, sub, name: str, args: dict) -> tuple[str, int]` — returns (result text ending with `status_line`, pages read). Sub state adds `results: list[Hit]` (last search), `last_page: dict | None` (source incl. `text`), and keeps `urls`, `queries`, `read` (doc key → pages) across loops.
  - Rules: `search` refuses fresh() failures ("already tried: …"); lists ≤8 results `[n] title — url — snippet`. `open_result` fetches; PDF → `add_found_pdf` → "Added as D7 (about…). Use pdf_find/pdf_page."; HTML → window ≤6,000 chars, sets `last_page`, counts 1 page. `pdf_page` refuses pages in `sub["read"][doc]`, clamps range errors to a message, counts 1 page, sets `last_page`. Page budget exhausted → every reading tool returns "Page budget used up; record what you have and call done." Unknown tool/bad args → `"error: …"`.

- [ ] **Step 1: Failing tests** (fakes: `FakeStore` + new `find`, patched `ddg`/`fetch`): `test_search_lists_and_refuses_repeat`, `test_open_result_html_sets_last_page`, `test_open_result_pdf_becomes_document`, `test_pdf_page_refuses_reread_and_bad_range`, `test_pdf_find_ranked`, `test_record_uses_last_page`, `test_bad_args_return_error_text`, `test_page_budget_message`, `test_results_end_with_status_line`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: agent tools with guardrails inside`

### Task 4: Agent loop for one sub-task

**Files:**
- Create: `backend/app/agent.py`
- Test: `backend/tests/test_agent.py`

**Interfaces:**
- Consumes: `local`, `Reply`, `TOOLS`, `run_tool`, `status_line`, `ctx.date_note`.
- Produces:
  - `def opening(ctx, sub) -> list[dict]` — system message: rules (short; examples name real values from this sub-task), `date_note`, numbered fields with entity/period/hint/status (closed fields marked "done, don't work on"), verified documents (key, about, covers, pages).
  - `async def run_loop(ctx, sub, on_round) -> str` — returns why it ended: `"done"`, `"closed"` (every field found or not public), `"steps"` (`MAX_STEPS` tool calls), `"stalled"` (two replies without tool calls). Runs ≤3 tool calls per turn (extra calls get "skipped: max 3 per turn"); after each tool, `on_round(label, pages)`. Before a turn, if the last `prompt_tokens` > `CONTEXT_TOKENS`, compacts to `opening + [notes]` where notes = status table, documents/pages read, searches tried, last `done`/assistant text. Memory pauses: passes `on_wait` that sets the sub-task status `paused_memory` and posts one chat message per pause; status restored after.
- Labels: `search "q"`, `open [3] host`, `D1 p.172`, `D1 find debt`, `recorded Total Revenue = 2,110,127`, `rejected: quote not on page`, `not public: Total Debt`.

- [ ] **Step 1: Failing tests** with a scripted fake `local` (sequence of `Reply`s): `test_tool_calls_run_and_results_appended`, `test_max_three_calls_per_turn`, `test_ends_when_all_fields_closed`, `test_ends_on_step_budget`, `test_text_only_reply_nudged_then_stalled`, `test_compaction_keeps_state_and_refusals` (prompt_tokens 13000 → next request starts with opening + notes; a repeated search still refused), `test_opening_marks_closed_fields`, `test_memory_pause_sets_status_and_posts_once`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: Bonsai agent loop with cached opening, compaction and budgets`

### Task 5: Self-review and the sub-task driver

**Files:**
- Modify: `backend/app/agent.py`
- Test: `backend/tests/test_agent.py`

**Interfaces:**
- Produces:
  - `async def self_review(ctx, sub) -> str` — one `local("research", …, think=True)` call with the fields table (quotes, sources); reply JSON `{"fields":[{"field":N,"action":"accept|reject|retry","hint":"…"}]}`; applies: accept → `accepted=1` (found or unavailable); reject → clear value, reopen (keep/replace hint); retry → reopen with hint (value kept). Returns `"reopened"` if any reject/retry, else `"ok"`; unparseable → `"ok"` (logged).
  - `async def run_subtask(ctx, subtask_id) -> None` — loops `run_loop` → `self_review`; reopened and `restarts < MAX_RESTARTS` → new loop (`loop+1`, `restarts+1`, fresh opening, kept `urls/queries/read`); else end `done` if all fields accepted≥1, else `partial`. Rejected values after the last restart stay cleared. Posts the end line as today.

- [ ] **Step 1: Failing tests:** `test_review_accept_sets_level_1`, `test_review_reject_clears_and_reopens`, `test_restart_cap_ends_partial`, `test_all_accepted_ends_done`, `test_unparseable_review_is_ok`, `test_review_call_has_thinking_on`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: Bonsai self-review and restarts per sub-task`

### Task 6: Final review, consistency checks, orchestration

**Files:**
- Create: `backend/app/final.py`
- Modify: `backend/app/task.py` (rewrite `run_task`; keep `_set/_post/_retry/_report_input/REPORT_PROMPT/switch notice`), `backend/app/docs.py` (delete `visit`, `_pick`, `_ask` stays for verify), delete `backend/app/explore.py`, `backend/app/checkin.py` and their tests
- Test: `backend/tests/test_final.py`, `backend/tests/test_task.py` (rewrite)

**Interfaces:**
- Produces:
  - `IDENTITIES` (name aliases → identity) and `def identity_notes(fields: list[dict]) -> dict[int, str]` — only when every member is found for the same entity and period; units normalised (crore 1e7, lakh 1e5, million 1e6, billion 1e9; `%` excluded); >1% mismatch → note per member field id.
  - `async def final_review(ctx) -> None` — one `cloud("checkin", …)` via `task._retry` over all fields (numbered, with quotes, sources, notes); accept → `accepted=2`; reject → clear value, status `open`, note reason (no reopening); unavailable → status unavailable, `accepted=2`. Failure → nothing changes (logged).
  - `run_task`: global `asyncio.Lock` (`_research_lock`); while waiting set task status `queued`; then plan → `find_document` for planned docs → `agent.run_subtask` sequentially → identity notes (stored in `fields.note`, appended) → `final_review` → report. Report labels: accepted 2 "confirmed", 1 "checked locally", 0 "unreviewed".
- [ ] **Step 1: Failing tests:** `test_identity_only_when_all_members_present`, `test_identity_mismatch_notes_each_member`, `test_units_normalised`, `test_final_review_levels_and_clears`, `test_final_review_failure_keeps_levels`, `test_run_task_end_to_end_with_fakes` (plan → one sub-task → report), `test_second_research_waits_queued`, plus the adapted auth-error / quota-pause / switch-notice tests.
- [ ] **Step 2–4:** FAIL → implement → PASS; full suite PASS.
- [ ] **Step 5: Commit** `feat: research flow on the Bonsai agent with consistency checks and one cloud final review`

### Task 7: Chat, API, UI, docs

**Files:**
- Modify: `backend/app/chat.py` (`_fields` labels by level), `backend/app/main.py` (ACTIVE adds `queued`, `paused_memory`; startup health check of both servers → warning log), `backend/app/config.py` (log retired settings if set), `frontend/src/TaskChecklist.jsx` (+ test), `README.md`, `.env.example`
- Test: `backend/tests/test_chat.py`, `backend/tests/test_api.py`, `frontend/src/TaskChecklist.test.jsx`

**Interfaces:**
- UI: statuses `queued` → "queued — waiting for the current research", `paused_memory` → "paused — Mac memory low or MTPLX not running, retrying"; field state labels: accepted 2 "✓ confirmed", 1 "✓ checked locally", unavailable+accepted≥1 "not public: note", unavailable+0 "not public? (unreviewed)", found+0 "unreviewed", open "searching".
- README: MTPLX setup (two `mtplx serve` commands from the spec), how research works now, settings, expected times; roadmap keeps Qwen-on-Mac optimisation and OCR redesign.

- [ ] **Step 1: Failing tests:** `test_fields_context_levels`, `test_startup_marks_queued_and_paused_interrupted`, UI `shows confirmed / checked locally labels`, `shows queued and paused_memory notes`.
- [ ] **Step 2–4:** FAIL → implement → PASS; `npm run build` OK.
- [ ] **Step 5: Commit** `feat: chat, API and UI for the Bonsai harness; README for MTPLX`

### Task 8: Live check

- [ ] **Step 1:** Start both MTPLX servers (README commands) and a scratch backend on 8766 (`DR_DB` scratch). Run `task: make an investability report for debt bonds of bharti telecom`.
- [ ] **Step 2:** Pass when the log shows: tool calls with readable labels, at least one `D<n> p.<page>` read from a verified document, values recorded with quotes, a compaction if the conversation passed 12K tokens, self-review, one cloud final review, a report, and no failures. Record total time, pages read, cloud calls, peak memory in the commit message of any fixes.
