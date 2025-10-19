"""
Acceptance tests for Duet orchestration.

These tests exercise the full orchestration loop using the echo adapter
to verify state transitions, artifact outputs, and edge case handling.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from rich.console import Console

from duet.artifacts import ArtifactStore
from duet.config import AssistantConfig, DuetConfig, LoggingConfig, StorageConfig, WorkflowConfig
from duet.orchestrator import Orchestrator


def create_test_workflow(workspace: Path) -> None:
    """Create default workflow.py for Sprint 10 tests."""
    duet_dir = workspace / ".duet"
    duet_dir.mkdir(parents=True, exist_ok=True)
    workflow_file = duet_dir / "workflow.py"
    workflow_file.write_text("""
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

# Define channels
task = Channel(name="task")
plan_ch = Channel(name="plan")
code = Channel(name="code")
verdict = Channel(name="verdict")
feedback = Channel(name="feedback")

# Define phases with step-based facet syntax
plan = (
    Phase(name="plan", agent="planner")
    .read(task, feedback)
    .call_agent("planner", writes=[plan_ch], role="planner")
)

implement = (
    Phase(name="implement", agent="implementer")
    .read(plan_ch)
    .call_agent("implementer", writes=[code], role="implementer")
)

review = (
    Phase(name="review", agent="reviewer")
    .read(plan_ch, code)
    .call_agent("reviewer", writes=[verdict, feedback], role="reviewer")
)

done = Phase.terminal_phase("done", "reviewer")
blocked = Phase.terminal_phase("blocked", "reviewer")

# Define workflow
workflow = Workflow(
    agents=[
        Agent(name="planner", provider="echo", model="echo-v1"),
        Agent(name="implementer", provider="echo", model="echo-v1"),
        Agent(name="reviewer", provider="echo", model="echo-v1"),
    ],
    channels=[task, plan_ch, code, verdict, feedback],
    phases=[plan, implement, review, done, blocked],
    transitions=[
        Transition(from_phase=plan, to_phase=implement),
        Transition(from_phase=implement, to_phase=review),
        Transition(from_phase=review, to_phase=done, when=When.channel_has(verdict, "approve")),
        Transition(from_phase=review, to_phase=plan, when=When.channel_has(verdict, "changes_requested")),
        Transition(from_phase=review, to_phase=blocked, when=When.channel_has(verdict, "blocked")),
    ],
    initial_phase=plan,
    task_channel=task,
)
""")


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        create_test_workflow(workspace)
        yield workspace


@pytest.fixture
def temp_artifacts_dir():
    """Create a temporary artifacts directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def echo_config(temp_workspace, temp_artifacts_dir):
    """Create a test configuration using echo adapters."""
    return DuetConfig(
        codex=AssistantConfig(
            provider="echo",
            model="echo-v1",
        ),
        claude=AssistantConfig(
            provider="echo",
            model="echo-v1",
        ),
        workflow=WorkflowConfig(
            max_iterations=3,
            require_human_approval=False,
        ),
        storage=StorageConfig(
            workspace_root=temp_workspace,
            run_artifact_dir=temp_artifacts_dir,
        ),
        logging=LoggingConfig(
            enable_jsonl=True,
            jsonl_dir=temp_artifacts_dir / "logs",
        ),
    )


def test_basic_orchestration_loop(echo_config, temp_artifacts_dir):
    """Test that the orchestration loop runs with echo adapters."""
    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(echo_config, artifact_store, console=console)

    # Run orchestration
    snapshot = orchestrator.run(run_id="test-basic-run")

    # Assertions
    assert snapshot.run_id == "test-basic-run"
    # Echo adapter auto-approves for reviewer roles, so workflow completes
    assert snapshot.phase == "done"
    assert snapshot.iteration >= 1

    # Verify checkpoint was created
    checkpoint = artifact_store.load_checkpoint("test-basic-run")
    assert checkpoint is not None
    assert checkpoint.run_id == "test-basic-run"
    assert checkpoint.phase == "done"


def test_artifact_persistence(echo_config, temp_artifacts_dir):
    """Test that artifacts are persisted correctly."""
    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(echo_config, artifact_store, console=console)

    snapshot = orchestrator.run(run_id="test-artifacts")

    # Check that iterations were persisted
    iterations = artifact_store.list_iterations("test-artifacts")
    assert len(iterations) > 0, "Should have persisted iteration records"

    # Load and verify iteration structure
    first_iter = artifact_store.load_iteration("test-artifacts", iterations[0])
    assert "timestamp" in first_iter
    assert "iteration" in first_iter
    assert "phase" in first_iter
    assert "request" in first_iter
    assert "response" in first_iter
    assert "decision" in first_iter

    # Verify request structure
    request = first_iter["request"]
    assert "role" in request
    assert "prompt" in request
    assert "context" in request

    # Verify response structure
    response = first_iter["response"]
    assert "content" in response
    assert "metadata" in response

    # Verify decision structure
    decision = first_iter["decision"]
    assert "next_phase" in decision
    assert "rationale" in decision


