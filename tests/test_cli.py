"""
Unit tests for CLI commands (Sprint 6 filtering and export features).

Tests CLI command behavior using in-memory database and mocked artifacts.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from duet.cli import app
from duet.models import Phase, ReviewVerdict, RunSnapshot
from duet.persistence import DuetDatabase

runner = CliRunner()


@pytest.fixture
def temp_config_dir():
    """Create temporary config directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / ".duet"
        config_dir.mkdir()
        (config_dir / "runs").mkdir()
        yield config_dir


@pytest.fixture
def sample_db(temp_config_dir):
    """Create file-based database with sample data."""
    db_path = temp_config_dir / "duet.db"
    db = DuetDatabase(str(db_path))

    # Insert sample runs
    from duet.models import RunSnapshot
    import datetime as dt

    # Run 1: Blocked in plan phase (October 15)
    snapshot1 = RunSnapshot(
        run_id="run-blocked-001",
        iteration=2,
        phase=Phase.BLOCKED,
        created_at=dt.datetime(2025, 10, 15, 10, 0, 0, tzinfo=dt.timezone.utc),
        metadata={
            "started_at": "2025-10-15T10:00:00Z",
            "completed_at": "2025-10-15T10:15:00Z",
            "original_branch": "main",
            "consecutive_replans": 0,
        },
        notes="Timeout during planning",
    )
    db.insert_run(snapshot1)
    db.insert_iteration(
        run_id="run-blocked-001",
        iteration=1,
        phase=Phase.PLAN,
        prompt="Create a plan",
        response_content="Planning...",
    )

    # Run 2: Completed with approve verdict (October 16)
    snapshot2 = RunSnapshot(
        run_id="run-approved-002",
        iteration=3,
        phase=Phase.DONE,
        created_at=dt.datetime(2025, 10, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        metadata={
            "started_at": "2025-10-16T14:00:00Z",
            "completed_at": "2025-10-16T14:30:00Z",
            "original_branch": "main",
            "consecutive_replans": 0,
        },
        notes="Successfully completed",
    )
    db.insert_run(snapshot2)
    db.insert_iteration(
        run_id="run-approved-002",
        iteration=3,
        phase=Phase.REVIEW,
        prompt="Review changes",
        response_content="Looks good",
        verdict=ReviewVerdict.APPROVE,
    )

    # Run 3: In progress (October 17)
    snapshot3 = RunSnapshot(
        run_id="run-inprogress-003",
        iteration=1,
        phase=Phase.IMPLEMENT,
        created_at=dt.datetime(2025, 10, 17, 9, 0, 0, tzinfo=dt.timezone.utc),
        metadata={
            "started_at": "2025-10-17T09:00:00Z",
            "original_branch": "main",
            "consecutive_replans": 0,
        },
    )
    db.insert_run(snapshot3)

    # Insert some streaming events for run 2
    db.insert_event(
        run_id="run-approved-002",
        event_type="thread.started",
        payload={"thread_id": "abc123"},
        iteration=1,
        phase="plan",
        timestamp="2025-10-16T14:00:05Z",
    )
    db.insert_event(
        run_id="run-approved-002",
        event_type="item.completed",
        payload={"item": {"type": "agent_message", "text": "Here is the plan"}},
        iteration=1,
        phase="plan",
        timestamp="2025-10-16T14:00:10Z",
    )
    db.insert_event(
        run_id="run-approved-002",
        event_type="turn.completed",
        payload={"usage": {"input_tokens": 100, "output_tokens": 50}},
        iteration=1,
        phase="plan",
        timestamp="2025-10-16T14:00:15Z",
    )

    return db


@pytest.fixture
def mock_config(temp_config_dir, sample_db):
    """Mock config file (database already created by sample_db fixture)."""
    # Create minimal config
    config_content = f"""
codex:
  provider: "echo"
  model: "gpt-4"

claude:
  provider: "echo"
  model: "claude-sonnet-4"

storage:
  workspace_root: "{temp_config_dir.parent}"
  run_artifact_dir: "{temp_config_dir / 'runs'}"

workflow:
  max_iterations: 5

logging:
  enable_jsonl: false
  quiet: false
"""
    config_file = temp_config_dir / "duet.yaml"
    config_file.write_text(config_content)

    db_path = temp_config_dir / "duet.db"
    return config_file, db_path


# ──────────────────────────────────────────────────────────────────────────────
# duet history Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_history_basic_listing(mock_config):
    """Test duet history lists runs."""
    config_file, db_path = mock_config

    result = runner.invoke(app, ["history", "--config", str(config_file)])

    assert result.exit_code == 0
    # Check for partial matches (IDs may be truncated in table)
    assert "run-blocked" in result.stdout
    assert "run-approved" in result.stdout
    assert "run-inprogress" in result.stdout
    assert "Showing 3 runs" in result.stdout


def test_history_filter_by_phase(mock_config):
    """Test duet history --phase filter."""
    config_file, db_path = mock_config

    result = runner.invoke(app, ["history", "--config", str(config_file), "--phase", "blocked"])

    assert result.exit_code == 0
    assert "run-blocked" in result.stdout
    assert "run-approved" not in result.stdout
    assert "Showing 1 run" in result.stdout


def test_history_filter_by_verdict(mock_config):
    """Test duet history --verdict filter."""
    config_file, db_path = mock_config

    result = runner.invoke(app, ["history", "--config", str(config_file), "--verdict", "approve"])

    assert result.exit_code == 0
    assert "run-approved" in result.stdout
    assert "run-blocked" not in result.stdout
    assert "Showing 1 run" in result.stdout


def test_history_filter_by_date_since(mock_config):
    """Test duet history --since filter."""
    config_file, db_path = mock_config

    # Filter for runs from October 17 onwards (should only get run-inprogress-003)
    result = runner.invoke(app, ["history", "--config", str(config_file), "--since", "2025-10-17"])

    assert result.exit_code == 0
    # Should include only the October 17 run
    assert "run-inprogress" in result.stdout
    # Should exclude earlier runs
    assert "run-blocked" not in result.stdout
    assert "run-approved" not in result.stdout
    assert "Showing 1 run" in result.stdout


def test_history_filter_by_contains(mock_config):
    """Test duet history --contains filter."""
    config_file, db_path = mock_config

    result = runner.invoke(app, ["history", "--config", str(config_file), "--contains", "timeout"])

    assert result.exit_code == 0
    assert "run-blocked" in result.stdout  # Contains "Timeout during planning"
    assert "run-approved" not in result.stdout
    assert "Showing 1 run" in result.stdout


def test_history_json_export(mock_config):
    """Test duet history --format json."""
    config_file, db_path = mock_config

    result = runner.invoke(app, ["history", "--config", str(config_file), "--format", "json"])

    assert result.exit_code == 0

    # Parse JSON output
    output_data = json.loads(result.stdout)
    assert isinstance(output_data, list)
    assert len(output_data) >= 3

    # Verify structure
    run_ids = [r["run_id"] for r in output_data]
    assert "run-blocked-001" in run_ids
    assert "run-approved-002" in run_ids


def test_history_combined_filters(mock_config):
    """Test duet history with multiple filters."""
    config_file, db_path = mock_config

    result = runner.invoke(
        app,
        [
            "history",
            "--config", str(config_file),
            "--phase", "done",
            "--verdict", "approve",
            "--since", "2025-10-01",
        ],
    )

    assert result.exit_code == 0
    assert "run-approved" in result.stdout
    assert "run-blocked" not in result.stdout
    assert "Showing 1 run" in result.stdout


# ──────────────────────────────────────────────────────────────────────────────
# duet inspect Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_inspect_basic(mock_config):
    """Test duet inspect shows run details."""
    config_file, db_path = mock_config

    result = runner.invoke(app, ["inspect", "run-approved-002", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "run-approved-002" in result.stdout
    assert "DONE" in result.stdout
    assert "Iterations:" in result.stdout


def test_inspect_shows_events_by_default(mock_config):
    """Test duet inspect shows events by default."""
    config_file, db_path = mock_config

    result = runner.invoke(app, ["inspect", "run-approved-002", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "Streaming Events:" in result.stdout
    assert "thread.started" in result.stdout
    assert "item.completed" in result.stdout
    assert "turn.completed" in result.stdout


def test_inspect_no_events_hides_timeline(mock_config):
    """Test duet inspect --no-events hides event timeline."""
    config_file, db_path = mock_config

    result = runner.invoke(
        app, ["inspect", "run-approved-002", "--config", str(config_file), "--no-events"]
    )

    assert result.exit_code == 0
    assert "Streaming Events:" not in result.stdout
    assert "thread.started" not in result.stdout


def test_inspect_json_export(mock_config):
    """Test duet inspect --output json."""
    config_file, db_path = mock_config

    result = runner.invoke(
        app, ["inspect", "run-approved-002", "--config", str(config_file), "--output", "json"]
    )

    assert result.exit_code == 0

    # Parse JSON output
    output_data = json.loads(result.stdout)
    assert "run" in output_data
    assert "iterations" in output_data
    assert "events" in output_data
    assert "statistics" in output_data

    # Verify structure
    assert output_data["run"]["run_id"] == "run-approved-002"
    assert len(output_data["events"]) == 3  # 3 events inserted
    assert output_data["statistics"]["total_output_tokens"] >= 0


def test_inspect_json_without_events(mock_config):
    """Test duet inspect --output json --no-events."""
    config_file, db_path = mock_config

    result = runner.invoke(
        app,
        ["inspect", "run-approved-002", "--config", str(config_file), "--output", "json", "--no-events"],
    )

    assert result.exit_code == 0

    # Parse JSON output
    output_data = json.loads(result.stdout)
    assert output_data["events"] == []  # No events when --no-events


def test_inspect_nonexistent_run(mock_config):
    """Test duet inspect with nonexistent run."""
    config_file, db_path = mock_config

    result = runner.invoke(app, ["inspect", "nonexistent-run", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "not found" in result.stdout.lower()


# ──────────────────────────────────────────────────────────────────────────────
# duet run --quiet Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_run_quiet_flag_sets_config(temp_config_dir):
    """Test that duet run --quiet sets logging.quiet config."""
    # Create minimal config
    config_content = f"""
codex:
  provider: "echo"
  model: "gpt-4"

claude:
  provider: "echo"
  model: "claude-sonnet-4"

storage:
  workspace_root: "{temp_config_dir.parent}"
  run_artifact_dir: "{temp_config_dir / 'runs'}"

workflow:
  max_iterations: 1

logging:
  quiet: false
"""
    config_file = temp_config_dir / "duet.yaml"
    config_file.write_text(config_content)

    # Create empty database
    db_path = temp_config_dir / "duet.db"
    db = DuetDatabase(str(db_path))

    # Mock the orchestrator.run to avoid actual execution
    with patch("duet.cli.Orchestrator") as MockOrchestrator:
        mock_orch = MockOrchestrator.return_value
        mock_snapshot = RunSnapshot(
            run_id="test-quiet", iteration=1, phase=Phase.DONE
        )
        mock_orch.run.return_value = mock_snapshot

        # Run with --quiet flag
        result = runner.invoke(app, ["run", "--config", str(config_file), "--quiet"])

        assert result.exit_code == 0

        # Verify orchestrator was created with quiet=True
        MockOrchestrator.assert_called_once()
        call_args = MockOrchestrator.call_args
        config_passed = call_args[0][0]  # First positional arg is DuetConfig
        assert config_passed.logging.quiet is True


# ──────────────────────────────────────────────────────────────────────────────
# Sprint 12: Lint Command Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestLintCommand:
    """Tests for the duet lint command."""

    def test_lint_valid_workflow(self, temp_config_dir):
        """Test that lint succeeds on valid workflow."""
        # Create valid workflow
        workflow_file = temp_config_dir / "workflow.py"
        workflow_file.write_text("""
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    agents=[Agent(name="planner", provider="echo", model="test")],
    channels=[Channel(name="task"), Channel(name="plan")],
    phases=[
        Phase(name="plan", agent="planner", consumes=["task"], publishes=["plan"]),
        Phase(name="done", agent="planner", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="done"),
    ],
)
""")

        # Create minimal config
        config_file = temp_config_dir / "duet.yaml"
        config_file.write_text(f"""
storage:
  workspace_root: "{temp_config_dir.parent}"
  run_artifact_dir: "{temp_config_dir / 'runs'}"
""")

        # Run lint
        result = runner.invoke(app, ["lint", "--config", str(config_file), "--workflow", str(workflow_file)])

        assert result.exit_code == 0
        assert "validation succeeded" in result.stdout.lower() or "✓" in result.stdout

    def test_lint_invalid_workflow_no_phases(self, temp_config_dir):
        """Test that lint fails on workflow with no phases."""
        # Create invalid workflow
        workflow_file = temp_config_dir / "workflow.py"
        workflow_file.write_text("""
from duet.dsl import Workflow

workflow = Workflow(
    agents=[],
    channels=[],
    phases=[],  # Invalid: no phases
    transitions=[],
)
""")

        config_file = temp_config_dir / "duet.yaml"
        config_file.write_text(f"""
storage:
  workspace_root: "{temp_config_dir.parent}"
  run_artifact_dir: "{temp_config_dir / 'runs'}"
""")

        # Run lint (should fail)
        result = runner.invoke(app, ["lint", "--config", str(config_file), "--workflow", str(workflow_file)])

        assert result.exit_code != 0
        assert "validation failed" in result.stdout.lower() or "error" in result.stdout.lower()

    def test_lint_unknown_channel(self, temp_config_dir):
        """Test that lint catches references to unknown channels."""
        # Create workflow with unknown channel reference
        workflow_file = temp_config_dir / "workflow.py"
        workflow_file.write_text("""
from duet.dsl import Agent, Channel, Phase, Transition, Workflow

workflow = Workflow(
    agents=[Agent(name="planner", provider="echo", model="test")],
    channels=[Channel(name="task")],  # Only task channel defined
    phases=[
        Phase(name="plan", agent="planner", consumes=["task"], publishes=["unknown_channel"]),  # References undefined channel
    ],
    transitions=[],
)
""")

        config_file = temp_config_dir / "duet.yaml"
        config_file.write_text(f"""
storage:
  workspace_root: "{temp_config_dir.parent}"
  run_artifact_dir: "{temp_config_dir / 'runs'}"
""")

        # Run lint (should fail)
        result = runner.invoke(app, ["lint", "--config", str(config_file), "--workflow", str(workflow_file)])

        assert result.exit_code != 0
        assert "unknown channel" in result.stdout.lower() or "error" in result.stdout.lower()

    def test_lint_syntax_error(self, temp_config_dir):
        """Test that lint catches Python syntax errors."""
        # Create workflow with syntax error
        workflow_file = temp_config_dir / "workflow.py"
        workflow_file.write_text("""
from duet.dsl import Workflow

workflow = Workflow(
    agents=[,  # Syntax error: empty element
    channels=[],
    phases=[],
    transitions=[],
)
""")

        config_file = temp_config_dir / "duet.yaml"
        config_file.write_text(f"""
storage:
  workspace_root: "{temp_config_dir.parent}"
  run_artifact_dir: "{temp_config_dir / 'runs'}"
""")

        # Run lint (should fail)
        result = runner.invoke(app, ["lint", "--config", str(config_file), "--workflow", str(workflow_file)])

        assert result.exit_code != 0

    def test_lint_missing_workflow_file(self, temp_config_dir):
        """Test that lint fails gracefully when workflow file doesn't exist."""
        config_file = temp_config_dir / "duet.yaml"
        config_file.write_text(f"""
storage:
  workspace_root: "{temp_config_dir.parent}"
  run_artifact_dir: "{temp_config_dir / 'runs'}"
""")

        nonexistent_workflow = temp_config_dir / "nonexistent.py"

        # Run lint (should fail)
        result = runner.invoke(app, ["lint", "--config", str(config_file), "--workflow", str(nonexistent_workflow)])

        assert result.exit_code != 0
        assert "not found" in result.stdout.lower() or "error" in result.stdout.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
