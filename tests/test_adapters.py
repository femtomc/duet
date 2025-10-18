"""
Unit tests for adapter implementations.

Tests adapter functionality using mocked CLI responses to avoid
requiring actual Codex/Claude Code installations.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from duet.adapters import ClaudeCodeAdapter, CodexAdapter, EchoAdapter, REGISTRY
from duet.adapters.base import StreamEvent
from duet.adapters.claude_code import ClaudeCodeError
from duet.adapters.codex import CodexError
from duet.models import AssistantRequest


# ──────────────────────────────────────────────────────────────────────────────
# Mock Helpers for Popen
# ──────────────────────────────────────────────────────────────────────────────


def mock_popen_success(stdout_lines: list[str], returncode: int = 0, stderr: str = ""):
    """
    Create a mock Popen process that yields stdout lines and returns exit code.

    Args:
        stdout_lines: List of lines to yield from stdout
        returncode: Exit code to return from wait()
        stderr: stderr content

    Returns:
        Mock process object compatible with Popen
    """
    mock_process = Mock()
    mock_process.stdout = iter(stdout_lines)
    mock_process.stderr = io.StringIO(stderr)
    mock_process.wait = Mock(return_value=returncode)
    mock_process.poll = Mock(return_value=returncode)  # Process is finished
    mock_process.kill = Mock()
    return mock_process


def mock_popen_timeout(timeout_seconds: float = 1.0):
    """Create a mock Popen that raises TimeoutExpired on wait()."""
    mock_process = Mock()
    mock_process.stdout = iter([])
    mock_process.stderr = io.StringIO("")
    mock_process.wait = Mock(side_effect=subprocess.TimeoutExpired("cmd", timeout_seconds))
    mock_process.poll = Mock(return_value=None)  # Process still running
    mock_process.kill = Mock()
    return mock_process


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


def test_echo_adapter_streaming():
    """Test that echo adapter supports streaming interface."""
    adapter = EchoAdapter(model="test-model")
    request = AssistantRequest(role="planner", prompt="Test streaming")

    events_received = []
    def on_event(event: StreamEvent):
        events_received.append(event)

    response = adapter.stream(request, on_event=on_event)

    assert "ECHO ADAPTER" in response.content
    # Echo adapter should emit at least one event (system_notice)
    assert len(events_received) >= 1
    assert events_received[0]["event_type"] == "system_notice"
    assert "text_snippet" in events_received[0]


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

    # Mock Popen with JSONL output (actual Codex event structure)
    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "abc123"}) + "\n",
        json.dumps({"type": "turn.started"}) + "\n",
        json.dumps(
            {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "Thinking..."}}
        ) + "\n",
        json.dumps(
            {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "Here is the plan..."}}
        ) + "\n",
        json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 20, "cached_input_tokens": 0}}
        ) + "\n",
    ]

    mock_process = mock_popen_success(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        response = adapter.generate(request)

    assert response.content == "Here is the plan..."
    assert response.concluded is False
    assert response.metadata["adapter"] == "codex"
    assert response.metadata["model"] == "gpt-4"
    assert response.metadata["stream_events"] == 5
    assert response.metadata["input_tokens"] == 10
    assert response.metadata["output_tokens"] == 20


def test_codex_adapter_streaming_with_callback():
    """Test Codex adapter streaming with on_event callback."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "xyz"}) + "\n",
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Response"}}
        ) + "\n",
    ]

    mock_process = mock_popen_success(stdout_lines, returncode=0)

    events_received = []
    def on_event(event: StreamEvent):
        events_received.append(event)

    with patch("subprocess.Popen", return_value=mock_process):
        response = adapter.stream(request, on_event=on_event)

    assert response.content == "Response"
    assert len(events_received) == 2
    # Events now use canonical types
    assert events_received[0]["event_type"] == "thread_started"
    assert events_received[1]["event_type"] == "assistant_message"
    # Verify enriched field
    assert "text_snippet" in events_received[1]
    assert events_received[1]["text_snippet"] == "Response"


