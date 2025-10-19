"""
Tests for Sprint DSL-4 facet runner.

Verifies that facet scripts execute correctly with step-by-step execution,
local context management, and explicit channel writes.
"""

import pytest
from rich.console import Console

from duet.dsl import Channel, Phase
from duet.dsl.tools import GitChangeTool
from duet.facet_runner import FacetRunner


def test_facet_runner_simple_read_write():
    """Test facet runner with simple read → write script."""
    task = Channel(name="task")
    output = Channel(name="output")

    phase = (
        Phase(name="process", agent="worker")
        .read(task)
        .write(output, value="processed")
    )

    runner = FacetRunner(console=Console())
    result = runner.execute_facet(
        phase=phase,
        channel_state={"task": "input data"},
        run_id="test-1",
        iteration=1,
        workspace_root="/workspace",
    )

    assert result.success
    assert "task" in result.context.channel_reads
    assert result.channel_writes["output"] == "processed"


def test_facet_runner_with_tool_step():
    """Test facet runner executing tool step."""
    task = Channel(name="task")
    plan_ch = Channel(name="plan")

    phase = (
        Phase(name="plan", agent="planner")
        .read(task)
        .tool(GitChangeTool())  # Context only
        .call_agent("planner", writes=[plan_ch])
    )

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        channel_state={"task": "Build feature"},
        run_id="test-2",
        iteration=1,
        workspace_root="/workspace",
    )

    assert result.success
    # Tool executed but no channel writes (context only)
    # Agent step prepared but not executed (needs orchestrator integration)


def test_facet_runner_human_step_pauses():
    """Test that HumanStep causes execution to pause."""
    task = Channel(name="task")
    plan_ch = Channel(name="plan")

    phase = (
        Phase(name="plan", agent="planner")
        .read(task)
        .human("Approval needed before planning")
        .call_agent("planner", writes=[plan_ch])  # Should not reach this
    )

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        channel_state={"task": "Sensitive operation"},
        run_id="test-3",
        iteration=1,
        workspace_root="/workspace",
    )

    # Execution paused at human step
    assert result.success  # No error, just paused
    assert result.human_approval_needed
    assert result.approval_reason == "Approval needed before planning"
    # Agent step should not have executed
    assert len(result.step_logs) == 2  # ReadStep + HumanStep only


def test_facet_runner_step_failure_stops_execution():
    """Test that step failure stops facet execution."""
    from duet.dsl.tools import BaseTool, ToolContext, ToolResult

    # Tool that fails
    class FailingTool(BaseTool):
        def run(self, context: ToolContext) -> ToolResult:
            return ToolResult.fail("Validation failed")

    task = Channel(name="task")
    plan_ch = Channel(name="plan")

    phase = (
        Phase(name="plan", agent="planner")
        .read(task)
        .tool(FailingTool(name="validator"))
        .call_agent("planner", writes=[plan_ch])  # Should not reach
    )

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        channel_state={"task": "data"},
        run_id="test-4",
        iteration=1,
        workspace_root="/workspace",
    )

    assert not result.success
    assert "Validation failed" in result.error
    # Only ReadStep and ToolStep executed
    assert len(result.step_logs) == 2


def test_facet_runner_multiple_writes():
    """Test facet runner with multiple WriteSteps."""
    status = Channel(name="status")
    count = Channel(name="count")
    message = Channel(name="message")

    phase = (
        Phase(name="finalize", agent="system")
        .write(status, value="complete")
        .write(count, value=42)
        .write(message, value="All done")
    )

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        channel_state={},
        run_id="test-5",
        iteration=1,
        workspace_root="/workspace",
    )

    assert result.success
    assert result.channel_writes["status"] == "complete"
    assert result.channel_writes["count"] == 42
    assert result.channel_writes["message"] == "All done"
    assert len(result.step_logs) == 3


def test_facet_runner_logs_steps():
    """Test that facet runner logs each step execution."""
    task = Channel(name="task")
    output = Channel(name="output")

    phase = (
        Phase(name="process", agent="worker")
        .read(task)
        .tool(GitChangeTool())
        .write(output, value="done")
    )

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        channel_state={"task": "data"},
        run_id="test-6",
        iteration=1,
        workspace_root="/workspace",
    )

    assert result.success
    assert len(result.step_logs) == 3
    assert result.step_logs[0]["step_type"] == "ReadStep"
    assert result.step_logs[1]["step_type"] == "ToolStep"
    assert result.step_logs[2]["step_type"] == "WriteStep"
    assert all(log["success"] for log in result.step_logs)


def test_facet_runner_context_accumulation():
    """Test that context accumulates results across steps."""
    from duet.dsl.tools import BaseTool, ToolContext, ToolResult

    # Tool that adds to context
    class DataTool(BaseTool):
        def run(self, context: ToolContext) -> ToolResult:
            return ToolResult(channel_updates={"computed": "result_value"}, success=True)

    task = Channel(name="task")
    output = Channel(name="output")

    phase = (
        Phase(name="compute", agent="worker")
        .read(task)
        .tool(DataTool(name="processor"))
        .write(output, value_key="computed")  # Use tool result from context
    )

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        channel_state={"task": "input"},
        run_id="test-7",
        iteration=1,
        workspace_root="/workspace",
    )

    assert result.success
    # Tool result should be in context
    assert result.context.get("computed") == "result_value"
    # And written to output channel
    assert result.channel_writes["output"] == "result_value"
