"""App logging: terminal + rotating file, every line tagged with its conversation/task/sub-task.

DR_LOG_LEVEL (INFO; DEBUG adds prompt and reply previews), DR_LOG_FILE (logs/dr.log).
"""
import contextvars
import logging
import os
from logging.handlers import RotatingFileHandler

# set where work starts; asyncio tasks inherit them, so child coroutines log with the same ids
conv = contextvars.ContextVar("conv", default=None)
task = contextvars.ContextVar("task", default=None)
sub = contextvars.ContextVar("sub", default=None)

_FMT = "%(asctime)s %(levelname)-5s %(name)-14s %(ctx)s%(message)s"
_handlers: list[logging.Handler] = []


class _Ctx(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ids = [f"{p}{v.get()}" for p, v in (("c", conv), ("t", task), ("s", sub)) if v.get() is not None]
        record.ctx = f"[{' '.join(ids)}] " if ids else ""
        return True


def clip(s, n: int = 160) -> str:
    """One-line preview of possibly long text."""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


def setup(force: bool = False) -> None:
    if _handlers and not force:
        return
    teardown()
    path = os.environ.get("DR_LOG_FILE", "logs/dr.log")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = logging.Formatter(_FMT, "%H:%M:%S")
    for h in (logging.StreamHandler(), RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5)):
        h.setFormatter(fmt)
        h.addFilter(_Ctx())
        _handlers.append(h)
    app = logging.getLogger("app")
    level = os.environ.get("DR_LOG_LEVEL", "INFO").upper()
    app.setLevel(level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO")
    for h in _handlers:
        app.addHandler(h)
    app.info("logging to %s at %s", path, logging.getLevelName(app.level))


def teardown() -> None:
    app = logging.getLogger("app")
    for h in _handlers:
        app.removeHandler(h)
        h.close()
    _handlers.clear()
