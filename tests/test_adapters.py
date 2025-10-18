"""
Unit tests for adapter implementations.

Tests adapter functionality using mocked CLI responses to avoid
requiring actual Codex/Claude Code installations.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from duet.adapters import ClaudeCodeAdapter, CodexAdapter, EchoAdapter, REGISTRY
from duet.adapters.claude_code import ClaudeCodeError
from duet.adapters.codex import CodexError
from duet.models import AssistantRequest


# ──────────────────────────────────────────────────────────────────────────────
# Registry Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_adapter_registry_contains_all_adapters():
    """Test that all adapters are registered."""
    assert "echo" in REGISTRY.adapters
    assert "codex" in REGISTRY.adapters
    assert "claude-code" in REGISTRY.adapters


def test_adapter_registry_resolves_echo():
    """Test that echo adapter can be resolved."""
    adapter = REGISTRY.resolve("echo", model="test-model")
    assert isinstance(adapter, EchoAdapter)


def test_adapter_registry_resolves_codex():
    """Test that codex adapter can be resolved."""
    adapter = REGISTRY.resolve("codex", model="gpt-4")
    assert isinstance(adapter, CodexAdapter)


def test_adapter_registry_resolves_claude_code():
    """Test that claude-code adapter can be resolved."""
    adapter = REGISTRY.resolve("claude-code", model="claude-sonnet-4")
    assert isinstance(adapter, ClaudeCodeAdapter)


# ──────────────────────────────────────────────────────────────────────────────
# Echo Adapter Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_echo_adapter_basic():
    """Test that echo adapter mirrors prompt."""
    adapter = EchoAdapter(model="test-model")
    request = AssistantRequest(
        role="planner", prompt="Test prompt", context={"key": "value"}
    )
    response = adapter.generate(request)

    assert "ECHO ADAPTER" in response.content
    assert "Test prompt" in response.content
    assert "key" in response.content
    assert response.metadata["adapter"] == "echo"


# ──────────────────────────────────────────────────────────────────────────────
# Codex Adapter Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_codex_adapter_initialization():
    """Test Codex adapter initialization with various configs."""
    adapter = CodexAdapter(model="gpt-4", timeout=120)
    assert adapter.model == "gpt-4"
    assert adapter.timeout == 120


def test_codex_adapter_success_response():
    """Test Codex adapter with successful JSONL stream response."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Create a plan")

    # Mock subprocess.run with JSONL output (one JSON object per line)
    mock_result = Mock()
    mock_result.returncode = 0
    # Simulate Codex JSONL stream with multiple events
    mock_result.stdout = "\n".join(
        [
            json.dumps({"type": "session_start", "session_id": "abc123"}),
            json.dumps({"type": "message", "role": "user", "content": "Create a plan"}),
            json.dumps({"type": "message", "role": "assistant", "content": "Here is the plan..."}),
            json.dumps({"type": "usage", "input_tokens": 10, "output_tokens": 20}),
        ]
    )
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        response = adapter.generate(request)

    assert response.content == "Here is the plan..."
    assert response.concluded is False
    assert response.metadata["adapter"] == "codex"
    assert response.metadata["model"] == "gpt-4"
    assert response.metadata["stream_events"] == 4
    assert response.metadata["input_tokens"] == 10
    assert response.metadata["output_tokens"] == 20


def test_codex_adapter_jsonl_partial_lines():
    """Test Codex adapter handling of partial/invalid JSON lines."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock JSONL with one invalid line
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "\n".join(
        [
            json.dumps({"type": "session_start", "session_id": "xyz"}),
            "{invalid json line",  # Invalid JSON
            json.dumps({"type": "message", "role": "assistant", "content": "Valid response"}),
        ]
    )
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        response = adapter.generate(request)

    # Should successfully extract message despite invalid line
    assert response.content == "Valid response"
    assert "parse_error" in [e.get("type") for e in response.metadata.get("event_types", [])]


def test_codex_adapter_cli_failure():
    """Test Codex adapter handling of CLI failures."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock failed CLI invocation
    mock_result = Mock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Authentication failed"

    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    assert "exit code 1" in str(exc_info.value)
    assert "Authentication failed" in str(exc_info.value)


def test_codex_adapter_timeout():
    """Test Codex adapter handling of timeout."""
    adapter = CodexAdapter(model="gpt-4", timeout=1)
    request = AssistantRequest(role="planner", prompt="Test")

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 1)):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    assert "timeout" in str(exc_info.value).lower()