def test_codex_adapter_jsonl_partial_lines():
    """Test Codex adapter handling of partial/invalid JSON lines."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock JSONL with one invalid line
    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "xyz"}) + "\n",
        "{invalid json line\n",  # Invalid JSON
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Valid response"}}
        ) + "\n",
    ]

    mock_process = mock_popen_success(stdout_lines, returncode=0)

    events_received = []
    def on_event(event: StreamEvent):
        events_received.append(event)

    with patch("subprocess.Popen", return_value=mock_process):
        response = adapter.stream(request, on_event=on_event)

    # Should successfully extract message despite invalid line
    assert response.content == "Valid response"
    assert response.metadata["parse_errors"] == 1

    # Verify parse_error event was emitted
    error_events = [e for e in events_received if e["event_type"] == "parse_error"]
    assert len(error_events) == 1


def test_codex_adapter_cli_failure():
    """Test Codex adapter handling of CLI failures."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock failed CLI invocation
    mock_process = mock_popen_success(
        stdout_lines=[],
        returncode=1,
        stderr="Authentication failed"
    )

    with patch("subprocess.Popen", return_value=mock_process):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    assert "exit code 1" in str(exc_info.value)
    assert "Authentication failed" in str(exc_info.value)


def test_codex_adapter_timeout():
    """Test Codex adapter handling of timeout."""
    adapter = CodexAdapter(model="gpt-4", timeout=1)
    request = AssistantRequest(role="planner", prompt="Test")

    mock_process = mock_popen_timeout(timeout_seconds=1.0)

    with patch("subprocess.Popen", return_value=mock_process):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    # Check for "timeout" or "timed out" in error message
    error_msg = str(exc_info.value).lower()
    assert "timeout" in error_msg or "timed out" in error_msg


