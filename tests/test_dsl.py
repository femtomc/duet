"""
Unit tests for Sprint 9 Workflow DSL.

Tests guard evaluation, component construction, validation, and compilation.
"""

from __future__ import annotations

import pytest

from duet.dsl import (
    Agent,
    AlwaysGuard,
    AndGuard,
    Channel,
    ChannelHasGuard,
    EmptyGuard,
    GitChangesGuard,
    NeverGuard,
    NotGuard,
    OrGuard,
    Phase,
    Transition,
    VerdictGuard,
    When,
    Workflow,
)
from duet.dsl.compiler import CompilationError, compile_workflow


# ──────────────────────────────────────────────────────────────────────────────
# Guard Evaluation Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_always_guard():
    """Test AlwaysGuard always evaluates to True."""
    guard = AlwaysGuard()
    assert guard.evaluate({}) is True
    assert guard.evaluate({"anything": "value"}) is True


def test_never_guard():
    """Test NeverGuard always evaluates to False."""
    guard = NeverGuard()
    assert guard.evaluate({}) is False
    assert guard.evaluate({"anything": "value"}) is False


def test_channel_has_guard():
    """Test ChannelHasGuard checks channel values."""
    guard = ChannelHasGuard("verdict", "approve")

    # Match
    assert guard.evaluate({"verdict": "approve"}) is True

    # No match
    assert guard.evaluate({"verdict": "changes_requested"}) is False

    # Missing channel
    assert guard.evaluate({}) is False


def test_empty_guard():
    """Test EmptyGuard checks for empty/None values."""
    guard = EmptyGuard("feedback")

    # None
    assert guard.evaluate({}) is True
    assert guard.evaluate({"feedback": None}) is True

    # Empty string
    assert guard.evaluate({"feedback": ""}) is True

    # Empty list
    assert guard.evaluate({"feedback": []}) is True

    # Empty dict
    assert guard.evaluate({"feedback": {}}) is True

    # Non-empty
    assert guard.evaluate({"feedback": "some text"}) is False
    assert guard.evaluate({"feedback": ["item"]}) is False


def test_verdict_guard():
    """Test VerdictGuard checks review verdicts."""
    guard = VerdictGuard("approve")

    # Case-insensitive match
    assert guard.evaluate({"verdict": "approve"}) is True
    assert guard.evaluate({"verdict": "APPROVE"}) is True
    assert guard.evaluate({"verdict": "Approve"}) is True

    # No match
    assert guard.evaluate({"verdict": "changes_requested"}) is False
    assert guard.evaluate({}) is False


def test_git_changes_guard():
    """Test GitChangesGuard checks for git changes."""
    guard_required = GitChangesGuard(required=True)
    guard_not_required = GitChangesGuard(required=False)

    context_with_changes = {"git_changes": {"has_changes": True}}
    context_no_changes = {"git_changes": {"has_changes": False}}
    context_missing = {}

    # Required=True
    assert guard_required.evaluate(context_with_changes) is True
    assert guard_required.evaluate(context_no_changes) is False
    assert guard_required.evaluate(context_missing) is False

    # Required=False
    assert guard_not_required.evaluate(context_with_changes) is False
    assert guard_not_required.evaluate(context_no_changes) is True
    assert guard_not_required.evaluate(context_missing) is True


def test_and_guard():
    """Test AndGuard combines guards with AND logic."""
    guard1 = ChannelHasGuard("verdict", "approve")
    guard2 = GitChangesGuard(required=True)
    combined = AndGuard(guard1, guard2)

    # Both true
    assert combined.evaluate({"verdict": "approve", "git_changes": {"has_changes": True}}) is True

    # One false
    assert combined.evaluate({"verdict": "approve", "git_changes": {"has_changes": False}}) is False
    assert combined.evaluate({"verdict": "blocked", "git_changes": {"has_changes": True}}) is False

    # Both false
    assert combined.evaluate({}) is False


