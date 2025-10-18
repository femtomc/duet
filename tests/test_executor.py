"""
Unit tests for Sprint 10 WorkflowExecutor and PromptBuilder.

Tests channel store, prompt context, builders, guard evaluation, and
graph-driven phase execution.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from duet.channels import ChannelStore
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow
from duet.dsl.compiler import compile_workflow
from duet.executor import GuardEvaluator, WorkflowExecutor
from duet.models import AssistantResponse, ReviewVerdict
from duet.prompt_builder import (
    DefaultImplementationBuilder,
    DefaultPlanningBuilder,
    DefaultReviewBuilder,
    PromptContext,
)


# ──────────────────────────────────────────────────────────────────────────────
# ChannelStore Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_channel_store_initialization():
    """Test ChannelStore initializes with channel definitions."""
    channels = {
        "task": Channel(name="task", initial_value="Build feature X"),
        "plan": Channel(name="plan"),
    }

    store = ChannelStore(channels=channels)

    # Initial value should be set
    assert store.get("task") == "Build feature X"
    assert store.get("plan") is None


def test_channel_store_get_set():
    """Test basic get/set operations."""
    channels = {"task": Channel(name="task"), "plan": Channel(name="plan")}
    store = ChannelStore(channels=channels)

    # Set and get
    store.set("plan", "Step 1: Create endpoint")
    assert store.get("plan") == "Step 1: Create endpoint"

    # Get with default
    assert store.get("nonexistent", "default") == "default"


def test_channel_store_undeclared_channel():
    """Test setting undeclared channel raises error."""
    channels = {"task": Channel(name="task")}
    store = ChannelStore(channels=channels)

    with pytest.raises(ValueError, match="undeclared channel"):
        store.set("invalid", "value")


def test_channel_store_update():
    """Test bulk update operation."""
    channels = {
        "task": Channel(name="task"),
        "plan": Channel(name="plan"),
        "code": Channel(name="code"),
    }
    store = ChannelStore(channels=channels)

    store.update({"task": "Task A", "plan": "Plan A"})

    assert store.get("task") == "Task A"
    assert store.get("plan") == "Plan A"
    assert store.get("code") is None


def test_channel_store_snapshot_restore():
    """Test snapshot and restore operations."""
    channels = {"task": Channel(name="task"), "plan": Channel(name="plan")}
    store = ChannelStore(channels=channels)

    # Set some values
    store.set("task", "Original task")
    store.set("plan", "Original plan")

    # Snapshot
    snapshot = store.snapshot()
    assert snapshot == {"task": "Original task", "plan": "Original plan"}

    # Modify
    store.set("task", "Modified task")
    assert store.get("task") == "Modified task"

    # Restore
    store.restore(snapshot)
    assert store.get("task") == "Original task"
    assert store.get("plan") == "Original plan"


def test_channel_store_schema_validation():
    """Test basic schema validation."""
    channels = {
        "task": Channel(name="task", schema="text"),
        "verdict": Channel(name="verdict", schema="verdict"),
        "data": Channel(name="data", schema="json"),
    }
    store = ChannelStore(channels=channels)

    # Text schema
    assert store.validate_value("task", "some text") is True
    assert store.validate_value("task", 123) is False

    # Verdict schema
    assert store.validate_value("verdict", "approve") is True
    assert store.validate_value("verdict", "changes_requested") is True
    assert store.validate_value("verdict", "invalid") is False

    # JSON schema
    assert store.validate_value("data", {"key": "value"}) is True
    assert store.validate_value("data", [1, 2, 3]) is True
    assert store.validate_value("data", "not json") is False


# ──────────────────────────────────────────────────────────────────────────────
# PromptBuilder Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_prompt_context_creation():
    """Test PromptContext creation and helpers."""
    context = PromptContext(
        run_id="test-run",
        iteration=1,
        phase="plan",
        agent="planner",
        max_iterations=5,
        channel_payloads={"task": "Build auth", "feedback": None},
    )

    assert context.get_channel("task") == "Build auth"
    assert context.get_channel("feedback") is None
    assert context.get_channel("missing", "default") == "default"
    assert context.has_channel("task") is True
    assert context.has_channel("feedback") is False  # None doesn't count


def test_planning_builder():
    """Test DefaultPlanningBuilder generates planning prompts."""
    context = PromptContext(
        run_id="test-run",
        iteration=1,
        phase="plan",
        agent="planner",
        max_iterations=5,
        channel_payloads={"task": "Implement user authentication"},
    )

    builder = DefaultPlanningBuilder()
    request = builder.build(context)

    assert request.role == "planner"
    assert "Draft the implementation plan" in request.prompt
    assert "Iteration: 1/5" in request.prompt
    assert "Implement user authentication" in request.prompt
    assert request.context["iteration"] == 1


def test_planning_builder_with_feedback():
    """Test DefaultPlanningBuilder includes feedback from prior review."""
    context = PromptContext(
        run_id="test-run",
        iteration=2,
        phase="plan",
        agent="planner",
        max_iterations=5,
        channel_payloads={
            "task": "Implement auth",
            "feedback": "Focus on error handling",
        },
    )

    builder = DefaultPlanningBuilder()
    request = builder.build(context)

    assert "Prior Review Feedback" in request.prompt
    assert "Focus on error handling" in request.prompt
    assert "revise the plan" in request.prompt


def test_implementation_builder():
    """Test DefaultImplementationBuilder generates implementation prompts."""
    context = PromptContext(
        run_id="test-run",
        iteration=1,
        phase="implement",
        agent="implementer",
        max_iterations=5,
        workspace_root="/workspace",
        channel_payloads={"plan": "Step 1: Create endpoint\nStep 2: Add tests"},
    )

    builder = DefaultImplementationBuilder()
    request = builder.build(context)

    assert request.role == "implementer"
    assert "Apply the plan" in request.prompt
    assert "Workspace: /workspace" in request.prompt
    assert "Step 1: Create endpoint" in request.prompt
    assert "Follow the plan" in request.prompt


def test_review_builder():
    """Test DefaultReviewBuilder generates review prompts."""
    context = PromptContext(
        run_id="test-run",
        iteration=1,
        phase="review",
        agent="reviewer",
        max_iterations=5,
        channel_payloads={
            "plan": "Step 1: Create endpoint",
            "code": "Added /api/login endpoint",
        },
    )

    builder = DefaultReviewBuilder()
    request = builder.build(context)

    assert request.role == "reviewer"
    assert "Review the latest changes" in request.prompt
    assert "Step 1: Create endpoint" in request.prompt
    assert "Added /api/login endpoint" in request.prompt
    assert "APPROVE" in request.prompt
    assert "CHANGES_REQUESTED" in request.prompt
    assert "BLOCKED" in request.prompt


# ──────────────────────────────────────────────────────────────────────────────
# GuardEvaluator Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_guard_evaluator_build_context():
    """Test building guard context from channels and metadata."""
    channels = {
        "task": Channel(name="task"),
        "plan": Channel(name="plan"),
        "verdict": Channel(name="verdict"),
    }
    store = ChannelStore(channels=channels)
    store.set("plan", "Implementation plan")

    response = AssistantResponse(
        content="Review complete",
        verdict=ReviewVerdict.APPROVE,
    )

    git_changes = {"has_changes": True, "files_changed": 3}

    evaluator = GuardEvaluator()
    context = evaluator.build_guard_context(store, response, git_changes)

    assert context["plan"] == "Implementation plan"
    assert context["verdict"] == "approve"
    assert context["git_changes"]["has_changes"] is True


def test_guard_evaluator_transitions_priority_order():
    """Test transitions evaluated in priority order (highest first)."""
    workflow = Workflow(
        agents=[Agent(name="a", provider="codex", model="gpt-5")],
        channels=[Channel(name="verdict")],
        phases=[
            Phase(name="review", agent="a", publishes=["verdict"]),
            Phase(name="done", agent="a", is_terminal=True),
            Phase(name="plan", agent="a"),
            Phase(name="blocked", agent="a", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="review", to_phase="plan",
                       when=When.channel_has("verdict", "changes_requested"), priority=5),
            Transition(from_phase="review", to_phase="blocked",
                       when=When.channel_has("verdict", "blocked"), priority=15),
            Transition(from_phase="review", to_phase="done",
                       when=When.channel_has("verdict", "approve"), priority=10),
            Transition(from_phase="plan", to_phase="done"),  # Add outgoing from plan
        ],
    )

    graph = compile_workflow(workflow)
    evaluator = GuardEvaluator()

    # Context with approve verdict
    context = {"verdict": "approve"}
    result = evaluator.evaluate_transitions("review", graph, context)

    assert result.next_phase == "done"
    assert result.transition.priority == 10


def test_guard_evaluator_first_match_wins():
    """Test that first passing guard determines next phase."""
    workflow = Workflow(
        agents=[Agent(name="a", provider="codex", model="gpt-5")],
        channels=[Channel(name="verdict")],
        phases=[
            Phase(name="review", agent="a"),
            Phase(name="done", agent="a", is_terminal=True),
            Phase(name="alt", agent="a", is_terminal=True),
        ],
        transitions=[
            # Both could match, but higher priority checked first
            Transition(from_phase="review", to_phase="done",
                       when=When.always(), priority=10),
            Transition(from_phase="review", to_phase="alt",
                       when=When.always(), priority=5),
        ],
    )

    graph = compile_workflow(workflow)
    evaluator = GuardEvaluator()

    result = evaluator.evaluate_transitions("review", graph, {})

    # Higher priority wins
    assert result.next_phase == "done"


def test_guard_evaluator_no_guards_pass():
    """Test handling when no guards pass."""
    workflow = Workflow(
        agents=[Agent(name="a", provider="codex", model="gpt-5")],
        channels=[Channel(name="verdict")],
        phases=[
            Phase(name="review", agent="a"),
            Phase(name="done", agent="a", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="review", to_phase="done",
                       when=When.channel_has("verdict", "approve")),
        ],
    )

    graph = compile_workflow(workflow)
    evaluator = GuardEvaluator()

    # Context where guard fails
    context = {"verdict": "blocked"}
    result = evaluator.evaluate_transitions("review", graph, context)

    assert result.next_phase is None
    assert result.transition is None
    assert "No guards passed" in result.rationale


# ──────────────────────────────────────────────────────────────────────────────
# WorkflowExecutor Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_workflow_executor_initialization():
    """Test WorkflowExecutor initializes with workflow graph."""
    workflow = Workflow(
        agents=[Agent(name="planner", provider="codex", model="gpt-5")],
        channels=[Channel(name="task", initial_value="Default task")],
        phases=[
            Phase(name="plan", agent="planner", consumes=["task"]),
            Phase(name="done", agent="planner", is_terminal=True),
        ],
        transitions=[Transition(from_phase="plan", to_phase="done")],
    )

    graph = compile_workflow(workflow)
    executor = WorkflowExecutor(graph)

    # Channel store initialized with initial values
    assert executor.channel_store.get("task") == "Default task"


def test_workflow_executor_seed_channel():
    """Test seeding channels with CLI input."""
    workflow = Workflow(
        agents=[Agent(name="a", provider="codex", model="gpt-5")],
        channels=[Channel(name="task"), Channel(name="plan")],
        phases=[Phase(name="p", agent="a"), Phase(name="d", agent="a", is_terminal=True)],
        transitions=[Transition(from_phase="p", to_phase="d")],
    )

    graph = compile_workflow(workflow)
    executor = WorkflowExecutor(graph)

    # Seed task channel
    executor.seed_channel("task", "Implement OAuth")
    assert executor.channel_store.get("task") == "Implement OAuth"


def test_workflow_executor_execute_phase_with_mock_adapter():
    """Test executing a phase with mocked adapter."""
    workflow = Workflow(
        agents=[Agent(name="planner", provider="codex", model="gpt-5")],
        channels=[
            Channel(name="task"),
            Channel(name="plan"),
        ],
        phases=[
            Phase(name="plan", agent="planner", consumes=["task"], publishes=["plan"]),
            Phase(name="done", agent="planner", is_terminal=True),
        ],
        transitions=[Transition(from_phase="plan", to_phase="done")],
    )

    graph = compile_workflow(workflow)
    executor = WorkflowExecutor(graph)

    # Seed task
    executor.seed_channel("task", "Build feature X")

    # Mock adapter
    mock_adapter = Mock()
    mock_adapter.stream.return_value = AssistantResponse(
        content="Step 1: Create files\nStep 2: Write tests",
        metadata={},
    )

    # Build context
    context = PromptContext(
        run_id="test-run",
        iteration=1,
        phase="plan",
        agent="planner",
        max_iterations=5,
        workspace_root="/workspace",
    )

    # Execute phase
    result = executor.execute_phase("plan", mock_adapter, context)

    # Verify execution
    assert result.phase_name == "plan"
    assert result.response is not None
    assert result.response.content == "Step 1: Create files\nStep 2: Write tests"
    assert result.next_phase == "done"
    assert result.error is None

    # Verify channel updated
    assert "Step 1" in executor.channel_store.get("plan")


def test_workflow_executor_guard_based_routing():
    """Test executor routes based on guard evaluation."""
    workflow = Workflow(
        agents=[Agent(name="reviewer", provider="codex", model="gpt-5")],
        channels=[
            Channel(name="verdict"),
            Channel(name="plan"),
        ],
        phases=[
            Phase(name="review", agent="reviewer", publishes=["verdict"]),
            Phase(name="done", agent="reviewer", is_terminal=True),
            Phase(name="plan", agent="reviewer"),
        ],
        transitions=[
            Transition(from_phase="review", to_phase="done",
                       when=When.channel_has("verdict", "approve"), priority=10),
            Transition(from_phase="review", to_phase="plan",
                       when=When.channel_has("verdict", "changes_requested"), priority=5),
            Transition(from_phase="plan", to_phase="done"),  # Add outgoing from plan
        ],
    )

    graph = compile_workflow(workflow)
    executor = WorkflowExecutor(graph)

    # Mock adapter returning approve verdict
    mock_adapter = Mock()
    mock_adapter.stream.return_value = AssistantResponse(
        content="Looks good",
        verdict=ReviewVerdict.APPROVE,
    )

    context = PromptContext(
        run_id="test-run",
        iteration=1,
        phase="review",
        agent="reviewer",
        max_iterations=5,
    )

    result = executor.execute_phase("review", mock_adapter, context)

    # Should route to 'done' based on approve verdict
    assert result.next_phase == "done"
    assert result.guard_evaluation.transition.priority == 10


def test_workflow_executor_channel_outputs_extracted():
    """Test that published channels are extracted from response."""
    workflow = Workflow(
        agents=[Agent(name="planner", provider="codex", model="gpt-5")],
        channels=[
            Channel(name="task"),
            Channel(name="plan"),
            Channel(name="verdict"),
        ],
        phases=[
            Phase(name="plan", agent="planner", consumes=["task"], publishes=["plan"]),
            Phase(name="done", agent="planner", is_terminal=True),
        ],
        transitions=[Transition(from_phase="plan", to_phase="done")],
    )

    graph = compile_workflow(workflow)
    executor = WorkflowExecutor(graph)

    mock_adapter = Mock()
    mock_adapter.stream.return_value = AssistantResponse(
        content="Detailed implementation plan...",
    )

    context = PromptContext(
        run_id="test-run",
        iteration=1,
        phase="plan",
        agent="planner",
        max_iterations=5,
    )

    result = executor.execute_phase("plan", mock_adapter, context)

    # Plan channel should be updated
    assert "plan" in result.channel_updates
    assert result.channel_updates["plan"] == "Detailed implementation plan..."


def test_workflow_executor_unknown_phase():
    """Test executor handles unknown phase gracefully."""
    workflow = Workflow(
        agents=[Agent(name="a", provider="codex", model="gpt-5")],
        channels=[],
        phases=[Phase(name="p", agent="a"), Phase(name="d", agent="a", is_terminal=True)],
        transitions=[Transition(from_phase="p", to_phase="d")],
    )

    graph = compile_workflow(workflow)
    executor = WorkflowExecutor(graph)

    mock_adapter = Mock()
    context = PromptContext(
        run_id="test-run",
        iteration=1,
        phase="unknown",
        agent="a",
        max_iterations=5,
    )

    result = executor.execute_phase("unknown", mock_adapter, context)

    assert result.error is not None
    assert "Unknown phase" in result.error
    assert result.next_phase is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
