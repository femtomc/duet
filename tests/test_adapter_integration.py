"""
Integration tests for adapters with orchestration.

Tests that adapters work correctly when integrated into the full
orchestration loop, using mocked CLI responses.
"""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from rich.console import Console

from duet.artifacts import ArtifactStore
from duet.config import AssistantConfig, DuetConfig, LoggingConfig, StorageConfig, WorkflowConfig
from duet.orchestrator import Orchestrator
from duet.persistence import DuetDatabase


def mock_popen_jsonl(stdout_lines: list[str], returncode: int = 0):
    """Create mock Popen for JSONL streaming."""
    mock_process = Mock()
    mock_process.stdout = iter(stdout_lines)
    mock_process.stderr = io.StringIO("")
    mock_process.returncode = returncode
    mock_process.wait = Mock(return_value=returncode)
    mock_process.poll = Mock(return_value=returncode)
    mock_process.kill = Mock()
    mock_process.communicate = Mock(return_value=("", ""))
    # Support context manager protocol (for subprocess.run compatibility)
    mock_process.__enter__ = Mock(return_value=mock_process)
    mock_process.__exit__ = Mock(return_value=None)
    return mock_process


def create_default_workflow(workspace: Path):
    """Create a default workflow.py for testing with echo adapters."""
    duet_dir = workspace / ".duet"
    duet_dir.mkdir(parents=True, exist_ok=True)

    workflow_content = """
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

# Define channels
task = Channel(name="task", schema="text")
plan_ch = Channel(name="plan", schema="text")
code = Channel(name="code", schema="git_diff")
verdict = Channel(name="verdict", schema="verdict")
feedback = Channel(name="feedback", schema="text")

# Define phases
plan = Phase(
    name="plan",
    agent="planner",
    consumes=[task, feedback],
    publishes=[plan_ch],
    metadata={"role_hint": "planner"},
)

implement = Phase(
    name="implement",
    agent="implementer",
    consumes=[plan_ch],
    publishes=[code],
    metadata={"role_hint": "implementer"},
)

review = Phase(
    name="review",
    agent="reviewer",
    consumes=[plan_ch, code],
    publishes=[verdict, feedback],
    metadata={"role_hint": "reviewer", "replan_transition": True},
)

done = Phase(name="done", agent="reviewer", is_terminal=True)
blocked = Phase(name="blocked", agent="reviewer", is_terminal=True)

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
"""
    (duet_dir / "workflow.py").write_text(workflow_content)


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        create_default_workflow(workspace)
        yield workspace