def test_or_guard():
    """Test OrGuard combines guards with OR logic."""
    guard1 = ChannelHasGuard("verdict", "approve")
    guard2 = ChannelHasGuard("verdict", "skip")
    combined = OrGuard(guard1, guard2)

    # First true
    assert combined.evaluate({"verdict": "approve"}) is True

    # Second true
    assert combined.evaluate({"verdict": "skip"}) is True

    # Both false
    assert combined.evaluate({"verdict": "blocked"}) is False
    assert combined.evaluate({}) is False


def test_not_guard():
    """Test NotGuard negates a guard."""
    guard = ChannelHasGuard("verdict", "approve")
    negated = NotGuard(guard)

    # Original true -> negated false
    assert negated.evaluate({"verdict": "approve"}) is False

    # Original false -> negated true
    assert negated.evaluate({"verdict": "blocked"}) is True
    assert negated.evaluate({}) is True


def test_when_factory():
    """Test When factory methods."""
    # always/never
    assert When.always().evaluate({}) is True
    assert When.never().evaluate({}) is False

    # channel_has
    guard = When.channel_has("status", "ready")
    assert guard.evaluate({"status": "ready"}) is True
    assert guard.evaluate({"status": "blocked"}) is False

    # empty
    guard = When.empty("feedback")
    assert guard.evaluate({}) is True
    assert guard.evaluate({"feedback": "text"}) is False

    # verdict
    guard = When.verdict("approve")
    assert guard.evaluate({"verdict": "approve"}) is True

    # git_changes
    guard = When.git_changes(required=True)
    assert guard.evaluate({"git_changes": {"has_changes": True}}) is True

    # all (AND)
    guard = When.all(
        When.channel_has("verdict", "approve"),
        When.git_changes(required=True),
    )
    assert guard.evaluate({"verdict": "approve", "git_changes": {"has_changes": True}}) is True
    assert guard.evaluate({"verdict": "approve"}) is False

    # any (OR)
    guard = When.any(
        When.channel_has("verdict", "approve"),
        When.channel_has("verdict", "skip"),
    )
    assert guard.evaluate({"verdict": "approve"}) is True
    assert guard.evaluate({"verdict": "skip"}) is True
    assert guard.evaluate({"verdict": "blocked"}) is False

    # not_
    guard = When.not_(When.empty("feedback"))
    assert guard.evaluate({"feedback": "text"}) is True
    assert guard.evaluate({}) is False


# ──────────────────────────────────────────────────────────────────────────────
# Component Construction Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_agent_construction():
    """Test Agent construction and validation."""
    agent = Agent(name="planner", provider="codex", model="gpt-5-codex")
    assert agent.name == "planner"
    assert agent.provider == "codex"
    assert agent.model == "gpt-5-codex"


def test_agent_validation():
    """Test Agent validation errors."""
    with pytest.raises(ValueError, match="Agent name cannot be empty"):
        Agent(name="", provider="codex", model="gpt-5")

    with pytest.raises(ValueError, match="Agent provider cannot be empty"):
        Agent(name="planner", provider="", model="gpt-5")

    with pytest.raises(ValueError, match="Agent model cannot be empty"):
        Agent(name="planner", provider="codex", model="")


def test_channel_construction():
    """Test Channel construction with schema."""
    channel = Channel(
        name="plan",
        description="Implementation plan",
        schema="text",
        initial_value="",
    )
    assert channel.name == "plan"
    assert channel.description == "Implementation plan"
    assert channel.schema == "text"
    assert channel.initial_value == ""


def test_channel_validation():
    """Test Channel validation errors."""
    with pytest.raises(ValueError, match="Channel name cannot be empty"):
        Channel(name="", description="test")


