# Deep Research Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A chat web app where a local Qwen3.5-4B answers, searches, or runs multi-step research tasks, with free-tier cloud models planning, judging and writing cited reports.

**Architecture:** A FastAPI backend owns everything. It holds one MLX Qwen instance behind a priority queue (chat first, then background work FIFO), cloud models behind role-based fallback lists with daily quota counting, the search, fetch and PDF pipeline, the task engine, and SQLite persistence. It pushes live progress to a React + Vite frontend over Server-Sent Events.

**Tech Stack:** Python 3.13, FastAPI, sse-starlette, mlx-lm, ddgs, trafilatura, requests, indra-ocr (+ pypdfium2, its dependency), openai SDK (OpenAI-compatible endpoints for Gemini and Groq), sqlite3, pytest. Frontend: React 18, Vite, react-markdown, vitest.

**Spec:** `docs/superpowers/specs/2026-11-24-deep-research-agent-design.md`

## Global Constraints

- Local model `mlx-community/Qwen3.5-4B-4bit` via `mlx_lm.load` (verified to load and generate). Always build prompts with `tokenizer.apply_chat_template(..., add_generation_prompt=True, tokenize=False, enable_thinking=False)`.
- Cloud role order (first = primary, then fallbacks):
  - `plan` / `report`: `gemini-3.5-flash-lite` → `qwen/qwen3.8-27b`
  - `checkin`: `qwen/qwen3.8-27b` → `gemini-3.1-flash-lite`
  - `pdf`: `gemini-3.1-flash-lite` → `gemini-3.5-flash-lite`
- Daily request limits: `gemini-3.5-flash-lite` 500, `gemini-3.1-flash-lite` 500, `qwen/qwen3.8-27b` 1000. The quota day is the calendar date in `America/Los_Angeles`.
- **Never use `gpt-oss-120b`** (or any gpt-oss model).
- Endpoints: Gemini `https://generativelanguage.googleapis.com/v1beta/openai/` with `GEMINI_API_KEY`; Groq `https://api.groq.com/openai/v1` with `GROQ_API_KEY`. Keys come from `.env`.
- Limits, configurable via `.env` (defaults in brackets): `DR_MAX_SUBTASKS` [8] sub-tasks per plan, `DR_ROUNDS_PER_LOOP` [20] rounds per loop, `DR_MAX_RESTARTS` [2] restarts per sub-task. Check-ins happen every 5 rounds (fixed) and also at the last round of a loop. Hitting any cap means status `partial`. Invalid or non-positive values fall back to the default with a logged warning.
- Search: `ddgs` text search with `max_results=8`; fetch the top 5; fetch timeout 15 s; max 2 MB per response.
- Web evidence passages must be verbatim substrings of the fetched page text. Each one is stored with URL, title, published date (or null) and fetched_at.
- A figure without a published date is cited as "accessed <fetched date>, publish date unknown" and flagged as lower confidence.
- Demo login: `analyst@demo` / `password`.
- Palette: dark base `#161514`, light base `#fafaf8`, the only accent is orange `#e8650f`, used only for actions and running states. No other accent colors.
- The PDF module stays behind `read_pdf(url, objective, subtask_id, loop, conn) -> list[Evidence]` so it can be replaced later (spec "Future work").
- Public information only; nothing finance-specific in prompts.

## Review Focus

1. **Qwen returns malformed or non-JSON output.** Each call site falls back to a safe default: routing → `answer`; rerank → original order with all hits kept; rewrite → previous query; passages → none. Covered in Tasks 2, 4, 6 and 7.
2. **DuckDuckGo errors or returns nothing, or every fetch fails.** The round is logged with zero hits, the query is rewritten, and the loop continues without crashing (Task 4, Task 6).
3. **Pages that are huge, binary, time out or return non-200.** These are skipped. A PDF Content-Type or a `.pdf` URL goes to the PDF path (Task 4).
4. **Server restarts mid-task.** On startup, tasks and sub-tasks left `running` or `queued` become `interrupted`. The UI shows this from the DB with no live events (Task 8).
5. **Every model in a role is out of quota.** The sub-task shows `paused_quota`, retries every 15 minutes, and resumes. It is not marked failed (Task 3, Task 6).

