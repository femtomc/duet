"""
Tests for configuration integration with adapters.

Verifies that configuration parameters flow correctly from YAML config
through the orchestrator to the adapters.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from rich.console import Console

from duet.adapters import REGISTRY
from duet.artifacts import ArtifactStore
from duet.config import AssistantConfig, DuetConfig, StorageConfig, WorkflowConfig
from duet.orchestrator import Orchestrator


def test_assistant_config_accepts_timeout_and_cli_path():
    """Test that AssistantConfig accepts and validates timeout and cli_path."""
    config = AssistantConfig(
        provider="codex",
        model="gpt-4",
        timeout=120,
        cli_path="/custom/path/to/codex",
    )

    assert config.provider == "codex"
    assert config.model == "gpt-4"
    assert config.timeout == 120
    assert config.cli_path == "/custom/path/to/codex"


def test_assistant_config_optional_fields():
    """Test that timeout and cli_path are optional."""
    config = AssistantConfig(
        provider="echo",
        model="test-model",
    )

    assert config.timeout is None
    assert config.cli_path is None


def test_adapter_receives_timeout_from_config():
    """Test that timeout flows from config to adapter."""
    config = AssistantConfig(
        provider="codex",
        model="gpt-4",
        timeout=999,
    )

    # Build adapter with unpacked config
    adapter = REGISTRY.resolve(config.provider, **config.dict(), workspace_root=".")

    assert adapter.timeout == 999


def test_adapter_receives_cli_path_from_config():
    """Test that cli_path flows from config to adapter."""
    config = AssistantConfig(
        provider="claude-code",
        model="claude-sonnet-4",
        cli_path="/my/custom/claude",
    )

    # Build adapter with unpacked config
    adapter = REGISTRY.resolve(config.provider, **config.dict(), workspace_root="/workspace")

    assert adapter.cli_path == "/my/custom/claude"


def test_adapter_uses_defaults_when_fields_omitted():
    """Test that adapters use default values when timeout/cli_path not specified."""
    config = AssistantConfig(
        provider="codex",
        model="gpt-4",
    )

    # Build adapter with unpacked config
    adapter = REGISTRY.resolve(config.provider, **config.dict(), workspace_root=".")

    # Should use default timeout
    assert adapter.timeout == 300  # Codex default
    assert adapter.cli_path == "codex"  # Default CLI name


def test_claude_adapter_receives_workspace_root():
    """Test that Claude Code adapter receives workspace_root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        config = AssistantConfig(
            provider="claude-code",
            model="claude-sonnet-4",
        )

        # Build adapter with workspace_root
        adapter = REGISTRY.resolve(config.provider, **config.dict(), workspace_root=str(workspace))

        assert adapter.workspace_root == str(workspace)


def test_orchestrator_passes_workspace_to_claude_adapter():
    """Test that orchestrator passes workspace_root to Claude adapter."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            workspace = Path(tmpdir_workspace)
            artifacts = Path(tmpdir_artifacts)

            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="claude-code", model="claude-sonnet-4"),
                workflow=WorkflowConfig(max_iterations=1),
                storage=StorageConfig(
                    workspace_root=workspace,
                    run_artifact_dir=artifacts,
                ),
            )

            console = Console()
            artifact_store = ArtifactStore(artifacts, console=console)
            orchestrator = Orchestrator(config, artifact_store, console=console)

            # Verify Claude adapter received workspace_root
            assert orchestrator.claude_adapter.workspace_root == str(workspace)


def test_orchestrator_passes_all_config_params_to_adapters():
    """Test that orchestrator passes all config parameters to adapters."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            workspace = Path(tmpdir_workspace)
            artifacts = Path(tmpdir_artifacts)

            config = DuetConfig(
                codex=AssistantConfig(
                    provider="codex",
                    model="gpt-4-custom",
                    timeout=250,
                    cli_path="/custom/codex",
                ),
                claude=AssistantConfig(
                    provider="claude-code",
                    model="claude-custom",
                    timeout=500,
                    cli_path="/custom/claude",
                ),
                workflow=WorkflowConfig(max_iterations=1),
                storage=StorageConfig(
                    workspace_root=workspace,
                    run_artifact_dir=artifacts,
                ),
            )

            console = Console()
            artifact_store = ArtifactStore(artifacts, console=console)
            orchestrator = Orchestrator(config, artifact_store, console=console)

            # Verify Codex adapter
            assert orchestrator.codex_adapter.model == "gpt-4-custom"
            assert orchestrator.codex_adapter.timeout == 250
            assert orchestrator.codex_adapter.cli_path == "/custom/codex"

            # Verify Claude adapter
            assert orchestrator.claude_adapter.model == "claude-custom"
            assert orchestrator.claude_adapter.timeout == 500
            assert orchestrator.claude_adapter.cli_path == "/custom/claude"
            assert orchestrator.claude_adapter.workspace_root == str(workspace)


def test_echo_adapter_ignores_extra_kwargs():
    """Test that echo adapter doesn't break with extra kwargs."""
    config = AssistantConfig(
        provider="echo",
        model="test-model",
        timeout=100,
        cli_path="/unused",
    )

    # Echo adapter should accept but ignore these params
    adapter = REGISTRY.resolve(config.provider, **config.dict(), workspace_root="/unused")

    # Should not raise any errors
    assert adapter.name == "echo"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