def test_phase_construction():
    """Test Phase construction with channels."""
    phase = Phase(
        name="plan",
        agent="planner",
        consumes=["task"],
        publishes=["plan"],
        description="Draft plan",
    )
    assert phase.name == "plan"
    assert phase.agent == "planner"
    assert phase.consumes == ["task"]
    assert phase.publishes == ["plan"]
    assert phase.is_terminal is False


def test_phase_validation():
    """Test Phase validation errors."""
    with pytest.raises(ValueError, match="Phase name cannot be empty"):
        Phase(name="", agent="planner")

    with pytest.raises(ValueError, match="Phase agent cannot be empty"):
        Phase(name="plan", agent="")


def test_transition_construction():
    """Test Transition construction with guards."""
    guard = When.channel_has("verdict", "approve")
    transition = Transition(
        from_phase="review",
        to_phase="done",
        when=guard,
        priority=10,
    )
    assert transition.from_phase == "review"
    assert transition.to_phase == "done"
    assert transition.when == guard
    assert transition.priority == 10


def test_transition_validation():
    """Test Transition validation errors."""
    with pytest.raises(ValueError, match="from_phase cannot be empty"):
        Transition(from_phase="", to_phase="done")

    with pytest.raises(ValueError, match="to_phase cannot be empty"):
        Transition(from_phase="review", to_phase="")

    with pytest.raises(TypeError, match="guard must be a Guard instance"):
        Transition(from_phase="review", to_phase="done", when="not a guard")


def test_workflow_construction():
    """Test Workflow construction."""
    workflow = Workflow(
        agents=[Agent(name="planner", provider="codex", model="gpt-5")],
        channels=[Channel(name="task")],
        phases=[
            Phase(name="plan", agent="planner", consumes=["task"]),
            Phase(name="done", agent="planner", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="plan", to_phase="done"),
        ],
    )
    assert workflow.initial_phase == "plan"  # Defaults to first phase
    assert len(workflow.agents) == 1
    assert len(workflow.channels) == 1
    assert len(workflow.phases) == 2
    assert len(workflow.transitions) == 1


def test_workflow_validation():
    """Test Workflow validation errors."""
    with pytest.raises(ValueError, match="at least one agent"):
        Workflow(agents=[], channels=[], phases=[], transitions=[])

    with pytest.raises(ValueError, match="at least one phase"):
        Workflow(
            agents=[Agent(name="a", provider="p", model="m")],
            channels=[],
            phases=[],
            transitions=[],
        )

    with pytest.raises(ValueError, match="at least one transition"):
        Workflow(
            agents=[Agent(name="a", provider="p", model="m")],
            channels=[],
            phases=[Phase(name="p", agent="a")],
            transitions=[],
        )


