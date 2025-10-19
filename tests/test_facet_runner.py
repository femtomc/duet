"""
Tests for Sprint DSL-4 facet runner.

Verifies that facet scripts execute correctly with step-by-step execution,
local context management, and explicit channel writes.
"""

import pytest
from rich.console import Console

from duet.dsl import Channel, Phase
from duet.dsl.tools import GitChangeTool
from duet.dataspace import Dataspace, ChannelFact, FactPattern
from duet.facet_runner import FacetRunner


def create_dataspace_with_channels(**channels) -> Dataspace:
    """Helper to create dataspace with ChannelFacts."""
    ds = Dataspace()
    for channel_name, value in channels.items():
        ds.assert_fact(ChannelFact(
            fact_id=f"{channel_name}_0",
            channel_name=channel_name,
            value=value,
            iteration=0,
        ))
    return ds


def test_facet_runner_simple_read_write():
    """Test facet runner with simple read → write script."""
    task = Channel(name="task")
    output = Channel(name="output")

    phase = (
        Phase(name="process", agent="worker")
        .read(task)
        .write(output, value="processed")
    )

    # Create dataspace with task fact
    ds = create_dataspace_with_channels(task="input data")

    runner = FacetRunner(console=Console())
    result = runner.execute_facet(
        phase=phase,
        dataspace=ds,
        run_id="test-1",
        iteration=1,
        workspace_root="/workspace",
    )

    assert result.success
    assert result.context.fact_reads["task"] == "input data"
    assert result.channel_writes["output"] == "processed"

    # Output fact asserted to dataspace
    output_facts = ds.query(FactPattern(fact_type=ChannelFact, constraints={"channel_name": "output"}))
    assert len(output_facts) == 1
    assert output_facts[0].value == "processed"
    assert output_facts[0].iteration == 1


def test_facet_runner_with_tool_step():
    """Test facet runner executing tool step and agent."""
    from unittest.mock import Mock
    from duet.models import AssistantResponse

    task = Channel(name="task")
    plan_ch = Channel(name="plan")

    phase = (
        Phase(name="plan", agent="planner")
        .read(task)
        .tool(GitChangeTool())  # Context only
        .call_agent("planner", writes=[plan_ch])
    )

    # Mock adapter
    mock_adapter = Mock()
    mock_adapter.stream = Mock(return_value=AssistantResponse(
        content="Implementation plan created",
        metadata={}
    ))

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        channel_state={"task": "Build feature"},
        run_id="test-2",
        iteration=1,
        workspace_root="/workspace",
        adapter=mock_adapter,
    )

    assert result.success
    # Agent should have been invoked
    assert mock_adapter.stream.called
    # Response written to plan channel
    assert plan_ch.name in result.channel_writes
    assert result.channel_writes[plan_ch.name] == "Implementation plan created"


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
    assert "Approval needed before planning" in result.approval_reason
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

    # Tool that adds to context (using new API)
    class DataTool(BaseTool):
        def run(self, context: ToolContext) -> ToolResult:
            return ToolResult.ok(
                context_updates={"computed": "result_value"},  # Context enrichment
                channel_updates={},  # No channel writes
            )

    task = Channel(name="task")
    output = Channel(name="output")

    phase = (
        Phase(name="compute", agent="worker")
        .read(task)
        .tool(DataTool(name="processor"))  # Context only
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
    # And written to output channel via WriteStep
    assert result.channel_writes["output"] == "result_value"
