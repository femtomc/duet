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

# Schema version for migrations
SCHEMA_VERSION = 3  # Added messages table for channel payload history

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
    consecutive_replans INTEGER DEFAULT 0,
    active_state_id TEXT  -- Sprint 8: current checkpoint state
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

-- Streaming events (Sprint 6)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    iteration INTEGER,
    phase TEXT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,  -- JSON-encoded event payload
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- Run states for stateful workflow
CREATE TABLE IF NOT EXISTS run_states (
    state_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    phase_status TEXT NOT NULL,  -- plan-ready, plan-complete, implement-ready, etc.
    created_at TEXT NOT NULL,
    baseline_commit TEXT,  -- git commit at this checkpoint
    parent_state_id TEXT,  -- previous state in chain
    notes TEXT,
    verdict TEXT,  -- review verdict if applicable
    feedback TEXT,  -- user feedback for this state
    metadata TEXT,  -- JSON blob for additional state data

    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (parent_state_id) REFERENCES run_states(state_id)
);

-- Channel messages for workflow message passing
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    state_id TEXT,  -- State when message was created
    iteration INTEGER,
    phase TEXT,  -- Phase that published this message
    channel TEXT NOT NULL,  -- Channel name
    payload TEXT NOT NULL,  -- JSON-encoded channel value
    metadata TEXT,  -- JSON-encoded metadata (schema, timestamp, source)
    created_at TEXT NOT NULL,

    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (state_id) REFERENCES run_states(state_id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_phase ON runs(phase);
CREATE INDEX IF NOT EXISTS idx_runs_active_state ON runs(active_state_id);
CREATE INDEX IF NOT EXISTS idx_iterations_run_id ON iterations(run_id);
CREATE INDEX IF NOT EXISTS idx_iterations_phase ON iterations(phase);
CREATE INDEX IF NOT EXISTS idx_iterations_verdict ON iterations(verdict);
CREATE INDEX IF NOT EXISTS idx_events_run_phase ON events(run_id, phase);
CREATE INDEX IF NOT EXISTS idx_events_run_iteration ON events(run_id, iteration);
CREATE INDEX IF NOT EXISTS idx_run_states_run_id ON run_states(run_id);
CREATE INDEX IF NOT EXISTS idx_run_states_phase_status ON run_states(phase_status);
CREATE INDEX IF NOT EXISTS idx_run_states_created_at ON run_states(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_run_id ON messages(run_id);
CREATE INDEX IF NOT EXISTS idx_messages_state_id ON messages(state_id);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_messages_run_channel ON messages(run_id, channel);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at DESC);
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
        conn.commit()
        # Apply migrations
        self._apply_migrations(conn)

    def _ensure_schema(self) -> None:
        """Ensure database schema exists (creates all tables if missing)."""
        db_location = str(self.db_path) if isinstance(self.db_path, Path) else self.db_path
        conn = sqlite3.connect(db_location)
        try:
            # Apply base schema
            conn.executescript(SCHEMA_DDL)
            conn.commit()

            # Check and apply migrations
            self._apply_migrations(conn)
        finally:
            conn.close()

    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        """Get current schema version."""
        try:
            cursor = conn.execute("SELECT MAX(version) as version FROM schema_version")
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            # schema_version table doesn't exist yet
            return 0

    def _set_schema_version(self, conn: sqlite3.Connection, version: int) -> None:
        """Set schema version."""
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        conn.commit()

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply schema migrations if needed."""
        current_version = self._get_schema_version(conn)

        # Migration 1 -> 2: Add active_state_id to runs table
        if current_version < 2:
            try:
                # Check if column already exists
                cursor = conn.execute("PRAGMA table_info(runs)")
                columns = [row[1] for row in cursor.fetchall()]

                if "active_state_id" not in columns:
                    # Add active_state_id column
                    conn.execute("ALTER TABLE runs ADD COLUMN active_state_id TEXT")
                    conn.commit()

                # Mark version as applied
                self._set_schema_version(conn, 2)
            except sqlite3.OperationalError as e:
                # Column might already exist, ignore error
                pass

        # Migration 2 -> 3: Add messages table
        if current_version < 3:
            try:
                # Check if table already exists
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
                )
                if not cursor.fetchone():
                    # Table doesn't exist, it will be created by SCHEMA_DDL
                    # Just mark version as applied
                    self._set_schema_version(conn, 3)
                else:
                    # Table exists, just update version
                    self._set_schema_version(conn, 3)
            except sqlite3.OperationalError as e:
                # Ignore errors, schema will be created
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
    # Event Operations (Sprint 6)
    # ──────────────────────────────────────────────────────────────────────────

    def insert_event(
        self,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        iteration: Optional[int] = None,
        phase: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """Insert a streaming event record."""
        if timestamp is None:
            timestamp = dt.datetime.now(dt.timezone.utc).isoformat()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO events (run_id, iteration, phase, timestamp, event_type, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    iteration,
                    phase,
                    timestamp,
                    event_type,
                    json.dumps(payload),
                ),
            )

    def list_events(
        self,
        run_id: str,
        iteration: Optional[int] = None,
        phase: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        List events for a run with optional filtering.

        Args:
            run_id: Run identifier
            iteration: Filter by iteration number
            phase: Filter by phase
            event_type: Filter by event type
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries with parsed JSON payloads
        """
        with self._conn() as conn:
            query = "SELECT * FROM events WHERE run_id = ?"
            params: List[Any] = [run_id]

            if iteration is not None:
                query += " AND iteration = ?"
                params.append(iteration)

            if phase:
                query += " AND phase = ?"
                params.append(phase)

            if event_type:
                query += " AND event_type = ?"
                params.append(event_type)

            query += " ORDER BY timestamp ASC, id ASC"

            if limit:
                query += " LIMIT ?"
                params.append(limit)

            cursor = conn.execute(query, params)
            events = []
            for row in cursor.fetchall():
                event_dict = dict(row)
                # Parse JSON payload back to dict
                event_dict["payload"] = json.loads(event_dict["payload"])
                events.append(event_dict)
            return events

    def count_events(
        self,
        run_id: str,
        iteration: Optional[int] = None,
        phase: Optional[str] = None,
    ) -> int:
        """Count events for a run with optional filtering."""
        with self._conn() as conn:
            query = "SELECT COUNT(*) as count FROM events WHERE run_id = ?"
            params: List[Any] = [run_id]

            if iteration is not None:
                query += " AND iteration = ?"
                params.append(iteration)

            if phase:
                query += " AND phase = ?"
                params.append(phase)

            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            return row["count"] if row else 0

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

    # ──────────────────────────────────────────────────────────────────────────
    # State Operations    # ──────────────────────────────────────────────────────────────────────────

    def insert_state(
        self,
        state_id: str,
        run_id: str,
        phase_status: str,
        baseline_commit: Optional[str] = None,
        parent_state_id: Optional[str] = None,
        notes: Optional[str] = None,
        verdict: Optional[str] = None,
        feedback: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert a new run state checkpoint."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO run_states (
                    state_id, run_id, phase_status, created_at,
                    baseline_commit, parent_state_id, notes, verdict, feedback, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state_id,
                    run_id,
                    phase_status,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    baseline_commit,
                    parent_state_id,
                    notes,
                    verdict,
                    feedback,
                    json.dumps(metadata) if metadata else None,
                ),
            )

    def get_state(self, state_id: str) -> Optional[Dict[str, Any]]:
        """Get state record by ID."""
        with self._conn() as conn:
            cursor = conn.execute("SELECT * FROM run_states WHERE state_id = ?", (state_id,))
            row = cursor.fetchone()
            if row:
                state_dict = dict(row)
                # Parse JSON metadata
                if state_dict.get("metadata"):
                    state_dict["metadata"] = json.loads(state_dict["metadata"])
                return state_dict
            return None

    def list_states(self, run_id: str) -> List[Dict[str, Any]]:
        """List all states for a run in chronological order."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM run_states WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            )
            states = []
            for row in cursor.fetchall():
                state_dict = dict(row)
                # Parse JSON metadata
                if state_dict.get("metadata"):
                    state_dict["metadata"] = json.loads(state_dict["metadata"])
                states.append(state_dict)
            return states

    def get_active_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get the active state for a run."""
        with self._conn() as conn:
            # Get active_state_id from runs table
            cursor = conn.execute(
                "SELECT active_state_id FROM runs WHERE run_id = ?", (run_id,)
            )
            row = cursor.fetchone()
            if not row or not row["active_state_id"]:
                return None

            active_state_id = row["active_state_id"]
            return self.get_state(active_state_id)

    def update_active_state(self, run_id: str, state_id: str) -> None:
        """Update the active state for a run."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET active_state_id = ? WHERE run_id = ?",
                (state_id, run_id),
            )

    def get_latest_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent state for a run."""
        with self._conn() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM run_states
                WHERE run_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            if row:
                state_dict = dict(row)
                if state_dict.get("metadata"):
                    state_dict["metadata"] = json.loads(state_dict["metadata"])
                return state_dict
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Message Operations
    # ──────────────────────────────────────────────────────────────────────────

    def insert_message(
        self,
        run_id: str,
        channel: str,
        payload: Any,
        state_id: Optional[str] = None,
        iteration: Optional[int] = None,
        phase: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert a channel message record."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    run_id, state_id, iteration, phase, channel,
                    payload, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    state_id,
                    iteration,
                    phase,
                    channel,
                    json.dumps(payload) if not isinstance(payload, str) else payload,
                    json.dumps(metadata) if metadata else None,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )

    def list_messages(
        self,
        run_id: str,
        channel: Optional[str] = None,
        phase: Optional[str] = None,
        state_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        List messages for a run with optional filtering.

        Messages are ordered newest-first (descending by created_at).

        Args:
            run_id: Run identifier
            channel: Filter by channel name
            phase: Filter by phase
            state_id: Filter by state
            limit: Maximum messages to return

        Returns:
            List of message dictionaries with parsed payloads (newest first)
        """
        with self._conn() as conn:
            query = "SELECT * FROM messages WHERE run_id = ?"
            params: List[Any] = [run_id]

            if channel:
                query += " AND channel = ?"
                params.append(channel)

            if phase:
                query += " AND phase = ?"
                params.append(phase)

            if state_id:
                query += " AND state_id = ?"
                params.append(state_id)

            query += " ORDER BY created_at DESC, id DESC"

            if limit:
                query += " LIMIT ?"
                params.append(limit)

            cursor = conn.execute(query, params)
            messages = []
            for row in cursor.fetchall():
                msg_dict = dict(row)
                # Parse JSON payload back to original type
                try:
                    msg_dict["payload"] = json.loads(msg_dict["payload"])
                except (json.JSONDecodeError, TypeError):
                    # Payload was plain string, keep as-is
                    pass
                # Parse metadata
                if msg_dict.get("metadata"):
                    try:
                        msg_dict["metadata"] = json.loads(msg_dict["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                messages.append(msg_dict)
            return messages

    def get_state_messages(self, state_id: str) -> List[Dict[str, Any]]:
        """Get all messages for a specific state."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM messages WHERE state_id = ? ORDER BY created_at ASC",
                (state_id,),
            )
            messages = []
            for row in cursor.fetchall():
                msg_dict = dict(row)
                try:
                    msg_dict["payload"] = json.loads(msg_dict["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
                if msg_dict.get("metadata"):
                    try:
                        msg_dict["metadata"] = json.loads(msg_dict["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                messages.append(msg_dict)
            return messages

    def get_latest_channel_message(
        self, run_id: str, channel: str
    ) -> Optional[Dict[str, Any]]:
        """Get the most recent message for a channel in a run."""
        with self._conn() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM messages
                WHERE run_id = ? AND channel = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id, channel),
            )
            row = cursor.fetchone()
            if row:
                msg_dict = dict(row)
                try:
                    msg_dict["payload"] = json.loads(msg_dict["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
                if msg_dict.get("metadata"):
                    try:
                        msg_dict["metadata"] = json.loads(msg_dict["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                return msg_dict
            return None

    def get_channel_history(
        self, run_id: str, channel: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get chronological history for a specific channel in a run.

        Convenience method wrapping list_messages with channel filter.

        Args:
            run_id: Run identifier
            channel: Channel name
            limit: Maximum messages to return

        Returns:
            List of messages in chronological order
        """
        return self.list_messages(run_id, channel=channel, limit=limit)
