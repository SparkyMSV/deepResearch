# Deep Research Agent: Design

Date: 2026-11-24 · Status: approved 2026-11-24

## Intent

A general-purpose, local-first deep research chatbot for public information on any topic. You talk to a small local model (Qwen3.5-4B on MLX). It answers directly, runs a quick **search**, or kicks off a long **task**. One example of a task: "make a report on Company X's credit rating to judge whether its debt bonds are investable". Tasks can be about anything; nothing is specific to finance. Larger free-tier cloud models do the planning, the judging and the long-form writing. The local model does all the searching, reranking and query refinement, which is the high-volume work, at zero cost.

Reports should be trustworthy: every figure or factual claim in a report cites its source (URL, or PDF + page) and the date of that source.

## Goals

- A chat UI where Qwen3.5-4B is the assistant that starts work and summarizes results.
- **Search**: DuckDuckGo → fetch clean page text → Qwen reranks → ranked results with title, source and snippet.
- **Task**: the larger LLM plans independent sub-tasks → each sub-task runs an iterative search loop on Qwen → the larger LLM checks in every 5 rounds → the larger LLM writes the final report → Qwen posts a chat summary next to the report.
- Long PDFs (annual reports, papers, filings, etc.): read only the sections the objective needs, chosen from the contents page.
- Runs entirely on free tiers plus the local Mac.

## Non-goals (for now)

- Real user accounts or auth. The login uses one hard-coded demo account.
- Sharing reports, or exports other than Markdown.
- three.js or any 3D visuals.
- Paid search or fetch APIs.

## Anti-goals (a failure even if it "works")

- Report numbers without a source and date, or numbers the model made up.
- The chat freezing while background research runs.
- One stuck sub-task burning the whole day's free quota.
- Neon or accent-heavy UI.

## Constraints and what's already on the machine

- macOS, Python 3.13 (miniconda), Node 26.
- Local model: `mlx-community/Qwen3.5-4B-4bit`, cached in the HF hub. The snapshot ships a preprocessor config, so it may be a vision-language checkpoint. **The builder must verify that it loads with `mlx_lm.load`; if not, use `mlx_vlm` in text-only mode.**
- Installed Python packages to reuse (no new deps for these): `ddgs` (DuckDuckGo), `trafilatura` (clean page text), `requests`, `PyMuPDF` + `pymupdf4llm` (PDF text and page ranges), `indra-ocr` 1.1.2 (OCR for scanned pages), `mlx-lm`/`mlx-vlm`, `google-genai`, `groq`, `openai`.
- One MLX model instance serves one generation at a time.
- Free-tier quotas: Gemini 3.5 Flash-Lite and Gemini 3.1 Flash-Lite at ~500 requests/day each; Groq `qwen/qwen3.8-27b` at ~1,000 requests/day. The tool is for public information only, so Gemini's free-tier data terms are acceptable.
- `gpt-oss-120b` is explicitly **not** to be used (user decision).

## Approaches considered

1. **Python CLI only.** The smallest option, but the user wants a proper web UI.
2. **Python backend + React/Vite web UI (chosen).** FastAPI owns the models and the queue and streams progress to a React chat UI.
3. **Agent framework (LangGraph/LlamaIndex).** Rejected: the loop is simple, and a framework hides it and makes 4B-model drift harder to debug.

Node backend: rejected, because MLX runs only in Python and Node would just proxy to it. Node is used only for the Vite/React frontend tooling.

## Decided by the user

### Models and roles

| Job | Primary | Fallback |
|---|---|---|
| Chat, intent routing, query rewrite, rerank, chat summaries | Qwen3.5-4B (local MLX) | none |
| Plan sub-tasks; write the final report | Gemini 3.5 Flash-Lite | Groq Qwen3.8-27B |
| 5-round check-in (continue / stop-sufficient / restart with new query or objective) | Groq Qwen3.8-27B | Gemini 3.1 Flash-Lite |
| PDF contents-page triage; extract figures from the chosen pages | Gemini 3.1 Flash-Lite | Gemini 3.5 Flash-Lite |