---

## File Structure

```
deepResearch/
  .env.example  .gitignore  requirements.txt
  backend/
    app/
      config.py      # env, model ids, limits (constants above)
      db.py          # sqlite schema + small query helpers
      local_llm.py   # Qwen MLX wrapper, priority queue, parse_json
      cloud_llm.py   # role fallback, quota counting, QuotaExhausted
      search.py      # ddgs + fetch + rerank + verbatim passages
      pdf.py         # read_pdf: contents triage, page read, extraction
      task.py        # plan, sub-task loop, check-ins, report
      chat.py        # routing, search replies, task summary, follow-ups
      events.py      # per-conversation SSE fan-out
      main.py        # FastAPI routes
    tests/           # one test file per module, fakes for all models
  frontend/
    index.html  package.json  vite.config.js
    src/ main.jsx App.jsx api.js theme.css
         Login.jsx Sidebar.jsx Chat.jsx Message.jsx TaskChecklist.jsx ReportBlock.jsx ThemeToggle.jsx
         App.test.jsx
```

Shared types (defined in `db.py` as dataclasses, used everywhere):

```python
@dataclass
class Hit:            # one search result
    title: str; url: str; snippet: str
    text: str = ""; published: str | None = None; fetched_at: str = ""; is_pdf: bool = False

@dataclass
class Evidence:       # one stored piece of evidence
    subtask_id: int; loop: int; url: str; title: str
    passage: str; published: str | None; fetched_at: str
    page: int | None = None; off_target: bool = False
```

SSE event payloads (`events.publish(conv_id, event: dict)`), all with `"type"`:
- `{"type":"message","message":{id,role,content,kind,task_id}}`. `kind` is one of `text|search_results|report|progress`.
- `{"type":"task","task":{id,status,objective}}`
- `{"type":"subtask","subtask":{id,task_id,objective,status,round,loop,restarts}}`. `status` is one of `queued|running|checking_in|restarted|done|partial|paused_quota|interrupted|failed`.
- `{"type":"round","round":{subtask_id,loop,n,query,hits:int}}`

---

### Task 1: Project scaffold, config, database

**Files:**
- Create: `.gitignore`, `.env.example`, `requirements.txt`, `backend/app/__init__.py`, `backend/app/config.py`, `backend/app/db.py`, `backend/tests/conftest.py`
- Test: `backend/tests/test_db.py`

**Interfaces:**
- Produces: `config` module constants: `LOCAL_MODEL`, `ROLES: dict[str, list[str]]`, `DAILY_LIMITS: dict[str,int]`, `PROVIDER: dict[str,str]` (model → `"gemini"|"groq"`), `MAX_SUBTASKS` (env `DR_MAX_SUBTASKS`, default 8), `CHECKIN_EVERY=5`, `ROUNDS_PER_LOOP` (env `DR_ROUNDS_PER_LOOP`, default 20), `MAX_RESTARTS` (env `DR_MAX_RESTARTS`, default 2), `SEARCH_MAX_RESULTS=8`, `FETCH_TOP=5`, `FETCH_TIMEOUT=15`, `FETCH_MAX_BYTES=2_000_000`, `DEMO_USER`, `DEMO_PASSWORD`, `DB_PATH` (env `DR_DB`, default `data/dr.sqlite`).
- Produces: `db.connect(path: str | None = None) -> sqlite3.Connection` (row_factory=Row, creates schema), the `Hit` and `Evidence` dataclasses, and helpers `insert(conn, table, **cols) -> int` and `update(conn, table, id, **cols) -> None`.
- Tables:
  - `conversations(id, title, created_at)`
  - `messages(id, conversation_id, role, content, kind, task_id, created_at)`
  - `tasks(id, conversation_id, objective, status, report, created_at)`
  - `subtasks(id, task_id, objective, query, needs_document, status, round, loop, restarts)`
  - `rounds(id, subtask_id, loop, n, query, hits)`
  - `evidence(id, subtask_id, loop, url, title, passage, published, fetched_at, page, off_target)`
  - `quota(day, model, used, PRIMARY KEY(day, model))`

