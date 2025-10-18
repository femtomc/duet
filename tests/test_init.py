"""
Tests for duet init command.

Tests workspace initialization, directory scaffolding, config generation,
and context discovery.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from rich.console import Console

from duet.init import DuetInitializer, InitError


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_init_creates_directory_structure(temp_workspace):
    """Test that init creates all required directories."""
    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        skip_discovery=True,  # Skip discovery for faster test
        console=Console(),
    )

    initializer.init()

    # Verify directories created
    duet_dir = temp_workspace / ".duet"
    assert duet_dir.exists()
    assert (duet_dir / "runs").exists()
    assert (duet_dir / "logs").exists()
    assert (duet_dir / "context").exists()


def test_init_creates_config_file(temp_workspace):
    """Test that init creates duet.yaml with correct content."""
    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        model_codex="test-codex-model",
        model_claude="test-claude-model",
        skip_discovery=True,
        console=Console(),
    )

    initializer.init()

    config_file = temp_workspace / ".duet" / "duet.yaml"
    assert config_file.exists()

    content = config_file.read_text()
    assert "test-codex-model" in content
    assert "test-claude-model" in content
    assert "workspace_root" in content
    assert "workflow" in content


def test_init_creates_workflow_definition(temp_workspace):
    """Test that init creates workflow definition using DSL."""
    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        skip_discovery=True,
        console=Console(),
    )

    initializer.init()

    workflow_file = temp_workspace / ".duet" / "workflow.py"
    assert workflow_file.exists()

    # Verify content
    content = workflow_file.read_text()
    assert "from duet.dsl import" in content
    assert "workflow = Workflow(" in content
    assert "agents=" in content
    assert "channels=" in content
    assert "phases=" in content
    assert "transitions=" in content

    # Verify it's valid Python and loads successfully
    from duet.workflow_loader import load_workflow
    graph = load_workflow(workflow_path=workflow_file)
    assert graph is not None
    assert len(graph.agents) > 0
    assert len(graph.phases) > 0


def test_init_creates_gitkeep_files(temp_workspace):
    """Test that init creates .gitkeep files."""
    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        skip_discovery=True,
        console=Console(),
    )

    initializer.init()

    duet_dir = temp_workspace / ".duet"
    assert (duet_dir / "logs" / ".gitkeep").exists()
    assert (duet_dir / "runs" / ".gitkeep").exists()
    assert (duet_dir / "context" / ".gitkeep").exists()


def test_init_creates_placeholder_db(temp_workspace):
    """Test that init creates placeholder database file."""
    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        skip_discovery=True,
        console=Console(),
    )

    initializer.init()

    db_path = temp_workspace / ".duet" / "duet.db"
    assert db_path.exists()


def test_init_fails_without_force_when_exists(temp_workspace):
    """Test that init fails if .duet exists without --force."""
    duet_dir = temp_workspace / ".duet"
    duet_dir.mkdir()

    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        force=False,
        skip_discovery=True,
        console=Console(),
    )

    with pytest.raises(InitError) as exc_info:
        initializer.init()

    assert "already exists" in str(exc_info.value)
    assert "--force" in str(exc_info.value)


def test_init_overwrites_with_force(temp_workspace):
    """Test that init overwrites existing .duet with --force."""
    duet_dir = temp_workspace / ".duet"
    duet_dir.mkdir()
    (duet_dir / "old_file.txt").write_text("old content")

    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        force=True,
        skip_discovery=True,
        console=Console(),
    )

    # Should not raise
    initializer.init()

    # Should have created new structure (workflow.py instead of prompts/)
    assert (duet_dir / "workflow.py").exists()
    assert (duet_dir / "duet.yaml").exists()


def test_init_skip_discovery_creates_placeholder(temp_workspace):
    """Test that --skip-discovery creates placeholder context."""
    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        skip_discovery=True,
        console=Console(),
    )

    initializer.init()

    context_file = temp_workspace / ".duet" / "context" / "context.md"
    assert context_file.exists()

    content = context_file.read_text()
    assert "Discovery skipped" in content or "placeholder" in content.lower()


def test_init_context_discovery_success(temp_workspace):
    """Test successful context discovery with Codex."""
    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        model_codex="test-model",
        skip_discovery=False,
        console=Console(),
    )

    # Mock Codex streaming response (Popen - Sprint 6)
    import io
    import json

    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "test"}) + "\n",
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "This is a test project for unit testing.",
            },
        }) + "\n",
        json.dumps({"type": "turn.completed", "usage": {}}) + "\n",
    ]

    mock_process = Mock()
    mock_process.stdout = iter(stdout_lines)
    mock_process.stderr = io.StringIO("")
    mock_process.returncode = 0
    mock_process.wait = Mock(return_value=0)
    mock_process.poll = Mock(return_value=0)
    mock_process.kill = Mock()

    with patch("subprocess.Popen", return_value=mock_process):
        initializer.init()

    context_file = temp_workspace / ".duet" / "context" / "context.md"
    assert context_file.exists()

    content = context_file.read_text()
    assert "test project" in content.lower()


def test_init_context_discovery_cli_failure(temp_workspace):
    """Test that init handles Codex CLI failures gracefully."""
    import io

    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        skip_discovery=False,
        console=Console(),
    )

    # Mock failed Codex CLI (Popen)
    mock_process = Mock()
    mock_process.stdout = iter([])
    mock_process.stderr = io.StringIO("Authentication failed")
    mock_process.returncode = 1
    mock_process.wait = Mock(return_value=1)
    mock_process.poll = Mock(return_value=1)
    mock_process.kill = Mock()

    with patch("subprocess.Popen", return_value=mock_process):
        # Should not raise, should create placeholder instead
        initializer.init()

    context_file = temp_workspace / ".duet" / "context" / "context.md"
    assert context_file.exists()

    content = context_file.read_text()
    assert "skipped" in content.lower() or "failed" in content.lower()


def test_init_custom_config_path(temp_workspace):
    """Test init with custom config path."""
    custom_path = temp_workspace / "custom" / "duet-config"

    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        config_path=custom_path,
        skip_discovery=True,
        console=Console(),
    )

    initializer.init()

    # Verify custom path used (workflow.py instead of prompts/)
    assert custom_path.exists()
    assert (custom_path / "duet.yaml").exists()
    assert (custom_path / "workflow.py").exists()


def test_init_creates_readme(temp_workspace):
    """Test that init creates README.md in .duet/."""
    initializer = DuetInitializer(
        workspace_root=temp_workspace,
        skip_discovery=True,
        console=Console(),
    )

    initializer.init()

    readme = temp_workspace / ".duet" / "README.md"
    assert readme.exists()

    content = readme.read_text()
    assert "Duet Workspace" in content
    assert "Structure" in content
    assert "duet run" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