def test_codex_adapter_empty_stream():
    """Test Codex adapter handling of empty JSONL stream."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock response with empty output
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    assert "no events" in str(exc_info.value).lower()


def test_codex_adapter_no_assistant_message():
    """Test Codex adapter when stream has no assistant message."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock JSONL with events but no assistant message
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "\n".join(
        [
            json.dumps({"type": "session_start", "session_id": "abc"}),
            json.dumps({"type": "message", "role": "user", "content": "Test"}),
            # No assistant message
        ]
    )
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    assert "no assistant message" in str(exc_info.value).lower()


def test_codex_adapter_tool_usage_metadata():
    """Test that Codex adapter captures tool usage metadata."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock JSONL with tool usage events
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "\n".join(
        [
            json.dumps({"type": "message", "role": "assistant", "content": "Using tools..."}),
            json.dumps({"type": "tool_use", "tool_name": "bash"}),
            json.dumps({"type": "tool_use", "tool_name": "read_file"}),
        ]
    )
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        response = adapter.generate(request)

    assert response.content == "Using tools..."
    assert "tool_calls" in response.metadata
    assert response.metadata["tool_calls"] == ["bash", "read_file"]


def test_codex_adapter_cli_not_found():
    """Test Codex adapter handling of missing CLI."""
    adapter = CodexAdapter(model="gpt-4", cli_path="/nonexistent/codex")
    request = AssistantRequest(role="planner", prompt="Test")

    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    assert "not found" in str(exc_info.value).lower()


# ──────────────────────────────────────────────────────────────────────────────
# Claude Code Adapter Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_claude_code_adapter_initialization():
    """Test Claude Code adapter initialization with various configs."""
    adapter = ClaudeCodeAdapter(
        model="claude-sonnet-4", timeout=300, workspace_root="/tmp"
    )
    assert adapter.model == "claude-sonnet-4"
    assert adapter.timeout == 300
    assert adapter.workspace_root == "/tmp"


def test_claude_code_adapter_success_response():
    """Test Claude Code adapter with successful CLI response."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Implement feature")

    # Mock subprocess.run
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(
        {
            "content": "Implementation complete",
            "concluded": False,
            "metadata": {"files_modified": ["src/main.py"]},
        }
    )
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        response = adapter.generate(request)

    assert response.content == "Implementation complete"
    assert response.concluded is False
    assert response.metadata["adapter"] == "claude-code"
    assert response.metadata["model"] == "claude-sonnet-4"


def test_claude_code_adapter_code_metadata():
    """Test Claude Code adapter captures code-specific metadata."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Implement feature")

    # Mock response with code metadata
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(
        {
            "content": "Done",
            "files_modified": ["a.py", "b.py"],
            "commands_executed": ["pytest"],
            "commit_sha": "abc123",
        }
    )
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        response = adapter.generate(request)

    assert response.metadata["files_modified"] == ["a.py", "b.py"]
    assert response.metadata["commands_executed"] == ["pytest"]
    assert response.metadata["commit_sha"] == "abc123"


def test_claude_code_adapter_cli_failure():
    """Test Claude Code adapter handling of CLI failures."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Test")

    # Mock failed CLI invocation
    mock_result = Mock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Permission denied"

    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(ClaudeCodeError) as exc_info:
            adapter.generate(request)

    assert "exit code 1" in str(exc_info.value)
    assert "Permission denied" in str(exc_info.value)


def test_claude_code_adapter_timeout():
    """Test Claude Code adapter handling of timeout."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4", timeout=1)
    request = AssistantRequest(role="implementer", prompt="Test")

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1)):
        with pytest.raises(ClaudeCodeError) as exc_info:
            adapter.generate(request)

    assert "timeout" in str(exc_info.value).lower()


def test_claude_code_adapter_invalid_json():
    """Test Claude Code adapter handling of invalid JSON response."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Test")

    # Mock response with invalid JSON
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "Invalid JSON response"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(ClaudeCodeError) as exc_info:
            adapter.generate(request)

    assert "parse" in str(exc_info.value).lower()


def test_claude_code_adapter_workspace_context():
    """Test that Claude Code adapter passes workspace to CLI."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4", workspace_root="/my/workspace")
    request = AssistantRequest(role="implementer", prompt="Test")

    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"content": "Done"})
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        adapter.generate(request)

        # Verify workspace was passed in command and cwd
        call_args = mock_run.call_args
        assert "--workspace" in call_args[0][0]
        assert call_args[1]["cwd"] == "/my/workspace"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