- [ ] **Step 1:** `git init`. Write `.gitignore` covering `.env`, `data/`, `node_modules/`, `dist/`, `.superpowers/`, `__pycache__/`. Write `.env.example`, and a `.env` copied from it, containing `GEMINI_API_KEY=replace-me-gemini`, `GROQ_API_KEY=replace-me-groq`, `DR_MAX_SUBTASKS=8`, `DR_ROUNDS_PER_LOOP=20` and `DR_MAX_RESTARTS=2`. The keys are placeholders that the user fills in later. Write `requirements.txt` listing the backend packages from Tech Stack (all already installed).
- [ ] **Step 2: Write failing tests** in `test_db.py`:
  - `test_schema_created`: `connect(":memory:")` has all 7 tables.
  - `test_insert_update`: `insert` returns an id, and `update(conn,"tasks",id,status="done")` persists.
  - `test_config_never_gpt_oss`: no model in `ROLES` values contains `"gpt-oss"`.
  - `test_roles_order`: `ROLES["checkin"] == ["qwen/qwen3.8-27b","gemini-3.1-flash-lite"]`.
  - `test_limits_from_env`: with `DR_MAX_SUBTASKS=3`, `DR_ROUNDS_PER_LOOP=10` and `DR_MAX_RESTARTS=1` set, reloading `config` gives 3, 10 and 1. With `DR_MAX_SUBTASKS=abc` or `0`, it gives the default 8.
- [ ] **Step 3:** Run `cd backend && pytest tests/test_db.py -v`. Expected: FAIL (module missing).
- [ ] **Step 4:** Implement `config.py` (load `.env` with `python-dotenv` from the repo root) and `db.py`.
- [ ] **Step 5:** Run `pytest tests/test_db.py -v`. Expected: PASS.
- [ ] **Step 6:** Commit with `git add -A && git commit -m "feat: scaffold backend config and sqlite schema"`.

### Task 2: Local Qwen with priority queue

**Files:**
- Create: `backend/app/local_llm.py`
- Test: `backend/tests/test_local_llm.py`

**Interfaces:**
- Produces:
  - `CHAT = 0`, `WORK = 1`
  - `async def qwen(messages: list[dict], priority: int = WORK, max_tokens: int = 800) -> str`
  - `def parse_json(text: str) -> dict | None`
  - `def set_backend(fn: Callable[[list[dict], int], str]) -> None` (tests inject a fake; default is MLX)
- Approach: one `asyncio.PriorityQueue` keyed by `(priority, itertools.count())`. A single worker coroutine pops jobs and runs the blocking generate in `asyncio.to_thread`, one job at a time. The MLX model loads lazily on the first call. FIFO within `WORK` gives natural round-robin across sub-tasks, because each sub-task awaits one generation before queuing the next.
- `parse_json`: strip `<think>…</think>` blocks and code fences, then `json.loads` the first balanced `{…}`. Return `None` on failure.

- [ ] **Step 1: Write failing tests:**
  - `test_chat_jumps_queue`: with a fake backend that sleeps 0.05 s and records order, queue 3 `WORK` jobs, then 1 `CHAT` job while the first runs. Assert the order is `[work1, chat, work2, work3]`.
  - `test_parse_json`:
    - `parse_json('<think>x</think>```json\n{"a":1}\n```') == {"a":1}`
    - `parse_json('ok then {"a":{"b":2}} trailing') == {"a":{"b":2}}`
    - `parse_json("no json") is None`
