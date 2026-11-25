import logging
import os
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# override=False so real environment variables win over .env
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


_PLAN = ["gemini-3.5-flash-lite", "qwen/qwen3.8-27b"]
ROLES: dict[str, list[str]] = {
    "plan": _PLAN,
    "report": _PLAN,
    "checkin": ["qwen/qwen3.8-27b", "gemini-3.1-flash-lite"],
}
DAILY_LIMITS: dict[str, int] = {
    "gemini-3.5-flash-lite": 500,
    "gemini-3.1-flash-lite": 500,
    "qwen/qwen3.8-27b": 1000,
}
PROVIDER: dict[str, str] = {
    "gemini-3.5-flash-lite": "gemini",
    "gemini-3.1-flash-lite": "gemini",
    "qwen/qwen3.8-27b": "groq",
}


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    log.warning("invalid %s=%r, using default %d", name, raw, default)
    return default


MAX_SUBTASKS = _positive_int("DR_MAX_SUBTASKS", 8)
MAX_RESTARTS = _positive_int("DR_MAX_RESTARTS", 2)
MAX_PAGES = _positive_int("DR_MAX_PAGES", 150)
MAX_DELEGATIONS = _positive_int("DR_MAX_DELEGATIONS", 6)  # main agent hand-offs per loop
PDF_STEPS = _positive_int("DR_PDF_STEPS", 12)  # tool calls per PDF-agent call
WEB_STEPS = _positive_int("DR_WEB_STEPS", 12)  # tool calls per web-agent call
CONTEXT_TOKENS = _positive_int("DR_CONTEXT_TOKENS", 12000)  # compact the agent conversation past this
THINK_TOKENS = _positive_int("DR_THINK_TOKENS", 3000)  # thinking budget of an agent's planning call
RESEARCH_URL = os.environ.get("DR_RESEARCH_URL", "").strip() or "http://127.0.0.1:8001/v1"  # Bonsai on MTPLX
CHAT_URL = os.environ.get("DR_CHAT_URL", "").strip() or "http://127.0.0.1:8002/v1"  # Qwen 3.5 4B on MTPLX
SEARCH_BACKENDS = os.environ.get("DR_SEARCH_BACKENDS", "").strip() or \
    "yahoo,bing,brave,duckduckgo,google,mojeek,startpage"
for _old in ("DR_ROUNDS_PER_LOOP", "DR_MAX_STEPS"):
    if os.environ.get(_old):
        log.info("%s is no longer used (see DR_MAX_DELEGATIONS, DR_PDF_STEPS, DR_WEB_STEPS)", _old)  # pages Qwen may read per research (PDF and web)

SEARCH_MAX_RESULTS = _positive_int("DR_SEARCH_MAX_RESULTS", 8)  # every result is fetched and read
# DuckDuckGo region code, e.g. in-en (India), us-en, uk-en, wt-wt (no region)
SEARCH_REGION = os.environ.get("DR_SEARCH_REGION", "").strip() or "us-en"
FETCH_TIMEOUT = 15
FETCH_MAX_BYTES = 2_000_000

DEMO_USER = "analyst@demo"
DEMO_PASSWORD = "password"

DB_PATH = os.environ.get("DR_DB", "data/dr.sqlite")
