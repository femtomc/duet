"""SQLite persistence layer for durable run history and queryable metadata."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Phase, ReviewVerdict, RunSnapshot


# ──────────────────────────────────────────────────────────────────────────────
# DATABASE SCHEMA
# ──────────────────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1

SCHEMA_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Orchestration runs
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    phase TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    original_branch TEXT,
    feature_branch TEXT,
    baseline_commit TEXT,
    latest_commit TEXT,
    consecutive_replans INTEGER DEFAULT 0
);

-- Per-iteration records
CREATE TABLE IF NOT EXISTS iterations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    phase TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response_content TEXT NOT NULL,
    verdict TEXT,  -- APPROVE, CHANGES_REQUESTED, BLOCKED (REVIEW phase only)
    concluded BOOLEAN DEFAULT 0,
    next_phase TEXT,
    requires_human BOOLEAN DEFAULT 0,
    decision_rationale TEXT,

    -- Git metadata
    files_changed INTEGER,
    insertions INTEGER,
    deletions INTEGER,
    commit_sha TEXT,
    new_commits_created BOOLEAN DEFAULT 0,

    -- Token/usage metadata
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,

    -- Event metadata
    stream_events INTEGER,
    thread_id TEXT,

    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    UNIQUE(run_id, iteration, phase)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_phase ON runs(phase);
CREATE INDEX IF NOT EXISTS idx_iterations_run_id ON iterations(run_id);
CREATE INDEX IF NOT EXISTS idx_iterations_phase ON iterations(phase);
CREATE INDEX IF NOT EXISTS idx_iterations_verdict ON iterations(verdict);
"""


class PersistenceError(Exception):
    """Exception raised during database operations."""

    pass