- [ ] **Step 2:** Run `pytest tests/test_local_llm.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `local_llm.py` as described.
- [ ] **Step 4:** Run `pytest tests/test_local_llm.py -v`. Expected: PASS. Then run the real-model smoke `python -c "import asyncio;from app.local_llm import qwen;print(asyncio.run(qwen([{'role':'user','content':'say ok'}])))"`, which should print text containing "ok".
- [ ] **Step 5:** Commit with `feat: local qwen with chat-priority queue`.

### Task 3: Cloud models with fallback and quota

**Files:**
- Create: `backend/app/cloud_llm.py`
- Test: `backend/tests/test_cloud_llm.py`

**Interfaces:**
- Consumes: `config.ROLES`, `DAILY_LIMITS`, `PROVIDER`, and `db.connect`.
- Produces:
  - `class QuotaExhausted(Exception)`
  - `async def cloud(role: str, messages: list[dict], conn: sqlite3.Connection) -> str`
  - `def set_client_factory(fn: Callable[[str], Any]) -> None` (tests inject fakes; default builds `openai.AsyncOpenAI(base_url=…, api_key=…)` per provider)
- Behavior: for each model in `ROLES[role]`, skip it if `quota.used >= DAILY_LIMITS[model]` for today (LA date). Otherwise increment `used` and call `chat.completions.create(model=…, messages=…)`. On `openai.RateLimitError`, mark that model's `used = limit` for today and try the next one. If none are left, raise `QuotaExhausted`. Other errors propagate.

- [ ] **Step 1: Write failing tests** (fake client returns `"from:<model>"` or raises a RateLimitError per test):
  - `test_primary_used`: `cloud("checkin", …)` returns `"from:qwen/qwen3.8-27b"` and quota used = 1.
  - `test_fallback_on_429`: when the primary raises, the result comes from `gemini-3.1-flash-lite`, and the primary's `used` equals 1000.
  - `test_exhausted`: with both models at their limit, it raises `QuotaExhausted` without calling the client.
  - `test_quota_day_is_la`: with time patched to `2026-11-25T07:30Z`, the quota row day is `2026-11-24`.
- [ ] **Step 2:** Run `pytest tests/test_cloud_llm.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement as described.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit with `feat: cloud model roles with fallback and daily quota`.

### Task 4: Search, fetch, rerank, verbatim passages

**Files:**
- Create: `backend/app/search.py`
- Test: `backend/tests/test_search.py`

**Interfaces:**
- Consumes: `qwen`, `parse_json`, `Hit`, and the config fetch limits.
- Produces:
  - `def ddg(query: str) -> list[Hit]`: `DDGS().text(query, max_results=8)`, mapping `title/href/body`. Return `[]` on any exception.
  - `def fetch(hit: Hit) -> Hit | None`: `requests.get(stream=True, timeout=15)`. Read at most 2 MB; anything larger is cut off and its text truncated. Return `None` on non-200, a timeout or other exception, or empty `trafilatura.extract` text. If the Content-Type contains `pdf` or the URL ends in `.pdf`, return the hit with `is_pdf=True` and no text. For HTML, use `trafilatura.extract(html, with_metadata=True, output_format="json")` to get `text` and `date`. `fetched_at` is the UTC ISO date.
  - `async def search(query: str, objective: str, priority: int = WORK) -> list[Hit]`: `ddg`, then `fetch` the top 5 concurrently (`asyncio.to_thread`), then rerank with one listwise Qwen call. That call returns `{"keep":[indices in best-first order]}` and is shown the title, URL and first 800 chars of each hit. If `parse_json` fails, keep all hits in their original order. PDFs pass through the rerank like any other hit.
  - `async def passages(hit: Hit, objective: str) -> list[str]`: Qwen sees the first 6000 chars of `hit.text` and returns `{"passages":[…]}`. Keep only strings that are substrings of `hit.text` after whitespace normalization. Return `[]` on a parse failure.

- [ ] **Step 1: Write failing tests** (monkeypatch `DDGS`, `requests.get` and the Qwen backend):
  - `test_ddg_error_returns_empty`
  - `test_fetch_skips_non_200_and_timeout`
  - `test_fetch_marks_pdf`: Content-Type `application/pdf` gives `is_pdf=True`.
  - `test_fetch_truncates_large`: a 5 MB body gives text from at most 2 MB.
  - `test_rerank_order`: a fake Qwen returning `{"keep":[2,0]}` gives hits `[h2,h0]`.
  - `test_rerank_bad_json_keeps_all`
  - `test_passages_must_be_verbatim`: a fake returns one real substring and one invented sentence; only the real one is kept.
