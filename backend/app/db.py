import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config

SCHEMA = """
create table if not exists conversations(
  id integer primary key autoincrement, title text, created_at text default current_timestamp);
create table if not exists messages(
  id integer primary key autoincrement, conversation_id integer, role text, content text,
  kind text, task_id integer, created_at text default current_timestamp);
create table if not exists tasks(
  id integer primary key autoincrement, conversation_id integer, objective text, status text,
  report text, created_at text default current_timestamp);
create table if not exists subtasks(
  id integer primary key autoincrement, task_id integer, objective text, query text,
  needs_document integer default 0, status text, round integer default 0,
  loop integer default 0, restarts integer default 0);
create table if not exists rounds(
  id integer primary key autoincrement, subtask_id integer, loop integer, n integer,
  query text, hits integer);
create table if not exists evidence(
  id integer primary key autoincrement, subtask_id integer, loop integer, url text, title text,
  passage text, published text, fetched_at text, page integer, off_target integer default 0);
create table if not exists documents(
  id integer primary key autoincrement, task_id integer, key text, name text, issuer text, type text,
  period text, url text, about text, covers text, pages integer, status text, domains text, search text,
  offset_ok integer default 0, contents text, notes text);
create table if not exists fields(
  id integer primary key autoincrement, task_id integer, subtask_id integer, name text, entity text,
  period text, kind text, sources text, search text, hint text, status text, accepted integer default 0,
  value text, unit text, reported_period text, source_url text, source_key text, page integer,
  quote text, source_kind text, note text, printed_page integer);
create table if not exists quota(
  day text, model text, used integer default 0, primary key(day, model));
"""


@dataclass
class Hit:  # one search result
    title: str
    url: str
    snippet: str
    text: str = ""
    published: str | None = None
    fetched_at: str = ""
    is_pdf: bool = False


@dataclass
class Evidence:  # one stored piece of evidence
    subtask_id: int
    loop: int
    url: str
    title: str
    passage: str
    published: str | None
    fetched_at: str
    page: int | None = None
    off_target: bool = False


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """`create table if not exists` leaves older databases' tables as they were: add new columns."""
    ref = sqlite3.connect(":memory:")
    ref.executescript(SCHEMA)
    for (table,) in ref.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"):
        have = {r[1] for r in conn.execute(f"pragma table_info({table})")}
        for _, name, typ, _, default, _ in ref.execute(f"pragma table_info({table})"):
            if name not in have:
                constant = default is not None and re.fullmatch(r"-?[\d.]+|'[^']*'", default)  # sqlite refuses others
                conn.execute(f"alter table {table} add column {name} {typ}" + (f" default {default}" if constant else ""))
    conn.commit()
    ref.close()


def insert(conn: sqlite3.Connection, table: str, **cols) -> int:
    names = ",".join(cols)
    cur = conn.execute(
        f"insert into {table}({names}) values({','.join('?' * len(cols))})", tuple(cols.values())
    )
    conn.commit()
    return cur.lastrowid


def update(conn: sqlite3.Connection, table: str, id: int, **cols) -> None:
    sets = ",".join(f"{c}=?" for c in cols)
    conn.execute(f"update {table} set {sets} where id=?", (*cols.values(), id))
    conn.commit()
