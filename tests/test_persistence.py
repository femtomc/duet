"""
Tests for SQLite persistence layer.

Tests database operations using in-memory SQLite for fast, isolated testing.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import pytest

from duet.models import ReviewVerdict, RunSnapshot
from duet.persistence import DuetDatabase


@pytest.fixture
def in_memory_db():
    """Create an in-memory SQLite database for testing."""
    db = DuetDatabase(":memory:")
    # Verify Sprint 8 schema applied
    with db._conn() as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='run_states'"
        )
        assert cursor.fetchone() is not None, "Sprint 8 run_states table not created"
    return db


@pytest.fixture
def temp_db():
    """Create a temporary file-based SQLite database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = Path(tmp.name)
    yield DuetDatabase(db_path)
    db_path.unlink()


def test_schema_creation(in_memory_db):
    """Test that schema is created on database initialization."""
    db = in_memory_db

    # Verify tables exist
    with db._conn() as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row["name"] for row in cursor.fetchall()]

    assert "runs" in tables
    assert "iterations" in tables
    assert "events" in tables  # Sprint 6


def test_insert_run(in_memory_db):
    """Test inserting a run record."""
    db = in_memory_db
    snapshot = RunSnapshot(
        run_id="test-run-001",
        iteration=0,
        phase="plan",
        metadata={"started_at": dt.datetime.now(dt.timezone.utc).isoformat()},
    )

    db.insert_run(snapshot)

    # Verify inserted
    run = db.get_run("test-run-001")
    assert run is not None
    assert run["run_id"] == "test-run-001"
    assert run["phase"] == "plan"
    assert run["iteration"] == 0