- [ ] **Step 2:** Run `pytest tests/test_search.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement as described.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit with `feat: ddg search with qwen rerank and verbatim passages`.

### Task 5: PDF reader (isolated, replaceable)

**Files:**
- Create: `backend/app/pdf.py`
- Test: `backend/tests/test_pdf.py`

**Interfaces:**
- Consumes: `cloud(role="pdf", …)`, `parse_json`, and `Evidence`.
- Produces: `async def read_pdf(url: str, objective: str, subtask_id: int, loop: int, conn) -> list[Evidence]`. This module's docstring states it is the swap point for the future OCR/retrieval redesign.
- Steps inside:
  1. Download to a temp file (same timeout; cap 50 MB).
  2. `conv = indra_ocr.load(mode="auto")`. It uses the text layer when the layer is trustworthy and OCR otherwise. Open the file with `pypdfium2.PdfDocument`, then read pages with `conv.pdf_page(path, doc, i).markdown`.
  3. Send the markdown of the first 8 pages plus the page count to the `pdf` role, asking for `{"pages":[[start,end],…]}` (1-based, inclusive).
  4. Clamp the ranges to the document and read at most 30 pages in total.
  5. Send the pages, tagged `[page N]`, to the `pdf` role, asking for `{"facts":[{"fact","page","period"}]}`.
  6. Map each fact to an `Evidence`: `passage=fact`, `page=page`, `published=period or None`, `fetched_at` = today.

- [ ] **Step 1: Write failing tests.** Build a 12-page PDF in the test with PyMuPDF (page 1 is "Contents … Results p.9", and page 9 holds "Revenue 120"). Serve it with a monkeypatched `requests.get`, and use a fake `cloud`.
  - `test_reads_only_triaged_pages`: when triage returns `[[9,9]]`, the extraction prompt contains "[page 9]" and no "[page 3]".
  - `test_page_cap`: when triage returns `[[1,500]]`, at most 30 pages are read.
  - `test_facts_to_evidence`: the extracted fact gives `Evidence(page=9, url=url)`.
  - `test_bad_triage_json_returns_empty`
- [ ] **Step 2:** Run `pytest tests/test_pdf.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement as described.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit with `feat: pdf contents triage and page extraction`.

### Task 6: Task engine

**Files:**
- Create: `backend/app/task.py`, `backend/app/events.py`
- Test: `backend/tests/test_task.py`

**Interfaces:**
- Consumes: `qwen`, `cloud`, `QuotaExhausted`, `search`, `passages`, `read_pdf`, and `db`.
- Produces:
  - `events.subscribe(conv_id) -> asyncio.Queue`, `events.unsubscribe(conv_id, q)`, `events.publish(conv_id, event: dict) -> None`
  - `async def run_task(conn, conv_id: int, task_id: int) -> str`: returns the report markdown and sets task status `done`.
  - `async def run_subtask(conn, conv_id: int, subtask_id: int) -> None`
  - `QUOTA_RETRY_SECONDS = 900` (module constant, patched in tests)
- Flow:
  - `run_task`:
    1. The `plan` role turns the objective into `{"subtasks":[{"objective","query","needs_document"}]}`. Truncate to `MAX_SUBTASKS` and insert the sub-tasks as `queued`.
    2. Run all sub-tasks concurrently with `asyncio.gather`. Interleaving happens naturally through the Qwen queue.
    3. Call the `report` role with the objective plus every non-off-target evidence row, grouped by sub-task, each with url/title/page/published/fetched_at. Partial sub-tasks are listed as gaps.
       The prompt requires every figure or claim to cite its source (URL, or PDF + page) and date, to use only figures present in the evidence, and to flag undated evidence as "accessed <fetched_at>, publish date unknown" (lower confidence).
    4. Store the report and publish a `message` with `kind="report"`.
  - `run_subtask` loop:
    - Each round runs `search` then, for each hit:
      - a PDF hit whose URL hasn't been seen in this task goes to `read_pdf`;
      - any other hit goes to `passages`, and each passage is inserted as evidence.
    - Insert the `rounds` row and publish a `round` event.
    - Qwen rewrites the query with `{"query"}`, keeping the old query if the JSON is bad.
    - If `needs_document` is set, the first round's query is `"<needs_document> pdf"`.
    - Every 5 rounds, and at round `ROUNDS_PER_LOOP`, send the `checkin` role the objective, the queries so far and up to 40 evidence passages. It returns `{"decision":"continue|sufficient|restart","query","objective","off_target_loops":[…]}`.
      - `sufficient` gives status `done`.
      - `restart` marks evidence in `off_target_loops` as off-target, increments `restarts` and `loop`, resets the round, and applies the new query and objective. Once `restarts > MAX_RESTARTS`, the status becomes `partial`.
      - A loop that reaches `ROUNDS_PER_LOOP` rounds without `sufficient` counts as a restart.
    - Any other exception from a cloud call (bad or placeholder API key, network error) sets the task to `failed` and posts a `progress` message such as "Research stopped: Gemini/Groq call failed (<error class>). Check API keys in .env." The server does not crash.
    - On `QuotaExhausted` anywhere, set `paused_quota`, publish, sleep `QUOTA_RETRY_SECONDS`, and retry that same step.
    - Every status change publishes a `subtask` event. Qwen posts a one-line `progress` message when a sub-task ends.
