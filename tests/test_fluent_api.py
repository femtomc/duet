"""
Tests for Sprint DSL-2 fluent Phase API.

Verifies that the fluent builder pattern works correctly with immutable
copy-on-write semantics.
"""

import pytest

from duet.dsl import Channel, Phase


def test_fluent_with_agent():
    """Test that with_agent() creates a new Phase with agent set."""
    phase = Phase(name="test", agent="original")
    new_phase = phase.with_agent("new_agent")

    assert phase.agent == "original"  # Original unchanged
    assert new_phase.agent == "new_agent"
    assert new_phase.name == "test"
    assert phase.id == new_phase.id  # Same logical phase (ID preserved)
    assert phase is not new_phase  # But different Python objects


def test_fluent_consume_channels():
    """Test that consume() appends channels immutably."""
    ch1 = Channel(name="ch1")
    ch2 = Channel(name="ch2")
    ch3 = Channel(name="ch3")

    phase = Phase(name="test", agent="agent", consumes=[ch1])
    new_phase = phase.consume(ch2, ch3)

    assert len(phase.consumes) == 1
    assert len(new_phase.consumes) == 3
    assert ch1 in new_phase.consumes
    assert ch2 in new_phase.consumes
    assert ch3 in new_phase.consumes


def test_fluent_publish_channels():
    """Test that publish() appends channels immutably."""
    ch1 = Channel(name="ch1")
    ch2 = Channel(name="ch2")

    phase = Phase(name="test", agent="agent")
    new_phase = phase.publish(ch1, ch2)

    assert len(phase.publishes) == 0
    assert len(new_phase.publishes) == 2
    assert ch1 in new_phase.publishes


def test_fluent_describe():
    """Test that describe() sets description immutably."""
    phase = Phase(name="test", agent="agent")
    new_phase = phase.describe("This is a test phase")

    assert phase.description == ""
    assert new_phase.description == "This is a test phase"


def test_fluent_terminal():
    """Test that terminal() marks phase as terminal."""
    phase = Phase(name="test", agent="agent")
    new_phase = phase.terminal()

    assert phase.is_terminal is False
    assert new_phase.is_terminal is True


def test_fluent_with_metadata():
    """Test that with_metadata() merges metadata immutably."""
    phase = Phase(name="test", agent="agent", metadata={"key1": "value1"})
    new_phase = phase.with_metadata(key2="value2", key3="value3")

    assert "key2" not in phase.metadata
    assert new_phase.metadata["key1"] == "value1"
    assert new_phase.metadata["key2"] == "value2"
    assert new_phase.metadata["key3"] == "value3"


def test_fluent_chaining():
    """Test that fluent methods can be chained."""
    ch1 = Channel(name="input")
    ch2 = Channel(name="output")

    phase = (
        Phase(name="worker", agent="agent")
        .consume(ch1)
        .publish(ch2)
        .describe("Processes input and produces output")
        .with_metadata(role_hint="implementer")
    )

    assert phase.name == "worker"
    assert ch1 in phase.consumes
    assert ch2 in phase.publishes
    assert phase.description == "Processes input and produces output"
    assert phase.metadata["role_hint"] == "implementer"


def test_policy_helper_with_human():
    """Test that with_human() attaches ApprovalTool."""
    phase = Phase(name="test", agent="agent")
    new_phase = phase.with_human("Manual review needed")

    assert len(new_phase.tools) == 1
    assert new_phase.tools[0].name == "approval_check"
    assert new_phase.tools[0].approval_message == "Manual review needed"


def test_policy_helper_requires_git():
    """Test that requires_git() attaches GitChangeTool."""
    phase = Phase(name="test", agent="agent")
    new_phase = phase.requires_git()

    assert len(new_phase.tools) == 1
    assert new_phase.tools[0].name == "git_change_validator"
    assert new_phase.tools[0].require_changes is True


def test_policy_helper_counts_as_replan():
    """Test that counts_as_replan() is now a no-op (deprecated)."""
    plan = Phase(name="plan", agent="agent")
    review_before = Phase(name="review", agent="agent")
    review_after = review_before.counts_as_replan(loop_to=plan)

    # No-op: returns self without changes
    assert review_before is review_after
    # No tools attached, no metadata set
    assert len(review_after.tools) == 0


def test_terminal_phase_constructor():
    """Test that terminal_phase() class method works."""
    done = Phase.terminal_phase("done", "agent", "Workflow complete")

    assert done.name == "done"
    assert done.agent == "agent"
    assert done.description == "Workflow complete"
    assert done.is_terminal is True


def test_fluent_complex_workflow():
    """Test fluent API with a complete workflow scenario."""
    # Define channels
    task = Channel(name="task", schema="text")
    plan_ch = Channel(name="plan", schema="text")
    code = Channel(name="code", schema="git_diff")
    verdict = Channel(name="verdict", schema="verdict")
    feedback = Channel(name="feedback", schema="text")

    # Define phases with fluent API
    plan = (
        Phase(name="plan", agent="planner")
        .consume(task, feedback)
        .publish(plan_ch)
        .describe("Draft implementation plan")
        .with_metadata(role_hint="planner")
    )

    implement = (
        Phase(name="implement", agent="implementer")
        .consume(plan_ch)
        .publish(code)
        .describe("Execute the plan")
        .requires_git()  # Attaches GitChangeTool
        .with_metadata(role_hint="implementer")
    )

    review = (
        Phase(name="review", agent="reviewer")
        .consume(plan_ch, code)
        .publish(verdict, feedback)
        .describe("Review implementation")
        .counts_as_replan(loop_to=plan)  # No-op (deprecated)
        .with_metadata(role_hint="reviewer")
    )

    # Verify phase construction
    assert len(plan.consumes) == 2
    assert len(plan.publishes) == 1
    assert plan.metadata["role_hint"] == "planner"

    # Verify tool attachment
    assert len(implement.tools) == 1
    assert implement.tools[0].name == "git_change_validator"
    assert implement.metadata["role_hint"] == "implementer"

    # Verify counts_as_replan is no-op
    assert len(review.tools) == 0  # No tools from counts_as_replan
    assert review.metadata["role_hint"] == "reviewer"