def test_update_run(in_memory_db):
    """Test updating a run record."""
    db = in_memory_db
    snapshot = RunSnapshot(
        run_id="test-run-002",
        iteration=1,
        phase="plan",
        notes="Initial",
    )

    db.insert_run(snapshot)

    # Update
    snapshot.phase = "done"
    snapshot.iteration = 3
    snapshot.notes = "Completed"
    snapshot.metadata["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    db.update_run(snapshot)

    # Verify updated
    run = db.get_run("test-run-002")
    assert run["phase"] == "done"
    assert run["iteration"] == 3
    assert run["notes"] == "Completed"
    assert run["completed_at"] is not None


def test_upsert_run(in_memory_db):
    """Test upsert (insert or update) run record."""
    db = in_memory_db
    snapshot = RunSnapshot(run_id="test-upsert", iteration=0, phase="plan")

    # First upsert (insert)
    db.upsert_run(snapshot)
    run = db.get_run("test-upsert")
    assert run["iteration"] == 0

    # Second upsert (update)
    snapshot.iteration = 5
    db.upsert_run(snapshot)
    run = db.get_run("test-upsert")
    assert run["iteration"] == 5


def test_insert_iteration(in_memory_db):
    """Test inserting iteration records."""
    db = in_memory_db

    # Insert parent run first
    snapshot = RunSnapshot(run_id="test-run-iter", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert iteration
    db.insert_iteration(
        run_id="test-run-iter",
        iteration=1,
        phase="plan",
        prompt="Create a plan",
        response_content="Here is the plan...",
        verdict=None,
        concluded=False,
        next_phase="implement",
        requires_human=False,
        decision_rationale="Plan complete",
        git_metadata={"files_changed": 3, "insertions": 42, "deletions": 10},
        usage_metadata={"input_tokens": 100, "output_tokens": 50},
        stream_metadata={"stream_events": 5, "thread_id": "abc123"},
    )

    # Verify inserted
    iterations = db.list_iterations("test-run-iter")
    assert len(iterations) == 1

    iter_record = iterations[0]
    assert iter_record["iteration"] == 1
    assert iter_record["phase"] == "plan"
    assert iter_record["response_content"] == "Here is the plan..."
    assert iter_record["files_changed"] == 3
    assert iter_record["input_tokens"] == 100
    assert iter_record["stream_events"] == 5


def test_insert_iteration_with_verdict(in_memory_db):
    """Test inserting iteration with review verdict."""
    db = in_memory_db
    snapshot = RunSnapshot(run_id="test-verdict", iteration=0, phase="review")
    db.insert_run(snapshot)

    db.insert_iteration(
        run_id="test-verdict",
        iteration=1,
        phase="review",
        prompt="Review changes",
        response_content="Approved",
        verdict=ReviewVerdict.APPROVE,
        concluded=True,
        next_phase="done",
        requires_human=False,
        decision_rationale="All good",
    )

    iter_record = db.get_iteration("test-verdict", 1, "review")
    assert iter_record is not None
    assert iter_record["verdict"] == "approve"
    assert iter_record["next_phase"] == "done"


def test_list_runs(in_memory_db):
    """Test listing runs."""
    db = in_memory_db

    # Insert multiple runs
    for i in range(5):
        snapshot = RunSnapshot(
            run_id=f"run-{i:03d}",
            iteration=i,
            phase="done" if i % 2 == 0 else "blocked",
        )
        db.insert_run(snapshot)

    # List all
    runs = db.list_runs(limit=10)
    assert len(runs) == 5

    # Filter by phase
    done_runs = db.list_runs(phase="done", limit=10)
    assert len(done_runs) == 3  # runs 0, 2, 4


def test_list_iterations(in_memory_db):
    """Test listing iterations for a run."""
    db = in_memory_db
    snapshot = RunSnapshot(run_id="test-iters", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert multiple iterations
    for i in range(3):
        db.insert_iteration(
            run_id="test-iters",
            iteration=i + 1,
            phase="plan",
            prompt=f"Prompt {i}",
            response_content=f"Response {i}",
        )

    iterations = db.list_iterations("test-iters")
    assert len(iterations) == 3
    assert iterations[0]["iteration"] == 1
    assert iterations[2]["iteration"] == 3


def test_get_run_statistics(in_memory_db):
    """Test aggregated statistics for a run."""
    db = in_memory_db
    snapshot = RunSnapshot(run_id="test-stats", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert iterations with various metadata
    db.insert_iteration(
        run_id="test-stats",
        iteration=1,
        phase="plan",
        prompt="Plan",
        response_content="Plan response",
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 10},
    )

    db.insert_iteration(
        run_id="test-stats",
        iteration=1,
        phase="implement",
        prompt="Implement",
        response_content="Implemented",
        usage_metadata={"input_tokens": 200, "output_tokens": 100},
    )

    db.insert_iteration(
        run_id="test-stats",
        iteration=1,
        phase="review",
        prompt="Review",
        response_content="Approved",
        verdict=ReviewVerdict.APPROVE,
    )

    stats = db.get_run_statistics("test-stats")

    assert stats["phase_counts"]["plan"] == 1
    assert stats["phase_counts"]["implement"] == 1
    assert stats["phase_counts"]["review"] == 1
    assert stats["total_input_tokens"] == 300
    assert stats["total_output_tokens"] == 150
    assert stats["total_cached_tokens"] == 10
    assert stats["verdict_counts"]["approve"] == 1


def test_search_runs_by_phase(in_memory_db):
    """Test searching runs by phase."""
    db = in_memory_db

    # Insert runs with different phases (unique IDs)
    phases = ["done", "blocked", "done"]
    for i, phase in enumerate(phases):
        snapshot = RunSnapshot(run_id=f"search-{phase}-{i}", iteration=0, phase=phase)
        db.insert_run(snapshot)

    # Search for DONE
    done_runs = db.search_runs(phase="done")
    assert len(done_runs) == 2

    # Search for BLOCKED
    blocked_runs = db.search_runs(phase="blocked")
    assert len(blocked_runs) == 1


def test_search_runs_by_id_prefix(in_memory_db):
    """Test searching runs by ID prefix."""
    db = in_memory_db

    # Insert runs with different prefixes (unique IDs)
    prefixes = ["prod", "test", "prod"]
    for i, prefix in enumerate(prefixes):
        snapshot = RunSnapshot(run_id=f"{prefix}-run-{i:03d}", iteration=0, phase="done")
        db.insert_run(snapshot)

    # Search by prefix
    prod_runs = db.search_runs(run_id_prefix="prod")
    assert len(prod_runs) == 2

    test_runs = db.search_runs(run_id_prefix="test")
    assert len(test_runs) == 1


# ──────────────────────────────────────────────────────────────────────────────
# State Management Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_sprint8_schema_migration(in_memory_db):
    """Test that Sprint 8 schema (run_states table, active_state_id column) is created."""
    db = in_memory_db

    # Verify run_states table exists
    with db._conn() as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='run_states'"
        )
        assert cursor.fetchone() is not None

        # Verify active_state_id column exists in runs table
        cursor = conn.execute("PRAGMA table_info(runs)")
        columns = [row[1] for row in cursor.fetchall()]
        assert "active_state_id" in columns


def test_insert_state(in_memory_db):
    """Test inserting a run state."""
    db = in_memory_db

    # Insert run first
    snapshot = RunSnapshot(run_id="test-run-001", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert state
    db.insert_state(
        state_id="test-run-001-plan-ready",
        run_id="test-run-001",
        phase_status="plan-ready",
        baseline_commit="abc123",
        notes="Initial state",
    )

    # Verify inserted
    state = db.get_state("test-run-001-plan-ready")
    assert state is not None
    assert state["state_id"] == "test-run-001-plan-ready"
    assert state["run_id"] == "test-run-001"
    assert state["phase_status"] == "plan-ready"
    assert state["baseline_commit"] == "abc123"
    assert state["notes"] == "Initial state"


def test_list_states(in_memory_db):
    """Test listing states for a run."""
    db = in_memory_db

    # Insert run
    snapshot = RunSnapshot(run_id="test-run-002", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert multiple states
    states_to_insert = [
        ("test-run-002-plan-ready", "plan-ready"),
        ("test-run-002-plan-complete", "plan-complete"),
        ("test-run-002-implement-ready", "implement-ready"),
    ]

    for state_id, phase_status in states_to_insert:
        db.insert_state(
            state_id=state_id,
            run_id="test-run-002",
            phase_status=phase_status,
        )

    # List states
    states = db.list_states("test-run-002")
    assert len(states) == 3
    assert states[0]["phase_status"] == "plan-ready"
    assert states[1]["phase_status"] == "plan-complete"
    assert states[2]["phase_status"] == "implement-ready"


def test_active_state(in_memory_db):
    """Test setting and getting active state."""
    db = in_memory_db

    # Insert run
    snapshot = RunSnapshot(run_id="test-run-003", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert state
    db.insert_state(
        state_id="test-run-003-plan-ready",
        run_id="test-run-003",
        phase_status="plan-ready",
    )

    # Set active state
    db.update_active_state("test-run-003", "test-run-003-plan-ready")

    # Get active state
    active = db.get_active_state("test-run-003")
    assert active is not None
    assert active["state_id"] == "test-run-003-plan-ready"


def test_state_with_parent(in_memory_db):
    """Test state with parent relationship."""
    db = in_memory_db

    # Insert run
    snapshot = RunSnapshot(run_id="test-run-004", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert parent state
    db.insert_state(
        state_id="test-run-004-plan-ready",
        run_id="test-run-004",
        phase_status="plan-ready",
    )

    # Insert child state
    db.insert_state(
        state_id="test-run-004-plan-complete",
        run_id="test-run-004",
        phase_status="plan-complete",
        parent_state_id="test-run-004-plan-ready",
    )

    # Verify parent relationship
    state = db.get_state("test-run-004-plan-complete")
    assert state["parent_state_id"] == "test-run-004-plan-ready"


def test_state_with_metadata(in_memory_db):
    """Test state with JSON metadata."""
    db = in_memory_db

    # Insert run
    snapshot = RunSnapshot(run_id="test-run-005", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert state with metadata
    metadata = {
        "branch": "main",
        "state_branch": "duet/state/test-run-005-plan-ready",
        "clean": True,
    }

    db.insert_state(
        state_id="test-run-005-plan-ready",
        run_id="test-run-005",
        phase_status="plan-ready",
        metadata=metadata,
    )

    # Verify metadata
    state = db.get_state("test-run-005-plan-ready")
    assert state["metadata"] == metadata


def test_get_latest_state(in_memory_db):
    """Test getting the latest state for a run."""
    db = in_memory_db

    # Insert run
    snapshot = RunSnapshot(run_id="test-run-006", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert states
    import time

    db.insert_state(
        state_id="test-run-006-state-1",
        run_id="test-run-006",
        phase_status="plan-ready",
    )

    time.sleep(0.01)  # Ensure different timestamps

    db.insert_state(
        state_id="test-run-006-state-2",
        run_id="test-run-006",
        phase_status="plan-complete",
    )

    # Get latest
    latest = db.get_latest_state("test-run-006")
    assert latest["state_id"] == "test-run-006-state-2"


# ──────────────────────────────────────────────────────────────────────────────
# Message Persistence Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_insert_message(in_memory_db):
    """Test inserting a channel message."""
    db = in_memory_db

    # Insert run first
    snapshot = RunSnapshot(run_id="test-run-001", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert message
    db.insert_message(
        run_id="test-run-001",
        channel="plan",
        payload="Step 1: Create endpoint\nStep 2: Add tests",
        iteration=1,
        phase="plan",
        metadata={"schema": "text", "source_phase": "plan"},
    )

    # List messages
    messages = db.list_messages("test-run-001")
    assert len(messages) == 1
    assert messages[0]["channel"] == "plan"
    assert "Step 1" in messages[0]["payload"]


def test_list_messages_filter_by_channel(in_memory_db):
    """Test filtering messages by channel."""
    db = in_memory_db

    snapshot = RunSnapshot(run_id="test-run-002", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert messages for different channels
    db.insert_message(run_id="test-run-002", channel="plan", payload="Plan content")
    db.insert_message(run_id="test-run-002", channel="code", payload="Code content")
    db.insert_message(run_id="test-run-002", channel="plan", payload="Updated plan")

    # Filter by channel
    plan_messages = db.list_messages("test-run-002", channel="plan")
    assert len(plan_messages) == 2
    assert all(m["channel"] == "plan" for m in plan_messages)

    code_messages = db.list_messages("test-run-002", channel="code")
    assert len(code_messages) == 1
    assert code_messages[0]["channel"] == "code"


def test_list_messages_filter_by_phase(in_memory_db):
    """Test filtering messages by phase."""
    db = in_memory_db

    snapshot = RunSnapshot(run_id="test-run-003", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert messages from different phases
    db.insert_message(run_id="test-run-003", channel="plan", payload="Plan", phase="plan")
    db.insert_message(run_id="test-run-003", channel="code", payload="Code", phase="implement")

    # Filter by phase
    plan_phase_messages = db.list_messages("test-run-003", phase="plan")
    assert len(plan_phase_messages) == 1
    assert plan_phase_messages[0]["phase"] == "plan"


def test_get_latest_channel_message(in_memory_db):
    """Test getting the latest message for a channel."""
    db = in_memory_db

    snapshot = RunSnapshot(run_id="test-run-004", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert multiple messages for same channel
    import time

    db.insert_message(run_id="test-run-004", channel="plan", payload="First plan")
    time.sleep(0.01)
    db.insert_message(run_id="test-run-004", channel="plan", payload="Updated plan")

    # Get latest
    latest = db.get_latest_channel_message("test-run-004", "plan")
    assert latest is not None
    assert latest["payload"] == "Updated plan"


def test_get_state_messages(in_memory_db):
    """Test getting messages for a specific state."""
    db = in_memory_db

    snapshot = RunSnapshot(run_id="test-run-005", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert state
    db.insert_state(
        state_id="test-run-005-plan-complete",
        run_id="test-run-005",
        phase_status="plan-complete",
    )

    # Insert messages associated with state
    db.insert_message(
        run_id="test-run-005",
        channel="plan",
        payload="Plan for state",
        state_id="test-run-005-plan-complete",
    )
    db.insert_message(
        run_id="test-run-005",
        channel="task",
        payload="Task input",
        state_id="test-run-005-plan-complete",
    )

    # Get state messages
    state_messages = db.get_state_messages("test-run-005-plan-complete")
    assert len(state_messages) == 2
    channels = {m["channel"] for m in state_messages}
    assert channels == {"plan", "task"}


def test_message_json_payload(in_memory_db):
    """Test storing and retrieving JSON payloads."""
    db = in_memory_db

    snapshot = RunSnapshot(run_id="test-run-006", iteration=0, phase="plan")
    db.insert_run(snapshot)

    # Insert message with dict payload
    payload_dict = {"steps": ["Create endpoint", "Add tests"], "risks": ["API changes"]}
    db.insert_message(
        run_id="test-run-006",
        channel="plan",
        payload=payload_dict,  # Will be JSON-encoded
        metadata={"schema": "json"},
    )

    # Retrieve and verify
    messages = db.list_messages("test-run-006")
    assert len(messages) == 1
    retrieved = messages[0]["payload"]
    assert retrieved["steps"] == ["Create endpoint", "Add tests"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