- The SQLite connection is shared within a single event loop, so no thread uses it.

- [ ] **Step 1: Write failing tests** (fake `search`, `passages`, Qwen and `cloud`, all scripted per test):
  - `test_plan_capped_at_8`: the planner returns 11 sub-tasks and 8 rows are inserted.
  - `test_limits_respected`: with `ROUNDS_PER_LOOP=7` and `MAX_RESTARTS=0` patched, and check-in always saying continue, there are check-ins at rounds 5 and 7, then status `partial` with 7 rounds.
  - `test_auth_error_fails_task`: when `cloud` raises `openai.AuthenticationError`, the task status is `failed` and a progress message mentions `.env`.
  - `test_sufficient_stops_at_round_5`: the first check-in says sufficient, giving status `done` and 5 rounds.
  - `test_continue_forever_ends_partial`: with the defaults, check-in always says continue, giving status `partial`, 3 loops and 60 rounds in total.
  - `test_restart_marks_off_target`: a restart with `off_target_loops:[0]` sets loop-0 evidence `off_target=1`, and the report prompt excludes it.
  - `test_quota_pause_resumes`: the checkin role raises `QuotaExhausted` once (retry patched to 0). A `paused_quota` event is published, then the sub-task completes.
  - `test_zero_hits_round_continues`: when search returns `[]`, the round is recorded with `hits=0` and the loop keeps going.
  - `test_pdf_hit_routed_once`: the same PDF URL in two rounds gives one `read_pdf` call.
- [ ] **Step 2:** Run `pytest tests/test_task.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `events.py` and `task.py`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit with `feat: task engine with check-ins, restarts and quota pause`.

### Task 7: Chat router and Qwen replies

**Files:**
- Create: `backend/app/chat.py`
- Test: `backend/tests/test_chat.py`

**Interfaces:**
- Consumes: `qwen(…, priority=CHAT)`, `search`, `run_task`, `db`, and `events`.
- Produces: `async def handle_message(conn, conv_id: int, text: str) -> None`. It stores the user message, then:
  - **Routing:** the `search:` or `task:` prefix forces a mode (the prefix is stripped). Otherwise Qwen sees the last 10 messages and returns `{"action":"answer|search|task","query"}`. Bad JSON means `answer`.
  - **answer:** Qwen replies using the conversation's last 10 messages plus up to 20 evidence passages from this conversation's tasks that match the question by keyword overlap. Each passage carries its URL.
  - **search:** `search(query, query, CHAT)`. Store a `search_results` message whose content is JSON `[{title,url,snippet}]`, followed by a 2–3 sentence Qwen text reply.
  - **task:** insert the task, post a Qwen `progress` message ("Planning…"), and launch `run_task` as a background `asyncio.create_task` so the call returns at once. When it finishes, Qwen writes a `text` summary (bottom line, key numbers, main risks) from the report.
  - Every stored message is also published as a `message` event. The first user message sets the conversation title (its first 60 chars).

- [ ] **Step 1: Write failing tests:**
  - `test_prefix_forces_search`: `"search: x"` runs search without calling the router.
  - `test_bad_router_json_answers`
  - `test_task_returns_immediately`: `handle_message` returns while a fake `run_task` is still blocked on an Event.
  - `test_summary_after_report`: after the fake `run_task` resolves, the last message is a `text` from Qwen and the one before it is the `report`.
  - `test_title_set_from_first_message`
- [ ] **Step 2:** Run `pytest tests/test_chat.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement as described.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit with `feat: qwen chat router, search replies and task summaries`.