On a rate-limit error (HTTP 429 or the provider's quota error), the call moves to the next model in its list. API keys live in `.env`.

### Chat flow

- Every user message goes to Qwen. Qwen decides: answer directly, run a search, or start a task. There's no mode toggle; a prefix such as `search:` / `task:` forces a mode.
- During a task, Qwen posts short progress notes in the chat ("ratings sub-task done; moving to leverage…") alongside a live checklist.
- On completion: the full report (written by the larger LLM) appears as an expandable, downloadable Markdown block, and Qwen writes a short chat summary of the bottom line, key numbers and main risks.
- Follow-up questions go to Qwen, which answers from the sources already gathered or runs a new search.

### Task loop (per sub-task)

1. Qwen gets the sub-task objective and forms a query.
2. Each round: DuckDuckGo search → fetch the top pages with `requests` + `trafilatura` → Qwen reranks and keeps the useful evidence → Qwen rewrites the query to get closer to the objective.
3. Every 5 rounds, report to the check-in model with the objective, the queries tried and the evidence summary. It returns one of: `continue`, `sufficient` (stop), or `restart` with a new query and/or a sharpened objective.
4. A restart begins a fresh loop. **Cap: 2 restarts per sub-task** (my call, accepted). When the cap is hit, the sub-task ends as "partial" and the report says what is missing.

### Queue and priority

- Sub-tasks go into a queue. The local model picks up the next unit of work the moment it is free.
- Granularity is one Qwen generation, so different sub-tasks' rounds can interleave. Larger-model calls (check-ins, triage) don't hold the local model and run concurrently.
- **User chat messages jump the queue.** A chat turn runs next, ahead of the next queued Qwen generation, so the chat never stalls behind background work.

### PDFs (any long document; annual reports are one example)

1. Find the PDF via search, then download it.
2. Extract the contents page(s) with PyMuPDF, falling back to `indra-ocr` when the page has no text layer.
3. Send the contents page and the objective to the triage model, which returns the sections and page ranges worth reading.
4. Read only those pages, using the text layer or OCR, and send them to the extraction model to pull figures. Each figure or fact is kept with its PDF URL, page number and document date/period.

### UI

- React + Vite. A design inspired by chatbot interfaces, clean, with no accents or neon.
- Palette: off-blacks, whites, and one orange (≈ `#e8650f`) used only for actions and running states.
- A **light/dark toggle**, with both themes supported. Dark has an off-black base (≈ `#161514`); light has a white base (≈ `#fafaf8`). Mockup: `.superpowers/brainstorm/*/content/look-v2.html`.
- Login page: one demo account shown on the page (e.g. `analyst@demo` / `password`).
- Main screen: a sidebar with "New research" and past conversations; a chat area; the task checklist shows each sub-task as queued / running (round n, check-in at 5) / checking in / restarted / done / partial; a collapsible log per sub-task lists every query tried; the report block has a download button.

## My calls (correctable)

- FastAPI backend; progress streamed to the browser with Server-Sent Events.
- Persistence: one SQLite file (conversations, messages, tasks, sub-tasks, rounds, sources), so history survives restarts.
- Fetching: top 5 search results fetched per round. Pages where `trafilatura` returns nothing are skipped.
- The citation rule is enforced in the report-writer prompt: only figures present in the stored evidence, each with a source and date.
- **Hard limits (configurable in `.env` per user request: `DR_MAX_SUBTASKS`, `DR_ROUNDS_PER_LOOP`, `DR_MAX_RESTARTS`):** defaults are at most 8 sub-tasks per plan and at most 20 rounds per loop (4 check-ins). With the 2-restart cap, that's at most 60 rounds per sub-task. Hitting any cap ends the sub-task as "partial". The backend counts larger-model calls per day per model and stops falling back once every model in a list is exhausted. The task then pauses with a visible "quota exhausted, resumes after reset" state instead of failing.
- **Web evidence:** for each kept page, Qwen stores the relevant passages verbatim (not paraphrased) with URL, page title, published date and fetch date. Figures are pulled from those passages only by the report writer, which keeps web pages free of extra cloud calls. PDF figures are extracted as described above.
- **Missing dates:** if `trafilatura` finds no published date, the evidence is kept and cited as "accessed <fetch date>, publish date unknown". The report flags such figures as lower confidence.
- **When the PDF path runs:** whenever a search result URL or Content-Type is a PDF, and whenever the planner marks a sub-task as needing a specific document (e.g. an annual report, a paper or a filing). In that case Qwen searches for that document's PDF first.
- **Restarts:** evidence from earlier loops is kept and tagged by loop. The check-in model's restart decision can mark earlier evidence as off-target, and the report writer skips evidence marked that way.
- **Several tasks at once:** allowed, including across conversations. The queue takes Qwen work round-robin across active sub-tasks; chat turns still jump ahead of everything.

## Left to the builder

- Prompt wording and the structured-output formats between the models.
- Exact SQLite schema, API routes and React component structure.
- How Qwen decides chat vs search vs task (prompted classifier or tool-calling).
- Reranking method (pointwise scoring vs listwise), within the rule that Qwen does it.
- Retry and backoff details beyond the fallback order.

## Future work (flagged by the user)

- **Redesign the document/OCR retrieval path.** The contents-page → triage → read-chosen-pages flow above is a first version. The user wants to revisit how information is retrieved from documents, and the OCR step with it. Keep the PDF module isolated behind a small interface (input: PDF URL + objective; output: passages/figures with page and date) so it can be replaced without touching the task loop.
