"""
Workflow DSL for defining orchestration flows.

This module provides a Python DSL for defining Duet workflows programmatically,
replacing the legacy .duet/prompts/*.md template system.

Example:
    from duet.dsl import Workflow, Agent, Phase, Transition, When

    workflow = Workflow(
        agents=[
            Agent(name="planner", provider="codex", model="gpt-5-codex"),
            Agent(name="implementer", provider="claude", model="sonnet"),
        ],
        phases=[
            Phase(name="plan", agent="planner", prompt="Draft implementation plan"),
            Phase(name="implement", agent="implementer", prompt="Execute the plan"),
        ],
        transitions=[
            Transition(from_phase="plan", to_phase="implement", when=When.always()),
        ],
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union


# ──────────────────────────────────────────────────────────────────────────────
# Guard System
# ──────────────────────────────────────────────────────────────────────────────


class Guard:
    """
    Base class for transition guards (predicates).

    Guards determine whether a transition should fire based on runtime state.
    """

    def evaluate(self, context: Dict[str, Any]) -> bool:
        """
        Evaluate the guard predicate.

        Args:
            context: Runtime context (response, metadata, git changes, etc.)

        Returns:
            True if the guard condition is met
        """
        raise NotImplementedError


class AlwaysGuard(Guard):
    """Guard that always evaluates to True."""

    def evaluate(self, context: Dict[str, Any]) -> bool:
        return True

    def __repr__(self) -> str:
        return "Always()"


class NeverGuard(Guard):
    """Guard that always evaluates to False."""

    def evaluate(self, context: Dict[str, Any]) -> bool:
        return False

    def __repr__(self) -> str:
        return "Never()"


class ChannelHasGuard(Guard):
    """Guard that checks if a channel (metadata field) has a specific value."""

    def __init__(self, channel: str, value: Any):
        self.channel = channel
        self.value = value

    def evaluate(self, context: Dict[str, Any]) -> bool:
        return context.get(self.channel) == self.value

    def __repr__(self) -> str:
        return f"ChannelHas({self.channel}={self.value})"


class EmptyGuard(Guard):
    """Guard that checks if a channel (metadata field) is empty/None."""

    def __init__(self, channel: str):
        self.channel = channel

    def evaluate(self, context: Dict[str, Any]) -> bool:
        value = context.get(self.channel)
        if value is None:
            return True
        if isinstance(value, (str, list, dict)):
            return len(value) == 0
        return False

    def __repr__(self) -> str:
        return f"Empty({self.channel})"


class VerdictGuard(Guard):
    """Guard that checks the review verdict."""

    def __init__(self, verdict: str):
        self.verdict = verdict.lower()

    def evaluate(self, context: Dict[str, Any]) -> bool:
        actual = context.get("verdict", "").lower()
        return actual == self.verdict

    def __repr__(self) -> str:
        return f"Verdict({self.verdict})"


class GitChangesGuard(Guard):
    """Guard that checks if git changes occurred."""

    def __init__(self, required: bool = True):
        self.required = required

    def evaluate(self, context: Dict[str, Any]) -> bool:
        git_changes = context.get("git_changes", {})
        has_changes = git_changes.get("has_changes", False)
        return has_changes == self.required

    def __repr__(self) -> str:
        return f"GitChanges(required={self.required})"


class AndGuard(Guard):
    """Guard that combines multiple guards with AND logic."""

    def __init__(self, *guards: Guard):
        self.guards = guards

    def evaluate(self, context: Dict[str, Any]) -> bool:
        return all(g.evaluate(context) for g in self.guards)

    def __repr__(self) -> str:
        return f"And({', '.join(repr(g) for g in self.guards)})"


class OrGuard(Guard):
    """Guard that combines multiple guards with OR logic."""

    def __init__(self, *guards: Guard):
        self.guards = guards

    def evaluate(self, context: Dict[str, Any]) -> bool:
        return any(g.evaluate(context) for g in self.guards)

    def __repr__(self) -> str:
        return f"Or({', '.join(repr(g) for g in self.guards)})"


class NotGuard(Guard):
    """Guard that negates another guard."""

    def __init__(self, guard: Guard):
        self.guard = guard

    def evaluate(self, context: Dict[str, Any]) -> bool:
        return not self.guard.evaluate(context)

    def __repr__(self) -> str:
        return f"Not({repr(self.guard)})"


class When:
    """
    Factory for creating guard expressions.

    Provides a fluent API for building guard conditions.
    """

    @staticmethod
    def always() -> Guard:
        """Guard that always passes."""
        return AlwaysGuard()

    @staticmethod
    def never() -> Guard:
        """Guard that never passes."""
        return NeverGuard()

    @staticmethod
    def channel_has(channel: str, value: Any) -> Guard:
        """Guard that checks if channel has specific value."""
        return ChannelHasGuard(channel, value)

    @staticmethod
    def empty(channel: str) -> Guard:
        """Guard that checks if channel is empty."""
        return EmptyGuard(channel)

    @staticmethod
    def verdict(verdict: str) -> Guard:
        """Guard that checks review verdict."""
        return VerdictGuard(verdict)

    @staticmethod
    def git_changes(required: bool = True) -> Guard:
        """Guard that checks if git changes occurred."""
        return GitChangesGuard(required)

    @staticmethod
    def all(*guards: Guard) -> Guard:
        """Combine guards with AND logic."""
        return AndGuard(*guards)

    @staticmethod
    def any(*guards: Guard) -> Guard:
        """Combine guards with OR logic."""
        return OrGuard(*guards)

    @staticmethod
    def not_(guard: Guard) -> Guard:
        """Negate a guard."""
        return NotGuard(guard)


# ──────────────────────────────────────────────────────────────────────────────
# Workflow Components
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Agent:
    """
    Defines an agent (Codex or Claude Code) that can execute phases.

    Attributes:
        name: Unique identifier for the agent
        provider: Provider name (codex, claude, echo)
        model: Model identifier
        timeout: Optional timeout in seconds
        cli_path: Optional custom CLI path
        api_key_env: Optional environment variable for API key
    """

    name: str
    provider: str
    model: str
    timeout: Optional[int] = None
    cli_path: Optional[str] = None
    api_key_env: Optional[str] = None

    def __post_init__(self):
        if not self.name:
            raise ValueError("Agent name cannot be empty")
        if not self.provider:
            raise ValueError("Agent provider cannot be empty")
        if not self.model:
            raise ValueError("Agent model cannot be empty")


@dataclass
class Phase:
    """
    Defines a workflow phase (plan, implement, review, etc.).

    Attributes:
        name: Unique phase identifier
        agent: Name of the agent that executes this phase
        prompt: Prompt template or description
        is_terminal: Whether this phase ends the workflow
    """

    name: str
    agent: str
    prompt: str
    is_terminal: bool = False

    def __post_init__(self):
        if not self.name:
            raise ValueError("Phase name cannot be empty")
        if not self.agent:
            raise ValueError("Phase agent cannot be empty")
        if not self.prompt:
            raise ValueError("Phase prompt cannot be empty")


@dataclass
class Transition:
    """
    Defines a transition between phases with optional guard conditions.

    Attributes:
        from_phase: Source phase name
        to_phase: Target phase name
        when: Guard condition that must evaluate to True
        priority: Priority for conflict resolution (higher = preferred)
    """

    from_phase: str
    to_phase: str
    when: Guard = field(default_factory=AlwaysGuard)
    priority: int = 0

    def __post_init__(self):
        if not self.from_phase:
            raise ValueError("Transition from_phase cannot be empty")
        if not self.to_phase:
            raise ValueError("Transition to_phase cannot be empty")
        if not isinstance(self.when, Guard):
            raise TypeError(f"Transition guard must be a Guard instance, got {type(self.when)}")


@dataclass
class Workflow:
    """
    Top-level workflow definition.

    Attributes:
        agents: List of agent definitions
        phases: List of phase definitions
        transitions: List of transitions between phases
        initial_phase: Name of the starting phase (defaults to first phase)
        metadata: Additional workflow metadata
    """

    agents: List[Agent]
    phases: List[Phase]
    transitions: List[Transition]
    initial_phase: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.agents:
            raise ValueError("Workflow must have at least one agent")
        if not self.phases:
            raise ValueError("Workflow must have at least one phase")
        if not self.transitions:
            raise ValueError("Workflow must have at least one transition")

        # Validate initial_phase
        if self.initial_phase:
            phase_names = {p.name for p in self.phases}
            if self.initial_phase not in phase_names:
                raise ValueError(f"Initial phase '{self.initial_phase}' not found in phases")
        else:
            # Default to first phase
            self.initial_phase = self.phases[0].name

    def get_agent(self, name: str) -> Optional[Agent]:
        """Get agent by name."""
        for agent in self.agents:
            if agent.name == name:
                return agent
        return None

    def get_phase(self, name: str) -> Optional[Phase]:
        """Get phase by name."""
        for phase in self.phases:
            if phase.name == name:
                return phase
        return None

    def get_transitions_from(self, phase_name: str) -> List[Transition]:
        """Get all transitions from a given phase."""
        return [t for t in self.transitions if t.from_phase == phase_name]
