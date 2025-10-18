"""
Tests for SQLite persistence layer.

Tests database operations using in-memory SQLite for fast, isolated testing.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import pytest

from duet.models import Phase, ReviewVerdict, RunSnapshot
from duet.persistence import DuetDatabase


@pytest.fixture
def in_memory_db():
    """Create an in-memory SQLite database for testing."""
    return DuetDatabase(":memory:")


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
        phase=Phase.PLAN,
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
        phase=Phase.PLAN,
        notes="Initial",
    )

    db.insert_run(snapshot)

    # Update
    snapshot.phase = Phase.DONE
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
    snapshot = RunSnapshot(run_id="test-upsert", iteration=0, phase=Phase.PLAN)

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
    snapshot = RunSnapshot(run_id="test-run-iter", iteration=0, phase=Phase.PLAN)
    db.insert_run(snapshot)

    # Insert iteration
    db.insert_iteration(
        run_id="test-run-iter",
        iteration=1,
        phase=Phase.PLAN,
        prompt="Create a plan",
        response_content="Here is the plan...",
        verdict=None,
        concluded=False,
        next_phase=Phase.IMPLEMENT,
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
    snapshot = RunSnapshot(run_id="test-verdict", iteration=0, phase=Phase.REVIEW)
    db.insert_run(snapshot)

    db.insert_iteration(
        run_id="test-verdict",
        iteration=1,
        phase=Phase.REVIEW,
        prompt="Review changes",
        response_content="Approved",
        verdict=ReviewVerdict.APPROVE,
        concluded=True,
        next_phase=Phase.DONE,
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
            phase=Phase.DONE if i % 2 == 0 else Phase.BLOCKED,
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
    snapshot = RunSnapshot(run_id="test-iters", iteration=0, phase=Phase.PLAN)
    db.insert_run(snapshot)

    # Insert multiple iterations
    for i in range(3):
        db.insert_iteration(
            run_id="test-iters",
            iteration=i + 1,
            phase=Phase.PLAN,
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
    snapshot = RunSnapshot(run_id="test-stats", iteration=0, phase=Phase.PLAN)
    db.insert_run(snapshot)

    # Insert iterations with various metadata
    db.insert_iteration(
        run_id="test-stats",
        iteration=1,
        phase=Phase.PLAN,
        prompt="Plan",
        response_content="Plan response",
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 10},
    )

    db.insert_iteration(
        run_id="test-stats",
        iteration=1,
        phase=Phase.IMPLEMENT,
        prompt="Implement",
        response_content="Implemented",
        usage_metadata={"input_tokens": 200, "output_tokens": 100},
    )

    db.insert_iteration(
        run_id="test-stats",
        iteration=1,
        phase=Phase.REVIEW,
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
    phases = [Phase.DONE, Phase.BLOCKED, Phase.DONE]
    for i, phase in enumerate(phases):
        snapshot = RunSnapshot(run_id=f"search-{phase.value}-{i}", iteration=0, phase=phase)
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
        snapshot = RunSnapshot(run_id=f"{prefix}-run-{i:03d}", iteration=0, phase=Phase.DONE)
        db.insert_run(snapshot)

    # Search by prefix
    prod_runs = db.search_runs(run_id_prefix="prod")
    assert len(prod_runs) == 2

    test_runs = db.search_runs(run_id_prefix="test")
    assert len(test_runs) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