class DuetDatabase:
    """SQLite persistence layer for Duet orchestration metadata."""

    def __init__(self, db_path: Path | str):
        self.db_path = db_path
        self.is_memory = db_path == ":memory:"

        # For in-memory databases, keep persistent connection
        if self.is_memory:
            self._memory_conn = sqlite3.connect(":memory:")
            self._memory_conn.row_factory = sqlite3.Row
            self._ensure_schema_on_conn(self._memory_conn)
        else:
            self._memory_conn = None
            self._ensure_schema()

    @contextmanager
    def _conn(self):
        """Context manager for database connections."""
        if self.is_memory:
            # Reuse persistent connection for in-memory database
            yield self._memory_conn
            # No commit/rollback here - caller manages transaction
        else:
            # Create new connection for file database
            db_location = str(self.db_path) if isinstance(self.db_path, Path) else self.db_path
            conn = sqlite3.connect(db_location)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _ensure_schema_on_conn(self, conn: sqlite3.Connection) -> None:
        """Apply schema to a specific connection."""
        conn.executescript(SCHEMA_DDL)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        conn.commit()

    def _ensure_schema(self) -> None:
        """
        Ensure database schema exists and is up to date.

        Applies migrations if schema version is outdated.
        """
        # Use direct connection for schema initialization (executescript auto-commits)
        db_location = str(self.db_path) if isinstance(self.db_path, Path) else self.db_path
        conn = sqlite3.connect(db_location)
        conn.row_factory = sqlite3.Row

        try:
            # Check if schema_version table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            )
            schema_table_exists = cursor.fetchone() is not None

            if not schema_table_exists:
                # Fresh database - apply full schema
                conn.executescript(SCHEMA_DDL)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, dt.datetime.now(dt.timezone.utc).isoformat()),
                )
                conn.commit()
            else:
                # Check current version
                cursor = conn.execute("SELECT MAX(version) as version FROM schema_version")
                row = cursor.fetchone()
                current_version = row["version"] if row else 0

                if current_version < SCHEMA_VERSION:
                    # Apply migrations (none yet, but framework in place)
                    self._apply_migrations(conn, current_version, SCHEMA_VERSION)
        finally:
            conn.close()

    def _apply_migrations(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        """Apply schema migrations from one version to another."""
        # Placeholder for future migrations
        # Example: if from_version < 2 and to_version >= 2: apply_migration_v2(conn)
        pass

    # ──────────────────────────────────────────────────────────────────────────
    # Run Operations
    # ──────────────────────────────────────────────────────────────────────────

    def insert_run(self, snapshot: RunSnapshot) -> None:
        """Insert a new run record."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, created_at, started_at, phase, iteration,
                    notes, original_branch, feature_branch, baseline_commit,
                    latest_commit, consecutive_replans
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.run_id,
                    snapshot.created_at.isoformat(),
                    snapshot.metadata.get("started_at"),
                    snapshot.phase.value,
                    snapshot.iteration,
                    snapshot.notes,
                    snapshot.metadata.get("original_branch"),
                    snapshot.metadata.get("feature_branch"),
                    snapshot.metadata.get("baseline_commit"),
                    snapshot.metadata.get("latest_commit"),
                    snapshot.metadata.get("consecutive_replans", 0),
                ),
            )

    def update_run(self, snapshot: RunSnapshot) -> None:
        """Update an existing run record."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE runs SET
                    phase = ?,
                    iteration = ?,
                    notes = ?,
                    completed_at = ?,
                    latest_commit = ?,
                    consecutive_replans = ?
                WHERE run_id = ?
                """,
                (
                    snapshot.phase.value,
                    snapshot.iteration,
                    snapshot.notes,
                    snapshot.metadata.get("completed_at"),
                    snapshot.metadata.get("latest_commit"),
                    snapshot.metadata.get("consecutive_replans", 0),
                    snapshot.run_id,
                ),
            )

    def upsert_run(self, snapshot: RunSnapshot) -> None:
        """Insert or update run record."""
        with self._conn() as conn:
            cursor = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (snapshot.run_id,))
            exists = cursor.fetchone() is not None

            if exists:
                self.update_run(snapshot)
            else:
                self.insert_run(snapshot)

    # ──────────────────────────────────────────────────────────────────────────
    # Iteration Operations
    # ──────────────────────────────────────────────────────────────────────────

    def insert_iteration(
        self,
        run_id: str,
        iteration: int,
        phase: Phase,
        prompt: str,
        response_content: str,
        verdict: Optional[ReviewVerdict] = None,
        concluded: bool = False,
        next_phase: Optional[Phase] = None,
        requires_human: bool = False,
        decision_rationale: Optional[str] = None,
        git_metadata: Optional[Dict[str, Any]] = None,
        usage_metadata: Optional[Dict[str, Any]] = None,
        stream_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert iteration record with full metadata."""
        git_meta = git_metadata or {}
        usage_meta = usage_metadata or {}
        stream_meta = stream_metadata or {}

        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO iterations (
                    run_id, iteration, phase, timestamp, prompt, response_content,
                    verdict, concluded, next_phase, requires_human, decision_rationale,
                    files_changed, insertions, deletions, commit_sha, new_commits_created,
                    input_tokens, output_tokens, cached_input_tokens,
                    stream_events, thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    iteration,
                    phase.value,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    prompt,
                    response_content,
                    verdict.value if verdict else None,
                    concluded,
                    next_phase.value if next_phase else None,
                    requires_human,
                    decision_rationale,
                    git_meta.get("files_changed"),
                    git_meta.get("insertions"),
                    git_meta.get("deletions"),
                    git_meta.get("commit_sha"),
                    git_meta.get("new_commits_created", False),
                    usage_meta.get("input_tokens"),
                    usage_meta.get("output_tokens"),
                    usage_meta.get("cached_input_tokens"),
                    stream_meta.get("stream_events"),
                    stream_meta.get("thread_id"),
                ),
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Query Operations
    # ──────────────────────────────────────────────────────────────────────────

    def list_runs(
        self,
        phase: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List runs with optional filters."""
        with self._conn() as conn:
            query = "SELECT * FROM runs"
            params = []

            if phase:
                query += " WHERE phase = ?"
                params.append(phase)

            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get run record by ID."""
        with self._conn() as conn:
            cursor = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_iterations(self, run_id: str) -> List[Dict[str, Any]]:
        """List all iterations for a run."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM iterations WHERE run_id = ? ORDER BY iteration, phase",
                (run_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_iteration(
        self, run_id: str, iteration: int, phase: str
    ) -> Optional[Dict[str, Any]]:
        """Get specific iteration record."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM iterations WHERE run_id = ? AND iteration = ? AND phase = ?",
                (run_id, iteration, phase),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    # ──────────────────────────────────────────────────────────────────────────
    # Statistics & Aggregations
    # ──────────────────────────────────────────────────────────────────────────

    def get_run_statistics(self, run_id: str) -> Dict[str, Any]:
        """Get aggregated statistics for a run."""
        with self._conn() as conn:
            # Count iterations by phase
            cursor = conn.execute(
                """
                SELECT phase, COUNT(*) as count
                FROM iterations
                WHERE run_id = ?
                GROUP BY phase
                """,
                (run_id,),
            )
            phase_counts = {row["phase"]: row["count"] for row in cursor.fetchall()}

            # Sum tokens
            cursor = conn.execute(
                """
                SELECT
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(cached_input_tokens) as total_cached
                FROM iterations
                WHERE run_id = ?
                """,
                (run_id,),
            )
            tokens = cursor.fetchone()

            # Get verdict counts
            cursor = conn.execute(
                """
                SELECT verdict, COUNT(*) as count
                FROM iterations
                WHERE run_id = ? AND verdict IS NOT NULL
                GROUP BY verdict
                """,
                (run_id,),
            )
            verdict_counts = {row["verdict"]: row["count"] for row in cursor.fetchall()}

            return {
                "phase_counts": phase_counts,
                "total_input_tokens": tokens["total_input"] or 0,
                "total_output_tokens": tokens["total_output"] or 0,
                "total_cached_tokens": tokens["total_cached"] or 0,
                "verdict_counts": verdict_counts,
            }

    def search_runs(
        self,
        phase: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        run_id_prefix: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Search runs with multiple filters."""
        with self._conn() as conn:
            query = "SELECT * FROM runs WHERE 1=1"
            params = []

            if phase:
                query += " AND phase = ?"
                params.append(phase)

            if date_from:
                query += " AND created_at >= ?"
                params.append(date_from)

            if date_to:
                query += " AND created_at <= ?"
                params.append(date_to)

            if run_id_prefix:
                query += " AND run_id LIKE ?"
                params.append(f"{run_id_prefix}%")

            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
