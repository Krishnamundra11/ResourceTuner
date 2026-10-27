"""Run/event store: persisted pipeline observability and the basis for replay.

One SQLite file (`data/events.db`) holds `runs` and an append-only `events` table whose
auto-increment `seq` is a global cursor. That single property gives us the three things SSE
needs without extra machinery:

* **ordering** — events are read back in `seq` order, the order they were recorded;
* **reconnect** — a client resumes with `Last-Event-ID: <seq>` and gets only `seq > N`;
* **no duplicates** — delivery is a function of the cursor, not of who is connected.

Writers are the pipeline (a worker thread) and the API (the event loop); readers are SSE
handlers. Both go through SQLite, so no cross-thread queue has to stay consistent. Every
payload is scrubbed of anything key-shaped before it is stored.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from resourcetuner.pipeline import stages as S

REPO = Path(__file__).resolve().parents[2]
DEFAULT_EVENTS_DB = REPO / "data" / "events.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Keys whose values are never stored, however deeply nested.
_SECRET_KEY_RE = re.compile(r"(api[_-]?key|secret|token|password|passwd|credential|authorization)", re.I)
_REDACTED = "[redacted]"
MAX_PAYLOAD_CHARS = 64_000


def scrub(value: Any, _depth: int = 0) -> Any:
    """Drop secret-looking values and keep the payload a reasonable size."""
    if _depth > 12:
        return "[too deep]"
    if isinstance(value, dict):
        return {k: (_REDACTED if _SECRET_KEY_RE.search(str(k)) else scrub(v, _depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, _depth + 1) for v in value[:200]]
    if isinstance(value, str):
        return value if len(value) <= 8000 else value[:8000] + "…[truncated]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:2000]


def new_run_id(kind: str) -> str:
    return f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


class EventStore:
    def __init__(self, db_path: Path | str = DEFAULT_EVENTS_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")       # a reader never blocks the pipeline's writes
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # --- runs ---------------------------------------------------------------------------

    def create_run(self, kind: str, scenario: str | None = None, level: str | None = None,
                   cycles_requested: int = 1, params: dict | None = None,
                   run_id: str | None = None) -> str:
        run_id = run_id or new_run_id(kind)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, kind, scenario, level, cycles_requested, status, params, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, kind, scenario, level, cycles_requested, S.RUN_REQUESTED,
                 json.dumps(scrub(params or {})), time.time()))
        return run_id

    def update_run(self, run_id: str, **fields) -> None:
        allowed = {"status", "cycles_done", "started_at", "ended_at", "error", "scenario", "level"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        if "error" in sets and sets["error"] is not None:
            sets["error"] = str(sets["error"])[:2000]
        clause = ", ".join(f"{k} = ?" for k in sets)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE runs SET {clause} WHERE run_id = ?", (*sets.values(), run_id))

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_row(row) if row else None

    def list_runs(self, limit: int = 50, offset: int = 0, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM runs"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._connect() as conn:
            return [_run_row(r) for r in conn.execute(sql, args).fetchall()]

    def count_runs(self, status: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM runs" + (" WHERE status = ?" if status else "")
        with self._connect() as conn:
            return conn.execute(sql, (status,) if status else ()).fetchone()["n"]

    def active_run(self) -> dict | None:
        """The one run that may be in flight. Enforces the 'one experiment at a time' rule."""
        marks = ",".join("?" * len(S.RUN_ACTIVE_STATUSES))
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM runs WHERE status IN ({marks}) ORDER BY created_at LIMIT 1",
                S.RUN_ACTIVE_STATUSES).fetchone()
        return _run_row(row) if row else None

    # --- events -------------------------------------------------------------------------

    def append(self, run_id: str, stage: str, status: str, *, cycle: int | None = None,
               summary: str = "", payload: dict | None = None, duration_ms: int | None = None,
               ts: float | None = None) -> int:
        """Append one event and return its `seq` (the SSE event id)."""
        if stage not in S.ALL_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        if status not in S.STATUSES:
            raise ValueError(f"unknown status {status!r}")
        body = json.dumps(scrub(payload or {}))
        if len(body) > MAX_PAYLOAD_CHARS:
            body = json.dumps({"_truncated": True, "_chars": len(body),
                               "_note": "payload too large to store; see the run log"})
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO events (run_id, cycle, stage, status, ts, duration_ms, summary, payload)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, cycle, stage, status, ts if ts is not None else time.time(),
                 duration_ms, summary[:2000], body))
            return int(cur.lastrowid)

    def events(self, run_id: str | None = None, after_seq: int = 0, limit: int = 500,
               cycle: int | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE seq > ?"
        args: list[Any] = [after_seq]
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        if cycle is not None:
            sql += " AND cycle = ?"
            args.append(cycle)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [_event_row(r) for r in conn.execute(sql, args).fetchall()]

    def last_seq(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone()
        return int(row["s"])

    def cycles(self, run_id: str) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT cycle FROM events WHERE run_id = ? AND cycle IS NOT NULL ORDER BY cycle",
                (run_id,)).fetchall()
        return [int(r["cycle"]) for r in rows]


def _run_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["params"] = json.loads(d.get("params") or "{}")
    except ValueError:
        d["params"] = {}
    d["active"] = d["status"] in S.RUN_ACTIVE_STATUSES
    return d


def _event_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except ValueError:
        d["payload"] = {}
    return d


class RunEventSink:
    """An EventStore bound to one run_id, with per-stage timing.

    The pipeline only ever sees this: `stage_start(...)` then `stage_done(...)`, so a stage is
    never reported complete before its result is in hand.
    """

    def __init__(self, store: EventStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self._t0: dict[tuple[str, int | None], float] = {}

    def emit(self, stage: str, status: str, *, cycle: int | None = None, summary: str = "",
             payload: dict | None = None, duration_ms: int | None = None) -> int:
        return self.store.append(self.run_id, stage, status, cycle=cycle, summary=summary,
                                 payload=payload, duration_ms=duration_ms)

    def stage_start(self, stage: str, *, cycle: int | None = None, summary: str = "",
                    payload: dict | None = None) -> int:
        self._t0[(stage, cycle)] = time.monotonic()
        return self.emit(stage, S.RUNNING, cycle=cycle, summary=summary, payload=payload)

    def stage_done(self, stage: str, status: str = S.COMPLETED, *, cycle: int | None = None,
                   summary: str = "", payload: dict | None = None) -> int:
        t0 = self._t0.pop((stage, cycle), None)
        ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None
        return self.emit(stage, status, cycle=cycle, summary=summary, payload=payload, duration_ms=ms)