def test_codex_adapter_empty_stream():
    """Test Codex adapter handling of empty JSONL stream."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock response with empty output
    mock_process = mock_popen_success(stdout_lines=[], returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    assert "no events" in str(exc_info.value).lower()


def test_codex_adapter_no_assistant_message():
    """Test Codex adapter when stream has no assistant message."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock JSONL with events but no agent_message
    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "abc"}) + "\n",
        json.dumps({"type": "turn.started"}) + "\n",
        json.dumps({"type": "turn.completed", "usage": {}}) + "\n",
        # No item.completed with type=agent_message
    ]

    mock_process = mock_popen_success(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        with pytest.raises(CodexError) as exc_info:
            adapter.generate(request)

    assert "no assistant message" in str(exc_info.value).lower()


def test_codex_adapter_tool_usage_metadata():
    """Test that Codex adapter captures tool usage metadata."""
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    # Mock JSONL with actual Codex structure including usage
    stdout_lines = [
        json.dumps({"type": "thread.started", "thread_id": "test-thread"}) + "\n",
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Response with metadata"}}
        ) + "\n",
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": 20,
                },
            }
        ) + "\n",
    ]

    mock_process = mock_popen_success(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        response = adapter.generate(request)

    assert response.content == "Response with metadata"
    assert response.metadata["input_tokens"] == 100
    assert response.metadata["output_tokens"] == 50
    assert response.metadata["cached_input_tokens"] == 20
    assert response.metadata["thread_id"] == "test-thread"


def test_codex_adapter_cli_not_found():
    """Test Codex adapter handling of missing CLI."""
    adapter = CodexAdapter(model="gpt-4", cli_path="/nonexistent/codex")
    request = AssistantRequest(role="planner", prompt="Test")

    with patch("subprocess.Popen", side_effect=FileNotFoundError()):
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

    # Mock Popen - Claude Code returns single JSON object
    json_response = json.dumps(
        {
            "result": "Implementation complete",
            "concluded": False,
            "metadata": {"files_modified": ["src/main.py"]},
        }
    )

    stdout_lines = [json_response + "\n"]
    mock_process = mock_popen_success(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        response = adapter.generate(request)

    assert response.content == "Implementation complete"
    assert response.concluded is False
    assert response.metadata["adapter"] == "claude-code"
    assert response.metadata["model"] == "claude-sonnet-4"


def test_claude_code_adapter_streaming_with_callback():
    """Test Claude Code adapter streaming with on_event callback."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Test")

    json_response = json.dumps({"result": "Done"})
    stdout_lines = [json_response + "\n"]
    mock_process = mock_popen_success(stdout_lines, returncode=0)

    events_received = []
    def on_event(event: StreamEvent):
        events_received.append(event)

    with patch("subprocess.Popen", return_value=mock_process):
        response = adapter.stream(request, on_event=on_event)

    assert response.content == "Done"
    assert len(events_received) >= 1
    # Claude events now use canonical types
    assert events_received[0]["event_type"] == "assistant_message"
    assert "text_snippet" in events_received[0]


def test_claude_code_adapter_multiline_json():
    """Test Claude Code adapter with JSON split across multiple lines."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Test")

    # Simulate JSON split across lines (common with pretty-printed output)
    stdout_lines = [
        '{\n',
        '  "result": "Multiline response",\n',
        '  "type": "success"\n',
        '}\n',
    ]
    mock_process = mock_popen_success(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        response = adapter.generate(request)

    assert response.content == "Multiline response"


def test_claude_code_adapter_code_metadata():
    """Test Claude Code adapter captures code-specific metadata."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Implement feature")

    # Mock response with code metadata
    json_response = json.dumps(
        {
            "result": "Done",
            "files_modified": ["a.py", "b.py"],
            "commands_executed": ["pytest"],
            "commit_sha": "abc123",
        }
    )
    stdout_lines = [json_response + "\n"]
    mock_process = mock_popen_success(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        response = adapter.generate(request)

    assert response.metadata["files_modified"] == ["a.py", "b.py"]
    assert response.metadata["commands_executed"] == ["pytest"]
    assert response.metadata["commit_sha"] == "abc123"


def test_claude_code_adapter_cli_failure():
    """Test Claude Code adapter handling of CLI failures."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Test")

    # Mock failed CLI invocation
    mock_process = mock_popen_success(
        stdout_lines=[],
        returncode=1,
        stderr="Permission denied"
    )

    with patch("subprocess.Popen", return_value=mock_process):
        with pytest.raises(ClaudeCodeError) as exc_info:
            adapter.generate(request)

    assert "exit code 1" in str(exc_info.value)
    assert "Permission denied" in str(exc_info.value)


def test_claude_code_adapter_timeout():
    """Test Claude Code adapter handling of timeout."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4", timeout=1)
    request = AssistantRequest(role="implementer", prompt="Test")

    mock_process = mock_popen_timeout(timeout_seconds=1.0)

    with patch("subprocess.Popen", return_value=mock_process):
        with pytest.raises(ClaudeCodeError) as exc_info:
            adapter.generate(request)

    # Check for "timeout" or "timed out" in error message
    error_msg = str(exc_info.value).lower()
    assert "timeout" in error_msg or "timed out" in error_msg


def test_claude_code_adapter_invalid_json():
    """Test Claude Code adapter handling of invalid JSON response."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    request = AssistantRequest(role="implementer", prompt="Test")

    # Mock response with invalid JSON
    stdout_lines = ["Invalid JSON response\n"]
    mock_process = mock_popen_success(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process):
        with pytest.raises(ClaudeCodeError) as exc_info:
            adapter.generate(request)

    assert "no valid json" in str(exc_info.value).lower()


def test_claude_code_adapter_workspace_context():
    """Test that Claude Code adapter passes workspace via cwd."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4", workspace_root="/my/workspace")
    request = AssistantRequest(role="implementer", prompt="Test")

    json_response = json.dumps({"result": "Done"})
    stdout_lines = [json_response + "\n"]
    mock_process = mock_popen_success(stdout_lines, returncode=0)

    with patch("subprocess.Popen", return_value=mock_process) as mock_popen:
        adapter.generate(request)

        # Verify workspace was passed via cwd (not --workspace flag)
        call_args = mock_popen.call_args
        assert call_args[1]["cwd"] == "/my/workspace"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
