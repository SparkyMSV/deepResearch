# Deep Research Harness

A harness that lets a small local model, Qwen 3.5 4B running on a MacBook with MLX, explore the web and
PDFs to answer research questions. Everything else in this repo exists to make that exploration work:

- **Larger cloud models** (Gemini Flash-Lite, Groq Qwen 27B) set targets, review progress and write the
  final report. They plan and supervise; they are not meant to do the exploring.
- **The harness** gives Qwen small, well-scoped decisions with compact context, and guards the results:
  no repeat searches or pages, free screening on search-result URLs and snippets, verbatim quotes for
  every extracted passage, budgets, and fallbacks when cloud models are down.
- **The chat UI** shows what Qwen is doing, round by round.

FastAPI backend in `backend/`, React + Vite frontend in `frontend/`.

## How it works today

1. **Chat.** Qwen reads each message and either answers, runs a quick web search (`search:`), or starts a
   research task (`task:`). Replies stream token by token.
2. **Plan.** A larger model splits the research into sub-tasks (at most `DR_MAX_SUBTASKS`) and names
   documents worth finding, such as an annual report.
3. **Explore.** Each sub-task runs rounds. In a round Qwen either searches the web or reads a PDF that any
   sub-task has already found:
   - Web: DuckDuckGo returns results, every page is fetched, Qwen ranks them and copies relevant passages
     word for word (passages not found verbatim on the page are dropped).
   - PDFs: indra-ocr turns pages into text; only text is sent to models. A larger model picks pages from
     the contents and pulls figures with page numbers. Every PDF read is shared with the other sub-tasks
     with a one-line note on what it is, and a sub-task can go back for more pages.
   - After each round Qwen picks the next step: read a shared PDF, or write a new search. Searches that
     repeat or nearly repeat earlier ones (80% shared words) are refused.
4. **Check in.** Every 5 rounds a larger model reviews the evidence and decides: continue, done, or
   restart with a better search and objective.
5. **Report.** A larger model writes a cited report (URL or PDF page, plus date, for each figure), then
   Qwen adds a short summary in chat.

## Roadmap

- **Redesign: Qwen explores PDFs itself** (design:
  `docs/superpowers/specs/2026-11-24-field-checklist-redesign-design.md`, awaiting approval). The plan
  becomes a checklist of specific fields (figure, entity, period). Qwen reads PDFs page by page from their
  contents and fills fields with exact quotes. The larger model only plans, reviews a fields table, and
  writes the report.
- **Optimise Qwen on the MacBook.** Make the local model faster and lighter: prompt caching, batching,
  quantisation and context-length choices, and measuring time per call.
- **Document/OCR retrieval.** Revisit how information is retrieved from documents, and the OCR step
  with it.

## Prerequisites
- macOS on Apple Silicon
- Python 3.13 with `pip install -r requirements.txt` (fastapi, uvicorn, sse-starlette, mlx-lm, ddgs,
  trafilatura, requests, indra-ocr, pypdfium2, openai, python-dotenv, tzdata, pytest, and pymupdf for the
  PDF tests)
- Node.js (for `frontend/`)
- MLX model `mlx-community/Qwen3.5-4B-4bit` in the Hugging Face cache
  (`huggingface-cli download mlx-community/Qwen3.5-4B-4bit`)

## Setup
Copy `.env.example` to `.env` and fill in:
- `GEMINI_API_KEY`, `GROQ_API_KEY`
- Optional limits (defaults): `DR_MAX_SUBTASKS=8`, `DR_ROUNDS_PER_LOOP=20`, `DR_MAX_RESTARTS=2`,
  `DR_SEARCH_MAX_RESULTS=8` (DuckDuckGo results per search; every one is fetched and read, so higher =
  slower rounds), `DR_SEARCH_REGION=us-en` (`in-en` India, `uk-en`, `wt-wt` no region)
- Optional: `DR_DB` (SQLite path, default `data/dr.sqlite`)
- Logs: every step (search URLs, fetches, Qwen's choices, PDF reads, cloud calls, check-ins) goes to the
  terminal and `logs/dr.log` (rotating, 5 × 5 MB), each line tagged `[c<conversation> t<task> s<sub-task>]`.
  `DR_LOG_LEVEL=DEBUG` adds prompt and reply previews; `DR_LOG_FILE` changes the path.

### Verify model IDs (once, after adding keys)
The IDs in `backend/app/config.py` (`ROLES`, `DAILY_LIMITS`, `PROVIDER`) must match what each endpoint
serves:
```
python -c "import os;from dotenv import load_dotenv;from openai import OpenAI;load_dotenv();print([m.id for m in OpenAI(base_url='https://generativelanguage.googleapis.com/v1beta/openai/',api_key=os.environ['GEMINI_API_KEY']).models.list()])"
python -c "import os;from dotenv import load_dotenv;from openai import OpenAI;load_dotenv();print([m.id for m in OpenAI(base_url='https://api.groq.com/openai/v1',api_key=os.environ['GROQ_API_KEY']).models.list()])"
```
Look for `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` and `qwen/qwen3.8-27b`.

## Run
```
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765   # from the repo root
cd frontend && npm run dev                                             # then open http://localhost:5173
```
Demo login: `analyst@demo` / `password`. The first message loads the local model, so it is slow.

## Limits and failure handling
- Each sub-task makes at most 5 PDF reads; a loop runs at most `DR_ROUNDS_PER_LOOP` rounds and
  `DR_MAX_RESTARTS` caps restarts, after which the sub-task ends `partial`.
- Cloud fallbacks: Gemini Flash-Lite → the other Flash-Lite → Groq Qwen 27B. Rate limits (429), overload
  (5xx) and timeouts cool a model down for 60 s and try the next; the chat says when it switches from
  Gemini to Groq. If every model is unavailable the research pauses, says so, and retries (every minute,
  or every 15 minutes once daily quotas are used up; they reset at midnight Pacific). A bad API key stops
  the research with a hint to check `.env`.
- Tasks don't survive a backend restart: running tasks are marked "interrupted". Ask again to rerun.
- Chat keeps working while a task runs (Qwen answers chat ahead of research work). Without research
  evidence, direct answers come from Qwen's general knowledge and cite no sources.

## Known limits
- Shortcut: demo auth only (fixed login, no token checks on API routes); localhost use only.
- Public information only.
