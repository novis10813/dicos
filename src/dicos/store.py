"""Private SQLite job records with serialized sequence allocation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3

from dicos import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, project TEXT, seq INT, slurm_id TEXT,
    mode TEXT CHECK (mode IN ('git', 'sync')), commit_sha TEXT, command TEXT,
    partition TEXT, gpus INT, cpus INT, mem TEXT, time_limit TEXT,
    status TEXT, slurm_state TEXT, exit_code INT, error TEXT, remote_dir TEXT,
    created_at TEXT, started_at TEXT, finished_at TEXT,
    UNIQUE (project, seq)
)
"""
_FIELDS = set("slurm_id mode commit_sha command partition gpus cpus mem time_limit status slurm_state exit_code error remote_dir created_at started_at finished_at".split())
_TERMINAL = {"succeeded", "failed", "cancelled", "lost"}
_TRANSITIONS = {"queued": {"running"} | _TERMINAL, "running": _TERMINAL}


class JobStore:
    """Each operation opens its own connection, including across threads."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else paths.ensure_home() / "dicos.db"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._connect() as db:
            db.execute(_SCHEMA)

    @contextmanager
    def _connect(self, write: bool = False):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _validate(fields):
        unknown = fields.keys() - _FIELDS
        if unknown:
            raise ValueError(f"Unknown or immutable job fields: {', '.join(sorted(unknown))}")
        if "status" in fields and fields["status"] not in _TRANSITIONS.keys() | _TERMINAL:
            raise ValueError(f"Invalid status: {fields['status']}")

    def add(self, project: str, prefix: str, **fields) -> str:
        self._validate(fields)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
            raise ValueError("Invalid job prefix")
        with self._connect(write=True) as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM jobs WHERE project = ?", (project,)).fetchone()[0]
            job_id = f"{prefix}-{seq}"
            row = {"id": job_id, "project": project, "seq": seq,
                   "status": "queued", "created_at": datetime.now(timezone.utc).isoformat(), **fields}
            columns = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            db.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(row.values()))
        return job_id

    def get(self, id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (id,)).fetchone()
            return dict(row) if row else None

    def list(self, status: str | None = None, limit: int | None = 20) -> list[dict]:
        query = "SELECT * FROM jobs"
        args = []
        if status is not None:
            query += " WHERE status = ?"
            args.append(status)
        query += " ORDER BY created_at DESC, rowid DESC"
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be nonnegative or None")
            query += " LIMIT ?"
            args.append(limit)
        with self._connect() as db:
            return [dict(row) for row in db.execute(query, args)]

    def update(self, id: str, **fields) -> None:
        self._validate(fields)
        with self._connect(write=True) as db:
            row = db.execute("SELECT status FROM jobs WHERE id = ?", (id,)).fetchone()
            if row is None:
                raise KeyError(id)
            current = row["status"]
            if current in _TERMINAL:
                raise ValueError(f"Terminal job {id} cannot be updated")
            target = fields.get("status", current)
            if target != current and target not in _TRANSITIONS[current]:
                raise ValueError(f"Invalid transition: {current} -> {target}")
            if fields:
                assignments = ", ".join(f"{key} = ?" for key in fields)
                db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), id))

    def active(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at DESC, rowid DESC")]
