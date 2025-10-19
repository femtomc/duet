"""
Tests for Sprint DSL-3 facet step model.

Verifies that phases can be defined as explicit facet scripts with
ordered steps (read → tool → agent → human → write).
"""

import pytest

from duet.dsl import Channel, Phase
from duet.dsl.steps import ReadStep, ToolStep, AgentStep, HumanStep, WriteStep, FacetContext, StepResult
from duet.dsl.tools import GitChangeTool, ApprovalTool


def test_read_step_execution():
    """Test that ReadStep reads channel values into context."""
    task = Channel(name="task", schema="text")
    feedback = Channel(name="feedback", schema="text")

    step = ReadStep(channels=[task, feedback])
    context = FacetContext(
        phase_name="test",
        run_id="run-1",
        iteration=1,
        channel_reads={"task": "Build feature X", "feedback": "Add tests"},
    )

    result = step.execute(context)

    assert result.success
    assert result.context_updates["task"] == "Build feature X"
    assert result.context_updates["feedback"] == "Add tests"


def test_read_step_with_custom_keys():
    """Test ReadStep with custom context keys."""
    task = Channel(name="task")
    feedback = Channel(name="feedback")

    step = ReadStep(channels=[task, feedback], into=["task_input", "review_notes"])
    context = FacetContext(
        phase_name="test",
        run_id="run-1",
        iteration=1,
        channel_reads={"task": "value1", "feedback": "value2"},
    )

    result = step.execute(context)

    assert result.context_updates["task_input"] == "value1"
    assert result.context_updates["review_notes"] == "value2"


def test_tool_step_execution():
    """Test that ToolStep executes tool and returns results."""
    tool = GitChangeTool()
    step = ToolStep(tool=tool)

    context = FacetContext(
        phase_name="implement",
        run_id="run-1",
        iteration=1,
        workspace_root="/workspace",
    )

    result = step.execute(context)

    # Stub tool returns success
    assert result.success


def test_write_step_with_static_value():
    """Test WriteStep with static value."""
    status = Channel(name="status")
    step = WriteStep(channel=status, static_value="complete")

    context = FacetContext(phase_name="test", run_id="run-1", iteration=1)
    result = step.execute(context)

    assert result.success
    assert result.channel_writes["status"] == "complete"


def test_write_step_with_context_lookup():
    """Test WriteStep reading from context."""
    output = Channel(name="output")
    step = WriteStep(channel=output, value_key="result")

    context = FacetContext(phase_name="test", run_id="run-1", iteration=1)
    context.set("result", "processed data")

    result = step.execute(context)

    assert result.success
    assert result.channel_writes["output"] == "processed data"


def test_phase_with_read_step():
    """Test adding ReadStep to phase via fluent API."""
    task = Channel(name="task")
    feedback = Channel(name="feedback")

    phase = Phase(name="plan", agent="planner").read(task, feedback)

    assert len(phase.steps) == 1
    assert isinstance(phase.steps[0], ReadStep)
    assert len(phase.steps[0].channels) == 2


def test_phase_with_tool_step():
    """Test adding ToolStep to phase via fluent API."""
    git_status = Channel(name="git_status")
    tool = GitChangeTool()
    phase = Phase(name="implement", agent="dev").tool(tool, outputs=[git_status])

    assert len(phase.steps) == 1
    assert isinstance(phase.steps[0], ToolStep)
    assert phase.steps[0].tool is tool
    assert len(phase.steps[0].outputs) == 1
    assert phase.steps[0].outputs[0] is git_status


def test_phase_with_agent_step():
    """Test adding AgentStep to phase via fluent API."""
    plan_ch = Channel(name="plan")
    phase = Phase(name="plan", agent="planner").call_agent("planner", writes=[plan_ch], role="planner")

    assert len(phase.steps) == 1
    assert isinstance(phase.steps[0], AgentStep)
    assert phase.steps[0].agent == "planner"
    assert phase.steps[0].writes == [plan_ch]
    assert phase.steps[0].role == "planner"


def test_phase_with_human_step():
    """Test adding HumanStep to phase via fluent API."""
    plan_ch = Channel(name="plan")
    code = Channel(name="code")

    phase = Phase(name="review", agent="reviewer").human(
        "Approval required",
        reads=[plan_ch, code],
        timeout=300
    )

    assert len(phase.steps) == 1
    assert isinstance(phase.steps[0], HumanStep)
    assert phase.steps[0].reason == "Approval required"
    assert len(phase.steps[0].reads) == 2
    assert phase.steps[0].timeout == 300


def test_phase_with_write_step():
    """Test adding WriteStep to phase via fluent API."""
    status = Channel(name="status")
    phase = Phase(name="finalize", agent="system").write(status, value="done")

    assert len(phase.steps) == 1
    assert isinstance(phase.steps[0], WriteStep)
    assert phase.steps[0].channel is status
    assert phase.steps[0].static_value == "done"


def test_facet_script_chaining():
    """Test that facet script steps can be chained."""
    task = Channel(name="task")
    plan_ch = Channel(name="plan")
    code = Channel(name="code")
    status = Channel(name="status")
    git_info = Channel(name="git_info")

    # Build a complete facet script
    implement = (
        Phase(name="implement", agent="implementer")
        .read(task, plan_ch)
        .tool(GitChangeTool(), outputs=[git_info])  # Channel object
        .call_agent("implementer", writes=[code], role="implementer")
        .write(status, value="implemented")
    )

    # Verify all steps were added in order
    assert len(implement.steps) == 4
    assert isinstance(implement.steps[0], ReadStep)
    assert isinstance(implement.steps[1], ToolStep)
    assert isinstance(implement.steps[2], AgentStep)
    assert isinstance(implement.steps[3], WriteStep)