### Task 8: HTTP API and SSE

**Files:**
- Create: `backend/app/main.py`
- Test: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: everything above.
- Routes:
  - `POST /api/login {email,password}` returns `200 {"token":"demo"}` or `401`. Add the comment `# shortcut: demo creds, no token check on other routes; add real auth before exposing beyond localhost`.
  - `GET /api/conversations` returns `[{id,title,created_at}]`, newest first.
  - `POST /api/conversations` returns `{id}`.
  - `GET /api/conversations/{id}` returns `{id,title,messages:[…],tasks:[{…,subtasks:[…],rounds:[…]}]}`, so the UI can rebuild everything after a reload.
  - `POST /api/conversations/{id}/messages {text}` returns `202` and calls `handle_message` in the background. An empty or whitespace text returns `400`.
  - `GET /api/conversations/{id}/events` is an SSE stream (`sse_starlette.EventSourceResponse`) of `events.subscribe` payloads as JSON, with a keepalive ping every 15 s.
- On startup: open the DB, then run `UPDATE tasks/subtasks SET status='interrupted' WHERE status IN ('running','queued','checking_in','paused_quota')`.
- Bind to `127.0.0.1:8000`; CORS for `http://localhost:5173`.

- [ ] **Step 1: Write failing tests** with `fastapi.testclient.TestClient` and a stubbed `handle_message`:
  - `test_login_ok_and_bad`
  - `test_empty_message_400`
  - `test_conversation_roundtrip`: create a conversation, post a message (202), and `GET` shows the stored user message.
  - `test_startup_marks_interrupted`: seed a `running` sub-task, start the app, and its status is `interrupted`.
- [ ] **Step 2:** Run `pytest tests/test_api.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement as described.
- [ ] **Step 4:** Run `pytest -v` (the whole suite). Expected: all PASS.
- [ ] **Step 5:** Commit with `feat: http api and sse stream`.

### Task 9: Frontend shell (login, themes, layout)

**Files:**
- Create: `frontend/` (Vite React scaffold), `src/theme.css`, `src/api.js`, `src/App.jsx`, `src/Login.jsx`, `src/ThemeToggle.jsx`, `src/Sidebar.jsx`
- Test: `frontend/src/App.test.jsx`

**Interfaces:**
- Produces in `api.js`: `login(email,password) -> Promise<boolean>`, `listConversations()`, `createConversation()`, `getConversation(id)`, `sendMessage(id,text)`, and `subscribe(id, onEvent) -> () => void` (an EventSource wrapper). The base URL is `http://127.0.0.1:8000/api`.
- Theme: CSS variables on `:root[data-theme=dark|light]`, using the palette from the mockup `.superpowers/brainstorm/*/content/look-v2.html`.
  - Dark: `--bg #161514`, `--surface #1f1e1c`, `--raised #2a2826`, `--line #34312e`, `--text #ece9e4`, `--muted #8f8a83`.
  - Light: `--bg #fafaf8`, `--surface #f1f0ed`, `--raised #ffffff`, `--line #e3e1dc`, `--text #161514`, `--muted #8a8680`.
  - Both: `--accent #e8650f`.
  - The default follows `prefers-color-scheme`. The toggle saves the choice in `localStorage` (wrapped in try/catch).
- Login: centered card with the "deep.research" wordmark (orange dot), email, password and a "Sign in" button, plus the hint line `demo: analyst@demo / password`. Show an error line on 401. `sessionStorage` remembers the login.