def test_workflow_initial_phase_validation():
    """Test Workflow initial_phase validation."""
    # Valid initial_phase
    workflow = Workflow(
        agents=[Agent(name="a", provider="p", model="m")],
        channels=[],
        phases=[
            Phase(name="plan", agent="a"),
            Phase(name="done", agent="a", is_terminal=True),
        ],
        transitions=[Transition(from_phase="plan", to_phase="done")],
        initial_phase="plan",
    )
    assert workflow.initial_phase == "plan"

    # Invalid initial_phase
    with pytest.raises(ValueError, match="Initial phase .* not found"):
        Workflow(
            agents=[Agent(name="a", provider="p", model="m")],
            channels=[],
            phases=[Phase(name="plan", agent="a")],
            transitions=[Transition(from_phase="plan", to_phase="plan")],
            initial_phase="nonexistent",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Compiler Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_compile_simple_workflow():
    """Test compiling a simple valid workflow."""
    workflow = Workflow(
        agents=[Agent(name="planner", provider="codex", model="gpt-5")],
        channels=[Channel(name="plan")],
        phases=[
            Phase(name="plan", agent="planner", publishes=["plan"]),
            Phase(name="done", agent="planner", is_terminal=True),
        ],
        transitions=[Transition(from_phase="plan", to_phase="done")],
    )

    graph = compile_workflow(workflow)

    assert "planner" in graph.agents
    assert "plan" in graph.channels
    assert "plan" in graph.phases
    assert "done" in graph.phases
    assert graph.initial_phase == "plan"
    assert "done" in graph.terminal_phases


def test_compile_duplicate_agent_names():
    """Test compiler rejects duplicate agent names."""
    workflow = Workflow(
        agents=[
            Agent(name="agent1", provider="codex", model="gpt-5"),
            Agent(name="agent1", provider="claude", model="sonnet"),  # Duplicate
        ],
        channels=[],
        phases=[Phase(name="p1", agent="agent1")],
        transitions=[Transition(from_phase="p1", to_phase="p1")],
    )

    with pytest.raises(CompilationError, match="Duplicate agent name"):
        compile_workflow(workflow)


def test_compile_duplicate_channel_names():
    """Test compiler rejects duplicate channel names."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[
            Channel(name="ch1"),
            Channel(name="ch1"),  # Duplicate
        ],
        phases=[Phase(name="p1", agent="a1")],
        transitions=[Transition(from_phase="p1", to_phase="p1")],
    )

    with pytest.raises(CompilationError, match="Duplicate channel name"):
        compile_workflow(workflow)


def test_compile_duplicate_phase_names():
    """Test compiler rejects duplicate phase names."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[],
        phases=[
            Phase(name="phase1", agent="a1"),
            Phase(name="phase1", agent="a1"),  # Duplicate
        ],
        transitions=[Transition(from_phase="phase1", to_phase="phase1")],
    )

    with pytest.raises(CompilationError, match="Duplicate phase name"):
        compile_workflow(workflow)


def test_compile_unknown_agent_reference():
    """Test compiler rejects unknown agent references."""
    workflow = Workflow(
        agents=[Agent(name="agent1", provider="codex", model="gpt-5")],
        channels=[],
        phases=[
            Phase(name="plan", agent="unknown_agent"),  # Invalid reference
        ],
        transitions=[Transition(from_phase="plan", to_phase="plan")],
    )

    with pytest.raises(CompilationError, match="references unknown agent"):
        compile_workflow(workflow)


def test_compile_unknown_channel_reference_consumes():
    """Test compiler rejects unknown channel in consumes."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[Channel(name="valid_channel")],
        phases=[
            Phase(name="plan", agent="a1", consumes=["unknown_channel"]),
        ],
        transitions=[Transition(from_phase="plan", to_phase="plan")],
    )

    with pytest.raises(CompilationError, match="consumes unknown channel"):
        compile_workflow(workflow)


def test_compile_unknown_channel_reference_publishes():
    """Test compiler rejects unknown channel in publishes."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[Channel(name="valid_channel")],
        phases=[
            Phase(name="plan", agent="a1", publishes=["unknown_channel"]),
        ],
        transitions=[Transition(from_phase="plan", to_phase="plan")],
    )

    with pytest.raises(CompilationError, match="publishes to unknown channel"):
        compile_workflow(workflow)


def test_compile_unknown_transition_from_phase():
    """Test compiler rejects unknown from_phase in transition."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[],
        phases=[Phase(name="plan", agent="a1")],
        transitions=[
            Transition(from_phase="unknown", to_phase="plan"),  # Invalid
        ],
    )

    with pytest.raises(CompilationError, match="from unknown phase"):
        compile_workflow(workflow)


def test_compile_unknown_transition_to_phase():
    """Test compiler rejects unknown to_phase in transition."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[],
        phases=[Phase(name="plan", agent="a1")],
        transitions=[
            Transition(from_phase="plan", to_phase="unknown"),  # Invalid
        ],
    )

    with pytest.raises(CompilationError, match="to unknown phase"):
        compile_workflow(workflow)