def test_facet_script_with_human_approval():
    """Test facet script with human approval step."""
    plan_ch = Channel(name="plan")
    code = Channel(name="code")
    verdict = Channel(name="verdict")

    review = (
        Phase(name="review", agent="reviewer")
        .read(plan_ch, code)
        .human("Manual code review required", reads=[plan_ch, code])
        .call_agent("reviewer", writes=[verdict], role="reviewer")
    )

    assert len(review.steps) == 3
    assert isinstance(review.steps[0], ReadStep)
    assert isinstance(review.steps[1], HumanStep)
    assert isinstance(review.steps[2], AgentStep)


def test_backward_compat_consume_publish():
    """Test that old consume/publish API still works alongside steps."""
    task = Channel(name="task")
    plan_ch = Channel(name="plan")

    # Old API still works
    phase = Phase(name="plan", agent="planner", consumes=[task], publishes=[plan_ch])

    assert len(phase.consumes) == 1
    assert len(phase.publishes) == 1
    assert len(phase.steps) == 0  # No steps added via old API


def test_step_result_factory_methods():
    """Test StepResult convenience constructors."""
    # Success
    result = StepResult.ok(key1="value1", key2="value2")
    assert result.success
    assert result.context_updates == {"key1": "value1", "key2": "value2"}

    # Failure
    result = StepResult.fail("Something went wrong")
    assert not result.success
    assert result.error == "Something went wrong"


def test_tool_context_only_no_channel_writes():
    """Test that ToolStep can update context without channel writes."""
    tool = GitChangeTool()
    step = ToolStep(tool=tool, into_context=True)  # No outputs - context only

    context = FacetContext(
        phase_name="test",
        run_id="run-1",
        iteration=1,
        workspace_root="/workspace",
    )

    result = step.execute(context)

    # Tool results go to context
    assert result.success
    # But no channel writes since outputs=[]
    assert len(result.channel_writes) == 0


def test_tool_with_explicit_channel_writes():
    """Test that ToolStep writes to channels when outputs declared."""
    from duet.dsl.tools import BaseTool, ToolContext, ToolResult

    # Custom tool that returns data (using new API)
    class DataTool(BaseTool):
        def run(self, context: ToolContext) -> ToolResult:
            return ToolResult.ok(
                context={"processed_internally": "context_value"},
                channels={"output": "data_value"},  # Key by output channel name
            )

    output_ch = Channel(name="output")
    tool = DataTool(name="data_processor")
    step = ToolStep(tool=tool, outputs=[output_ch])

    context = FacetContext(
        phase_name="test",
        run_id="run-1",
        iteration=1,
    )

    result = step.execute(context)

    # Tool results split correctly
    assert result.success
    assert "processed_internally" in result.context_updates  # Context enrichment
    assert "output" in result.channel_writes  # Channel write


def test_phase_get_reads_from_steps():
    """Test that Phase.get_reads() extracts channels from steps."""
    task = Channel(name="task")
    feedback = Channel(name="feedback")
    plan_ch = Channel(name="plan")

    phase = (
        Phase(name="plan", agent="planner")
        .read(task, feedback)
        .call_agent("planner", writes=[plan_ch])
    )

    reads = phase.get_reads()
    assert len(reads) == 2
    assert task in reads
    assert feedback in reads


def test_phase_get_writes_from_steps():
    """Test that Phase.get_writes() extracts channels from steps."""
    task = Channel(name="task")
    plan_ch = Channel(name="plan")
    status = Channel(name="status")

    phase = (
        Phase(name="plan", agent="planner")
        .read(task)
        .call_agent("planner", writes=[plan_ch])
        .write(status, value="planned")
    )

    writes = phase.get_writes()
    assert len(writes) == 2
    assert plan_ch in writes
    assert status in writes


def test_phase_fallback_to_consumes_publishes():
    """Test that phases without steps fall back to old consumes/publishes."""
    task = Channel(name="task")
    plan_ch = Channel(name="plan")

    # Old-style phase definition
    phase = Phase(name="plan", agent="planner", consumes=[task], publishes=[plan_ch])

    reads = phase.get_reads()
    writes = phase.get_writes()

    assert reads == [task]
    assert writes == [plan_ch]


def test_step_ordering_validation():
    """Test that phase validates step ordering."""
    task = Channel(name="task")
    plan_ch = Channel(name="plan")
    code = Channel(name="code")

    # Invalid: AgentStep without prior ReadStep
    bad_phase = Phase(name="bad", agent="agent").call_agent("agent", writes=[plan_ch])
    errors = bad_phase.validate_step_ordering()
    assert len(errors) == 1
    assert "ReadStep" in errors[0]

    # Invalid: Multiple AgentSteps
    multi_agent = (
        Phase(name="multi", agent="agent")
        .read(task)
        .call_agent("agent1", writes=[plan_ch])
        .call_agent("agent2", writes=[code])
    )
    errors = multi_agent.validate_step_ordering()
    assert len(errors) == 1
    assert "Multiple AgentSteps" in errors[0]

    # Valid: Proper ordering
    good_phase = (
        Phase(name="good", agent="agent")
        .read(task)
        .tool(GitChangeTool())
        .call_agent("agent", writes=[plan_ch])
    )
    errors = good_phase.validate_step_ordering()
    assert len(errors) == 0