- [ ] **Step 1:** `npm create vite@latest frontend -- --template react`, then `npm i react-markdown` and `npm i -D vitest @testing-library/react jsdom`.
- [ ] **Step 2: Write failing tests:**
  - `shows login when signed out`
  - `theme toggle flips data-theme`: clicking the toggle changes `document.documentElement.dataset.theme` from `dark` to `light`.
- [ ] **Step 3:** Run `npx vitest run`. Expected: FAIL.
- [ ] **Step 4:** Implement the components listed.
- [ ] **Step 5:** Run `npx vitest run && npm run build`. Expected: PASS, and the build succeeds.
- [ ] **Step 6:** Commit with `feat: frontend shell with login and light/dark themes`.

### Task 10: Chat screen, live checklist, report block

**Files:**
- Create: `src/Chat.jsx`, `src/Message.jsx`, `src/TaskChecklist.jsx`, `src/ReportBlock.jsx`
- Modify: `src/App.jsx`, `src/App.test.jsx`

**Interfaces:**
- Consumes: `api.js` and the SSE event payloads defined above.
- Chat:
  - Load the conversation with `getConversation`, then apply SSE events on top (upsert by id).
  - Show user bubbles on the right and assistant messages on the left, with markdown rendered by react-markdown.
  - `search_results` render as a numbered list of title links with the domain and snippet.
  - `progress` messages render as muted single lines.
  - The input sits at the bottom. Enter sends and Shift+Enter adds a newline. It's disabled only while the POST is in flight, never while a task runs.
- TaskChecklist (one per task, attached under the message that started it):
  - Each row shows a dot (orange = running or checking_in, grey = done, hollow = queued, muted-orange outline = paused_quota or partial or interrupted), the objective, and a tag ("round n · check-in at 5k", "done · n rounds", "partial", "paused — quota", "interrupted", "failed").
  - Clicking a row expands its log: the round list with the query and hit count.
- ReportBlock: collapsed card showing the title and first lines, with "Expand" and "Download .md" buttons. Download builds a `Blob` client-side, with the filename taken from the task objective.

- [ ] **Step 1: Write failing tests** with mocked `api.js`:
  - `renders subtask status from event`: dispatching a `subtask` event with `status:"running", round:3` shows "round 3".
  - `report download builds markdown blob`: clicking "Download .md" calls `URL.createObjectURL` with a Blob of type `text/markdown`.
  - `input stays enabled while task runs`
- [ ] **Step 2:** Run `npx vitest run`. Expected: FAIL.
- [ ] **Step 3:** Implement the components listed.
- [ ] **Step 4:** Run `npx vitest run && npm run build`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat: chat screen with live task checklist and report block`.

### Task 11: End-to-end run with real models

**Files:**
- Create: `README.md` (setup: `.env` keys; `uvicorn app.main:app --app-dir backend`; `npm run dev` in `frontend`)

- [ ] **Step 1:** The user fills `.env` with real keys later. Until then, run only the key-free checks: login, themes, Search mode, and a Task, which should end `failed` with the `.env` hint. Once keys are in: Verify the Gemini model ids by running `python -c "from openai import OpenAI;…models.list()"` against the Gemini endpoint. If `gemini-3.5-flash-lite` or `gemini-3.1-flash-lite` are named differently, fix `config.ROLES` and `DAILY_LIMITS`. Do the same for Groq's `qwen/qwen3.8-27b`.
- [ ] **Step 2:** Start the backend and frontend. In the browser, log in and toggle the theme.
  - Search: "latest RBI repo rate decision". Ranked results appear with a short Qwen reply.
  - Task: "Brief report on the current state of solid-state battery commercialization". The checklist fills in, rounds tick up, check-ins happen at round 5, and a cited report plus a Qwen summary appear.
  - While that task runs, send "what is 2+2?" and check that the reply comes in within seconds.
- [ ] **Step 3:** Reload the page mid-task and check that the state is rebuilt. Then restart the backend mid-task and check that its sub-tasks show "interrupted".
- [ ] **Step 4:** Check the report: every figure has a URL or PDF page and a date (or an "accessed …, publish date unknown" flag).
- [ ] **Step 5:** Commit with `docs: readme and verified model ids`.