@pytest.fixture
def temp_artifacts_dir():
    """Create a temporary artifacts directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_orchestration_with_codex_adapter(temp_workspace, temp_artifacts_dir):
    """Test orchestration with mocked Codex adapter."""
    config = DuetConfig(
        codex=AssistantConfig(provider="codex", model="gpt-4"),
        claude=AssistantConfig(provider="echo", model="echo-v1"),
        workflow=WorkflowConfig(max_iterations=1, require_human_approval=False),
        storage=StorageConfig(
            workspace_root=temp_workspace, run_artifact_dir=temp_artifacts_dir
        ),
    )

    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(config, artifact_store, console=console)

    # Mock Codex CLI response (JSONL format) for Popen
    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "test"}) + "\n",
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": "Plan: Implement the feature step by step..."
        }}) + "\n",
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 20}}) + "\n",
    ]
    mock_process = mock_popen_jsonl(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        snapshot = orchestrator.run(run_id="test-codex-integration")

    # Verify orchestration completed
    assert snapshot.run_id == "test-codex-integration"
    assert snapshot.iteration >= 1

    # Verify artifacts were created
    iterations = artifact_store.list_iterations("test-codex-integration")
    assert len(iterations) > 0


def test_orchestration_with_claude_code_adapter(temp_workspace, temp_artifacts_dir):
    """Test orchestration with mocked Claude Code adapter."""
    config = DuetConfig(
        codex=AssistantConfig(provider="echo", model="echo-v1"),
        claude=AssistantConfig(provider="claude-code", model="claude-sonnet-4"),
        workflow=WorkflowConfig(max_iterations=1, require_human_approval=False),
        storage=StorageConfig(
            workspace_root=temp_workspace, run_artifact_dir=temp_artifacts_dir
        ),
    )

    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(config, artifact_store, console=console)

    # Mock Claude Code CLI response (JSON format) for Popen
    json_response = json.dumps({
        "result": "Implementation complete. Modified 3 files.",
        "concluded": False,
        "files_modified": ["src/main.py", "src/utils.py", "tests/test_main.py"],
        "commands_executed": ["pytest"],
    })
    stdout_lines = [json_response + "\n"]
    mock_process = mock_popen_jsonl(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        snapshot = orchestrator.run(run_id="test-claude-integration")

    # Verify orchestration completed
    assert snapshot.run_id == "test-claude-integration"
    assert snapshot.iteration >= 1

    # Verify artifacts were created
    iterations = artifact_store.list_iterations("test-claude-integration")
    assert len(iterations) > 0

    # Verify metadata was captured
    first_iter = artifact_store.load_iteration("test-claude-integration", iterations[0])
    # The IMPLEMENT phase should have the Claude Code response
    if first_iter["phase"] == "implement":
        assert "files_modified" in first_iter["response"]["metadata"]


def test_orchestration_with_both_real_adapters(temp_workspace, temp_artifacts_dir):
    """Test orchestration with both Codex and Claude Code adapters mocked."""
    config = DuetConfig(
        codex=AssistantConfig(provider="codex", model="gpt-4"),
        claude=AssistantConfig(provider="claude-code", model="claude-sonnet-4"),
        workflow=WorkflowConfig(max_iterations=2, require_human_approval=False),
        storage=StorageConfig(
            workspace_root=temp_workspace, run_artifact_dir=temp_artifacts_dir
        ),
    )

    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(config, artifact_store, console=console)

    # Create a mock that returns different responses based on the command (Popen)
    def mock_subprocess_popen(cmd, **kwargs):
        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.wait = Mock(return_value=0)
        mock_process.poll = Mock(return_value=0)
        mock_process.kill = Mock()
        mock_process.communicate = Mock(return_value=("", ""))
        mock_process.stderr = io.StringIO("")
        # Support context manager protocol
        mock_process.__enter__ = Mock(return_value=mock_process)
        mock_process.__exit__ = Mock(return_value=None)

        # Check which CLI is being called
        if "codex" in str(cmd[0]):
            # Codex response (planning/review) - JSONL format
            stdout_lines = [
                json.dumps({"type": "thread.started", "thread_id": "test"}) + "\n",
                json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message",
                    "text": "This is a Codex response for planning/review"
                }}) + "\n",
                json.dumps({"type": "turn.completed", "usage": {}}) + "\n",
            ]
            mock_process.stdout = iter(stdout_lines)
        elif "claude" in str(cmd[0]):
            # Claude Code response (implementation) - JSON with "result" field
            json_response = json.dumps({
                "result": "This is a Claude Code response for implementation",
                "type": "result",
                "subtype": "success"
            })
            mock_process.stdout = iter([json_response + "\n"])
        else:
            # Fallback
            mock_process.stdout = iter([json.dumps({"content": "Unknown CLI", "concluded": False}) + "\n"])

        return mock_process

    with patch("subprocess.Popen", side_effect=mock_subprocess_popen):
        snapshot = orchestrator.run(run_id="test-both-adapters")

    # Verify orchestration completed multiple iterations
    assert snapshot.run_id == "test-both-adapters"
    assert snapshot.iteration >= 1

    # Verify artifacts were created
    iterations = artifact_store.list_iterations("test-both-adapters")
    assert len(iterations) > 0


def test_adapter_error_handling_in_orchestration(temp_workspace, temp_artifacts_dir):
    """Test that empty adapter responses are handled gracefully in orchestration."""
    config = DuetConfig(
        codex=AssistantConfig(provider="echo", model="echo-v1"),
        claude=AssistantConfig(provider="echo", model="echo-v1"),
        workflow=WorkflowConfig(max_iterations=5, require_human_approval=False, require_git_changes=False),
        storage=StorageConfig(
            workspace_root=temp_workspace, run_artifact_dir=temp_artifacts_dir
        ),
    )

    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    orchestrator = Orchestrator(config, artifact_store, console=console)

    # Mock echo adapter to return empty response (simulates adapter failure)
    # Patch the EchoAdapter stream method directly
    from duet.adapters.echo import EchoAdapter
    from duet.models import AssistantResponse

    original_stream = EchoAdapter.stream

    def mock_empty_response(self, request, on_event=None):
        # Return empty response to trigger error handling
        return AssistantResponse(content="", metadata={})

    with patch.object(EchoAdapter, 'stream', mock_empty_response):
        snapshot = orchestrator.run(run_id="test-error-handling")

    # Verify orchestration detected empty response and blocked
    assert snapshot.phase == "blocked"
    assert "empty response" in snapshot.notes.lower()


def test_orchestrator_persists_streaming_events(temp_workspace, temp_artifacts_dir):
    """Test that orchestrator persists streaming events to SQLite."""
    config = DuetConfig(
        codex=AssistantConfig(provider="codex", model="gpt-4"),
        claude=AssistantConfig(provider="echo", model="echo-v1"),
        workflow=WorkflowConfig(max_iterations=1, require_human_approval=False),
        storage=StorageConfig(
            workspace_root=temp_workspace, run_artifact_dir=temp_artifacts_dir
        ),
        logging=LoggingConfig(quiet=True),  # Disable Live display for testing
    )

    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)

    # Create in-memory database for testing
    db = DuetDatabase(":memory:")

    orchestrator = Orchestrator(config, artifact_store, console=console, db=db)

    # Mock Codex streaming response
    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "test-events"}) + "\n",
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": "Plan with streaming events"
        }}) + "\n",
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 100}}) + "\n",
    ]
    mock_process = mock_popen_jsonl(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        snapshot = orchestrator.run(run_id="test-event-persistence")

    # Verify events were persisted to database
    events = db.list_events("test-event-persistence")
    assert len(events) > 0, "No events were persisted to database"

    # Verify event structure
    first_event = events[0]
    assert "event_type" in first_event
    assert "payload" in first_event
    assert "timestamp" in first_event
    assert first_event["run_id"] == "test-event-persistence"

    # Verify specific event types were captured (canonical types)
    event_types = [e["event_type"] for e in events]
    assert "thread_started" in event_types
    assert "assistant_message" in event_types
    assert "turn_complete" in event_types

    # Verify event count matches stream
    assert db.count_events("test-event-persistence") == len(events)


def test_orchestrator_quiet_mode_still_persists_events(temp_workspace, temp_artifacts_dir):
    """Test that quiet mode disables display but still persists events."""
    config = DuetConfig(
        codex=AssistantConfig(provider="codex", model="gpt-4"),
        claude=AssistantConfig(provider="echo", model="echo-v1"),
        workflow=WorkflowConfig(max_iterations=1, require_human_approval=False),
        storage=StorageConfig(
            workspace_root=temp_workspace, run_artifact_dir=temp_artifacts_dir
        ),
        logging=LoggingConfig(quiet=True),  # Enable quiet mode
    )

    console = Console()
    artifact_store = ArtifactStore(temp_artifacts_dir, console=console)
    db = DuetDatabase(":memory:")

    orchestrator = Orchestrator(config, artifact_store, console=console, db=db)

    # Mock Codex response
    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "quiet-test"}) + "\n",
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": "Quiet mode plan"
        }}) + "\n",
    ]
    mock_process = mock_popen_jsonl(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        snapshot = orchestrator.run(run_id="test-quiet-mode")

    # Verify events were still persisted despite quiet mode
    events = db.list_events("test-quiet-mode")
    assert len(events) > 0, "Events should be persisted even in quiet mode"

    # Verify run completed
    assert snapshot.run_id == "test-quiet-mode"
    assert snapshot.iteration >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
