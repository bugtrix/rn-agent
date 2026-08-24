"""SQLite knowledge store: the agent's memory for one project.

Holds command runs, findings (so a later ``fix --issue <id>`` can refer to what
``health``/``review`` reported), context snapshots, developer decisions and AI
usage accounting. Everything is local to ``.rn-agent/knowledge/knowledge.db``.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import KnowledgeStoreError

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    command       TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    status        TEXT    NOT NULL DEFAULT 'running',
    exit_code     INTEGER,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    agent_version TEXT,
    summary       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_command ON runs(command, started_at DESC);

CREATE TABLE IF NOT EXISTS findings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    kind      TEXT    NOT NULL,
    key       TEXT    NOT NULL,
    severity  TEXT    NOT NULL,
    title     TEXT    NOT NULL,
    payload   TEXT,
    created_at TEXT   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id, severity);
CREATE INDEX IF NOT EXISTS idx_findings_key ON findings(kind, key);

CREATE TABLE IF NOT EXISTS context_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    rn_version TEXT,
    fingerprint TEXT,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    topic      TEXT NOT NULL,
    decision   TEXT NOT NULL,
    rationale  TEXT,
    command    TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    command       TEXT NOT NULL,
    provider      TEXT,
    model         TEXT,
    task          TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    calls         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class KnowledgeStore:
    """Thin, explicit SQLite access. One connection, one lock."""

    __slots__ = ("_path", "_conn", "_lock")

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(self._path), timeout=10.0, check_same_thread=False, isolation_level=None
            )
        except sqlite3.Error as exc:  # pragma: no cover - unwritable project
            raise KnowledgeStoreError(f"cannot open {self._path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version < SCHEMA_VERSION:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # -- lifecycle ---------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def __enter__(self) -> KnowledgeStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            with self._lock:
                return self._conn.execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise KnowledgeStoreError(f"query failed: {exc}") from exc

    # -- runs --------------------------------------------------------------
    def start_run(self, command: str, *, dry_run: bool, agent_version: str) -> int:
        cursor = self._execute(
            "INSERT INTO runs (command, started_at, dry_run, agent_version) VALUES (?, ?, ?, ?)",
            (command, _now(), int(dry_run), agent_version),
        )
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        exit_code: int = 0,
        summary: dict[str, Any] | None = None,
    ) -> None:
        self._execute(
            "UPDATE runs SET finished_at = ?, status = ?, exit_code = ?, summary = ? WHERE id = ?",
            (_now(), status, exit_code, json.dumps(summary or {}, default=str), run_id),
        )

    def recent_runs(self, *, limit: int = 20, command: str | None = None) -> list[dict[str, Any]]:
        if command:
            rows = self._execute(
                "SELECT * FROM runs WHERE command = ? ORDER BY id DESC LIMIT ?",
                (command, limit),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def last_run(self, command: str) -> dict[str, Any] | None:
        rows = self.recent_runs(limit=1, command=command)
        return rows[0] if rows else None

    # -- findings ----------------------------------------------------------
    def record_findings(self, run_id: int, kind: str, findings: Sequence[dict[str, Any]]) -> int:
        timestamp = _now()
        count = 0
        for finding in findings:
            self._execute(
                """
                INSERT INTO findings (run_id, kind, key, severity, title, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    str(finding.get("id") or finding.get("key") or ""),
                    str(finding.get("severity") or "info"),
                    str(finding.get("title") or ""),
                    json.dumps(finding, default=str),
                    timestamp,
                ),
            )
            count += 1
        return count

    def latest_findings(self, kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._execute(
            """
            SELECT payload FROM findings
             WHERE kind = ? AND run_id = (
                 SELECT MAX(run_id) FROM findings WHERE kind = ?
             )
             ORDER BY id ASC LIMIT ?
            """,
            (kind, kind, limit),
        ).fetchall()
        parsed: list[dict[str, Any]] = []
        for row in rows:
            try:
                parsed.append(json.loads(row["payload"]))
            except (json.JSONDecodeError, TypeError):  # pragma: no cover
                continue
        return parsed

    # -- context snapshots -------------------------------------------------
    def save_context(self, payload: dict[str, Any], *, rn_version: str | None) -> int:
        fingerprint = str(abs(hash(json.dumps(payload, sort_keys=True, default=str))))
        cursor = self._execute(
            "INSERT INTO context_snapshots (created_at, rn_version, fingerprint, payload) VALUES (?, ?, ?, ?)",
            (_now(), rn_version, fingerprint, json.dumps(payload, default=str)),
        )
        self._execute(
            """
            DELETE FROM context_snapshots WHERE id NOT IN (
                SELECT id FROM context_snapshots ORDER BY id DESC LIMIT 20
            )
            """
        )
        return int(cursor.lastrowid or 0)

    def context_history(self, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT id, created_at, rn_version FROM context_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- decisions ---------------------------------------------------------
    def record_decision(
        self, topic: str, decision: str, *, rationale: str | None = None, command: str | None = None
    ) -> int:
        cursor = self._execute(
            "INSERT INTO decisions (created_at, topic, decision, rationale, command) VALUES (?, ?, ?, ?, ?)",
            (_now(), topic, decision, rationale, command),
        )
        return int(cursor.lastrowid or 0)

    def decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- AI usage ----------------------------------------------------------
    def record_ai_usage(
        self,
        *,
        command: str,
        provider: str | None,
        model: str | None,
        task: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        calls: int = 1,
    ) -> int:
        cursor = self._execute(
            """
            INSERT INTO ai_usage (created_at, command, provider, model, task, input_tokens, output_tokens, calls)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_now(), command, provider, model, task, input_tokens, output_tokens, calls),
        )
        return int(cursor.lastrowid or 0)

    def ai_usage_summary(self) -> dict[str, int]:
        row = self._execute(
            """
            SELECT COALESCE(SUM(calls), 0) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
              FROM ai_usage
            """
        ).fetchone()
        return {
            "calls": int(row["calls"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
        }

    # -- key/value ---------------------------------------------------------
    def set_value(self, key: str, value: Any) -> None:
        self._execute(
            """
            INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, default=str), _now()),
        )

    def get_value(self, key: str, default: Any = None) -> Any:
        row = self._execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):  # pragma: no cover
            return default

    def stats(self) -> dict[str, Any]:
        runs = self._execute("SELECT COUNT(*) AS total FROM runs").fetchone()["total"]
        findings = self._execute("SELECT COUNT(*) AS total FROM findings").fetchone()["total"]
        return {
            "runs": int(runs),
            "findings": int(findings),
            "ai": self.ai_usage_summary(),
            "database": str(self._path),
        }
