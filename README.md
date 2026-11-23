# Deep Research Chatbot

A chat app that answers quick questions locally (Qwen on MLX), runs web searches, and runs multi-round research tasks that end in a cited report.
FastAPI backend in `backend/`, React + Vite frontend in `frontend/`.

## Prerequisites
- macOS on Apple Silicon
- Python 3.13 with `pip install -r requirements.txt` (fastapi, uvicorn, sse-starlette, mlx-lm, ddgs, trafilatura, requests, indra-ocr, pypdfium2, openai, python-dotenv, tzdata, pytest)
- Node.js (for `frontend/`)
- MLX model `mlx-community/Qwen3.5-4B-4bit` already in the Hugging Face cache (`huggingface-cli download mlx-community/Qwen3.5-4B-4bit`)

## Setup
Copy `.env.example` to `.env` and fill in:
- `GEMINI_API_KEY`, `GROQ_API_KEY`
- Optional limits (defaults): `DR_MAX_SUBTASKS=8`, `DR_ROUNDS_PER_LOOP=20`, `DR_MAX_RESTARTS=2`
- Optional: `DR_DB` (SQLite path, default `data/dr.sqlite`)

### Verify model IDs (once, after adding keys)
The IDs in `backend/app/config.py` (`ROLES`, `DAILY_LIMITS`, `PROVIDER`) are unverified. List what each endpoint serves and fix any that differ:
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

## How tasks work
- Start a message with `task:` (or let the router pick) to launch a research task; `search:` runs a plain web search.
- The planner splits the objective into sub-tasks (at most `DR_MAX_SUBTASKS`), shown as a live checklist.
- Each sub-task loops search, fetch and extract rounds (up to `DR_ROUNDS_PER_LOOP`); a check-in every 5 rounds decides whether to continue.
- A sub-task interrupted by a backend restart shows "interrupted" (`DR_MAX_RESTARTS` caps restarts).
- The final report cites a URL or PDF page and a date for each figure, then Qwen adds a short summary. Chat keeps working while a task runs.

If the keys are still placeholders, a task ends `failed` with a hint to check `.env`.

## Known limits
- Shortcut: demo auth only (fixed login, no token checks on API routes); localhost use only.
- Public information only.
- PDF/OCR retrieval is a first pass and will be redesigned later.
