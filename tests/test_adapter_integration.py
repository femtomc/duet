"""
Integration tests for adapters with orchestration.

Tests that adapters work correctly when integrated into the full
orchestration loop, using mocked CLI responses.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from rich.console import Console

from duet.artifacts import ArtifactStore
from duet.config import AssistantConfig, DuetConfig, StorageConfig, WorkflowConfig
from duet.orchestrator import Orchestrator


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


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

    # Mock Codex CLI response (JSONL format)
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "test"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": "Plan: Implement the feature step by step..."
        }}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 20}})
    ])
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
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

    # Mock Claude Code CLI response
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(
        {
            "content": "Implementation complete. Modified 3 files.",
            "concluded": False,
            "files_modified": ["src/main.py", "src/utils.py", "tests/test_main.py"],
            "commands_executed": ["pytest"],
        }
    )
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
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

    # Create a mock that returns different responses based on the command
    def mock_subprocess_run(cmd, **kwargs):
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        # Check which CLI is being called
        if "codex" in str(cmd[0]):
            # Codex response (planning/review) - JSONL format
            mock_result.stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "test"}),
                json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message",
                    "text": "This is a Codex response for planning/review"
                }}),
                json.dumps({"type": "turn.completed", "usage": {}})
            ])
        elif "claude" in str(cmd[0]):
            # Claude Code response (implementation) - JSON with "result" field
            mock_result.stdout = json.dumps(
                {
                    "result": "This is a Claude Code response for implementation",
                    "type": "result",
                    "subtype": "success"
                }
            )
        else:
            # Fallback
            mock_result.stdout = json.dumps({"content": "Unknown CLI", "concluded": False})

        return mock_result

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        snapshot = orchestrator.run(run_id="test-both-adapters")

    # Verify orchestration completed multiple iterations
    assert snapshot.run_id == "test-both-adapters"
    assert snapshot.iteration >= 1

    # Verify artifacts were created
    iterations = artifact_store.list_iterations("test-both-adapters")
    assert len(iterations) > 0


def test_adapter_error_handling_in_orchestration(temp_workspace, temp_artifacts_dir):
    """Test that adapter errors are handled gracefully in orchestration."""
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

    # Mock CLI failure
    mock_result = Mock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Authentication error"

    with patch("subprocess.run", return_value=mock_result):
        snapshot = orchestrator.run(run_id="test-error-handling")

    # Verify orchestration was blocked due to adapter error
    from duet.models import Phase

    assert snapshot.phase == Phase.BLOCKED
    assert "Adapter failure" in snapshot.notes or "error" in snapshot.notes.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