def test_run_summary_generation(echo_config, temp_artifacts_dir):
    """Test that run summaries are generated correctly."""
    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(echo_config, artifact_store, console=console)

    snapshot = orchestrator.run(run_id="test-summary")

    # Verify summary was created
    summary_path = temp_artifacts_dir / "test-summary" / "summary.json"
    assert summary_path.exists(), "Summary file should be created"

    # Load and verify summary structure
    summary = artifact_store.generate_run_summary("test-summary")
    assert summary["run_id"] == "test-summary"
    assert "checkpoint" in summary
    assert "iterations" in summary
    assert "statistics" in summary

    # Verify statistics
    stats = summary["statistics"]
    assert stats["total_iterations"] > 0
    assert "phase_counts" in stats
    # Echo adapter auto-approves, so workflow completes
    assert stats["final_phase"] == "done"


def test_jsonl_logging(echo_config, temp_artifacts_dir):
    """Test that JSONL logging works when enabled."""
    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(echo_config, artifact_store, console=console)

    snapshot = orchestrator.run(run_id="test-logging")

    # Verify JSONL log file was created
    log_file = temp_artifacts_dir / "logs" / "duet.jsonl"
    assert log_file.exists(), "JSONL log file should be created"

    # Read and verify log entries
    import json

    with log_file.open("r") as f:
        logs = [json.loads(line) for line in f if line.strip()]

    assert len(logs) > 0, "Should have logged events"

    # Verify log structure
    for log in logs:
        assert "timestamp" in log
        assert "event" in log
        assert "level" in log

    # Verify expected events
    events = [log["event"] for log in logs]
    assert "iteration_start" in events
    assert "state_transition" in events
    assert "run_complete" in events


def test_max_iterations_edge_case(temp_workspace, temp_artifacts_dir):
    """Test that max iterations limit is enforced."""
    config = DuetConfig(
        codex=AssistantConfig(provider="echo", model="echo-v1"),
        claude=AssistantConfig(provider="echo", model="echo-v1"),
        workflow=WorkflowConfig(
            max_iterations=1,  # Only 1 iteration
            require_human_approval=False,
        ),
        storage=StorageConfig(
            workspace_root=temp_workspace,
            run_artifact_dir=temp_artifacts_dir,
        ),
    )

    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(config, artifact_store, console=console)

    snapshot = orchestrator.run(run_id="test-max-iter")

    assert snapshot.iteration == 1
    assert snapshot.phase == "blocked"
    assert "Max iterations" in snapshot.notes


def test_state_transitions(echo_config, temp_artifacts_dir):
    """Test that state transitions follow expected flow."""
    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(echo_config, artifact_store, console=console)

    snapshot = orchestrator.run(run_id="test-transitions")

    # Load iterations and verify phase progression
    iterations = artifact_store.list_iterations("test-transitions")
    phases = []
    for iter_file in iterations:
        record = artifact_store.load_iteration("test-transitions", iter_file)
        phases.append(record["phase"])

    # First iteration should be: PLAN
    # Then each iteration should cycle: PLAN → IMPLEMENT → REVIEW → PLAN (loop)
    assert phases[0] == "plan", "First phase should be PLAN"

    # Verify we see expected phase transitions
    assert "plan" in phases
    assert "implement" in phases
    assert "review" in phases


def test_checkpoint_resumability(echo_config, temp_artifacts_dir):
    """Test that checkpoints contain sufficient data for resumption."""
    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(echo_config, artifact_store, console=console)

    snapshot = orchestrator.run(run_id="test-checkpoint")

    # Load checkpoint
    checkpoint = artifact_store.load_checkpoint("test-checkpoint")

    # Verify checkpoint has required fields for resumption
    assert checkpoint.run_id == "test-checkpoint"
    assert checkpoint.iteration >= 0
    assert checkpoint.phase in ["blocked", "done"]
    assert checkpoint.created_at is not None
    assert "started_at" in checkpoint.metadata
    assert "completed_at" in checkpoint.metadata


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