def test_compile_unreachable_phases():
    """Test compiler detects unreachable phases."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[],
        phases=[
            Phase(name="plan", agent="a1"),
            Phase(name="implement", agent="a1"),
            Phase(name="orphan", agent="a1"),  # Unreachable
        ],
        transitions=[
            Transition(from_phase="plan", to_phase="implement"),
            # No path to 'orphan'
        ],
        initial_phase="plan",
    )

    with pytest.raises(CompilationError, match="Unreachable phases"):
        compile_workflow(workflow)


def test_compile_non_terminal_phase_no_transitions():
    """Test compiler detects non-terminal phases with no outgoing transitions."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[],
        phases=[
            Phase(name="start", agent="a1"),
            Phase(name="end", agent="a1"),  # No outgoing, not terminal
        ],
        transitions=[
            Transition(from_phase="start", to_phase="end"),
            # 'end' has no outgoing transitions but not marked terminal
        ],
    )

    with pytest.raises(CompilationError, match="no outgoing transitions but is not marked terminal"):
        compile_workflow(workflow)


def test_compile_complex_workflow():
    """Test compiling a realistic workflow with channels and guards."""
    workflow = Workflow(
        agents=[
            Agent(name="planner", provider="codex", model="gpt-5-codex"),
            Agent(name="implementer", provider="claude", model="sonnet"),
            Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
        ],
        channels=[
            Channel(name="task", schema="text"),
            Channel(name="plan", schema="text"),
            Channel(name="code", schema="git_diff"),
            Channel(name="verdict", schema="verdict"),
        ],
        phases=[
            Phase(name="plan", agent="planner", consumes=["task"], publishes=["plan"]),
            Phase(name="implement", agent="implementer", consumes=["plan"], publishes=["code"]),
            Phase(name="review", agent="reviewer", consumes=["plan", "code"], publishes=["verdict"]),
            Phase(name="done", agent="reviewer", is_terminal=True),
            Phase(name="blocked", agent="reviewer", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="plan", to_phase="implement"),
            Transition(from_phase="implement", to_phase="review"),
            Transition(from_phase="review", to_phase="done", when=When.verdict("approve"), priority=10),
            Transition(from_phase="review", to_phase="plan", when=When.verdict("changes_requested"), priority=5),
            Transition(from_phase="review", to_phase="blocked", when=When.verdict("blocked"), priority=15),
        ],
    )

    graph = compile_workflow(workflow)

    # Verify structure
    assert len(graph.agents) == 3
    assert len(graph.channels) == 4
    assert len(graph.phases) == 5
    assert graph.initial_phase == "plan"
    assert graph.terminal_phases == {"done", "blocked"}

    # Verify transitions sorted by priority
    review_transitions = graph.get_next_transitions("review")
    assert len(review_transitions) == 3
    assert review_transitions[0].priority == 15  # blocked (highest)
    assert review_transitions[1].priority == 10  # done
    assert review_transitions[2].priority == 5   # plan


def test_workflow_get_methods():
    """Test Workflow helper methods."""
    workflow = Workflow(
        agents=[Agent(name="a1", provider="codex", model="gpt-5")],
        channels=[Channel(name="ch1")],
        phases=[
            Phase(name="p1", agent="a1"),
            Phase(name="p2", agent="a1", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="p1", to_phase="p2"),
        ],
    )

    # get_agent
    assert workflow.get_agent("a1") is not None
    assert workflow.get_agent("a1").name == "a1"
    assert workflow.get_agent("nonexistent") is None

    # get_channel
    assert workflow.get_channel("ch1") is not None
    assert workflow.get_channel("ch1").name == "ch1"
    assert workflow.get_channel("nonexistent") is None

    # get_phase
    assert workflow.get_phase("p1") is not None
    assert workflow.get_phase("p1").name == "p1"
    assert workflow.get_phase("nonexistent") is None

    # get_transitions_from
    transitions = workflow.get_transitions_from("p1")
    assert len(transitions) == 1
    assert transitions[0].to_phase == "p2"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
