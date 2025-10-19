"""
Workflow DSL for defining orchestration flows.

This module provides a Python DSL for defining Duet workflows programmatically,
replacing the legacy .duet/prompts/*.md template system.

Example:
    from duet.dsl import Workflow, Agent, Phase, Transition, When

    plan = Phase(name="plan", agent=planner)
    implement = Phase(name="implement", agent=implementer)

    workflow = Workflow(
        agents=[
            Agent(name="planner", provider="codex", model="gpt-5-codex"),
            Agent(name="implementer", provider="claude", model="sonnet"),
        ],
        phases=[plan, implement],
        transitions=[
            Transition(from_phase=plan, to_phase=implement, when=When.always()),
        ],
    )
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union


# ──────────────────────────────────────────────────────────────────────────────
# Base Element (UUID-based identity)
# ──────────────────────────────────────────────────────────────────────────────


class BaseElement:
    """
    Base class for DSL elements with stable UUID-based identity.

    Provides:
    - Unique ID generation (UUID)
    - Equality and hashing by ID (not by name)
    - Human-readable name for display

    Subclasses should define 'name' and 'id' fields in their dataclass.
    """

    def __post_init__(self):
        """Generate UUID if not provided."""
        if not hasattr(self, 'id') or self.id is None:
            # Generate unique UUID for each instance
            object.__setattr__(self, 'id', str(uuid.uuid4()))

    def __eq__(self, other):
        """Equality based on ID, not name."""
        if not isinstance(other, BaseElement):
            return False
        return self.id == other.id

    def __hash__(self):
        """Hash based on ID."""
        return hash(self.id)

    def __repr__(self):
        """Readable representation showing name and ID."""
        return f"{self.__class__.__name__}(name='{self.name}', id='{self.id[:8]}...')"


# ──────────────────────────────────────────────────────────────────────────────
# Guard System
# ──────────────────────────────────────────────────────────────────────────────


class Guard:
    """
    Base class for transition guards (predicates).

    Guards determine whether a transition should fire based on runtime state.
    Modern guards query the dataspace for facts; legacy guards use context dict.
    """

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        """
        Evaluate the guard predicate.

        Args:
            context: Runtime context (response, metadata, git changes, etc.) - legacy
            dataspace: Dataspace for querying facts (new API)

        Returns:
            True if the guard condition is met
        """
        raise NotImplementedError


class AlwaysGuard(Guard):
    """Guard that always evaluates to True."""

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        return True

    def __repr__(self) -> str:
        return "Always()"


class NeverGuard(Guard):
    """Guard that always evaluates to False."""

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        return False

    def __repr__(self) -> str:
        return "Never()"


class ChannelHasGuard(Guard):
    """
    Guard that checks if a channel has a specific value.

    Uses dataspace to query ChannelFact if available (new API),
    falls back to context dict (legacy API).
    """

    def __init__(self, channel: Channel, value: Any):
        if not isinstance(channel, Channel):
            raise TypeError(
                f"ChannelHasGuard requires Channel object, got {type(channel)}.\n"
                f"Migration: Define channel as variable:\n"
                f"  verdict = Channel(name='verdict')\n"
                f"  When.channel_has(verdict, '{value}')"
            )
        self.channel_name = channel.name
        self.channel_id = channel.id
        self.value = value

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        # New API: query dataspace for ChannelFact
        if dataspace:
            from ..dataspace import ChannelFact, FactPattern

            pattern = FactPattern(
                fact_type=ChannelFact, constraints={"channel_name": self.channel_name}
            )
            facts = dataspace.query(pattern, latest_only=True)

            if facts:
                return facts[0].value == self.value
            return False

        # Legacy API: use context dict
        return context.get(self.channel_name) == self.value

    def __repr__(self) -> str:
        return f"ChannelHas({self.channel_name}={self.value})"


class EmptyGuard(Guard):
    """Guard that checks if a channel is empty/None."""

    def __init__(self, channel: Channel):
        if not isinstance(channel, Channel):
            raise TypeError(
                f"EmptyGuard requires Channel object, got {type(channel)}. "
                f"Use Channel objects instead of strings."
            )
        self.channel_name = channel.name
        self.channel_id = channel.id

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        # New API: query dataspace for ChannelFact
        if dataspace:
            from ..dataspace import ChannelFact, FactPattern

            pattern = FactPattern(
                fact_type=ChannelFact, constraints={"channel_name": self.channel_name}
            )
            facts = dataspace.query(pattern, latest_only=True)

            if not facts:
                return True

            value = facts[0].value
            if value is None:
                return True
            if isinstance(value, (str, list, dict)):
                return len(value) == 0
            return False

        # Legacy API: use context dict
        value = context.get(self.channel_name)
        if value is None:
            return True
        if isinstance(value, (str, list, dict)):
            return len(value) == 0
        return False

    def __repr__(self) -> str:
        return f"Empty({self.channel_name})"


class VerdictGuard(Guard):
    """Guard that checks the review verdict."""

    def __init__(self, verdict: str):
        self.verdict = verdict.lower()

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        # Legacy only - uses context dict
        actual = context.get("verdict", "").lower()
        return actual == self.verdict

    def __repr__(self) -> str:
        return f"Verdict({self.verdict})"


class GitChangesGuard(Guard):
    """Guard that checks if git changes occurred."""

    def __init__(self, required: bool = True):
        self.required = required

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        # Legacy only - uses context dict
        git_changes = context.get("git_changes", {})
        has_changes = git_changes.get("has_changes", False)
        return has_changes == self.required

    def __repr__(self) -> str:
        return f"GitChanges(required={self.required})"


class AndGuard(Guard):
    """Guard that combines multiple guards with AND logic."""

    def __init__(self, *guards: Guard):
        self.guards = guards

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        return all(g.evaluate(context, dataspace) for g in self.guards)

    def __repr__(self) -> str:
        return f"And({', '.join(repr(g) for g in self.guards)})"


class OrGuard(Guard):
    """Guard that combines multiple guards with OR logic."""

    def __init__(self, *guards: Guard):
        self.guards = guards

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        return any(g.evaluate(context, dataspace) for g in self.guards)

    def __repr__(self) -> str:
        return f"Or({', '.join(repr(g) for g in self.guards)})"


class NotGuard(Guard):
    """Guard that negates another guard."""

    def __init__(self, guard: Guard):
        self.guard = guard

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        return not self.guard.evaluate(context, dataspace)

    def __repr__(self) -> str:
        return f"Not({repr(self.guard)})"


class FactExistsGuard(Guard):
    """
    Guard that checks if a fact of specific type exists in dataspace.

    **Typed Fact Guard (New API):**
        When.fact_exists(ReviewVerdict, constraints={"verdict": "approve"})
    """

    def __init__(self, fact_type: type, constraints: Optional[Dict[str, Any]] = None):
        self.fact_type = fact_type
        self.constraints = constraints or {}

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        if not dataspace:
            # Cannot evaluate without dataspace
            return False

        from ..dataspace import FactPattern

        pattern = FactPattern(fact_type=self.fact_type, constraints=self.constraints)
        facts = dataspace.query(pattern)
        return len(facts) > 0

    def __repr__(self) -> str:
        constraints_str = ", ".join(f"{k}={v}" for k, v in self.constraints.items())
        if constraints_str:
            return f"FactExists({self.fact_type.__name__}, {constraints_str})"
        return f"FactExists({self.fact_type.__name__})"


class FactMatchesGuard(Guard):
    """
    Guard that checks if a fact matches specific criteria using a predicate function.

    **Advanced Fact Guard:**
        When.fact_matches(
            ReviewVerdict,
            lambda fact: fact.verdict == "approve" and fact.feedback is not None
        )
    """

    def __init__(self, fact_type: type, predicate: Callable[[Any], bool]):
        self.fact_type = fact_type
        self.predicate = predicate

    def evaluate(self, context: Dict[str, Any], dataspace=None) -> bool:
        if not dataspace:
            return False

        from ..dataspace import FactPattern

        pattern = FactPattern(fact_type=self.fact_type)
        facts = dataspace.query(pattern)

        # Check if any fact matches the predicate
        return any(self.predicate(fact) for fact in facts)

    def __repr__(self) -> str:
        return f"FactMatches({self.fact_type.__name__}, <predicate>)"


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

    @staticmethod
    def fact_exists(fact_type: type, constraints: Optional[Dict[str, Any]] = None) -> Guard:
        """
        Guard that checks if a fact of specific type exists in dataspace.

        **Typed Fact Guard (New API):**
            When.fact_exists(ReviewVerdict, constraints={"verdict": "approve"})

        Args:
            fact_type: The fact type to query
            constraints: Optional dict of field constraints

        Returns:
            FactExistsGuard instance
        """
        return FactExistsGuard(fact_type, constraints)

    @staticmethod
    def fact_matches(fact_type: type, predicate: Callable[[Any], bool]) -> Guard:
        """
        Guard that checks if a fact matches specific criteria using a predicate.

        **Advanced Fact Guard:**
            When.fact_matches(
                ReviewVerdict,
                lambda fact: fact.verdict == "approve" and fact.feedback is not None
            )

        Args:
            fact_type: The fact type to query
            predicate: Function that takes a fact and returns bool

        Returns:
            FactMatchesGuard instance
        """
        return FactMatchesGuard(fact_type, predicate)


# ──────────────────────────────────────────────────────────────────────────────
# Workflow Components
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Agent:
    """
    Defines an agent (Codex or Claude Code) that can execute phases.

    Attributes:
        name: Unique identifier for the agent
        provider: Provider name (codex, claude-code, echo)
        model: Model identifier
        timeout: Optional timeout in seconds
        cli_path: Optional custom CLI path
        api_key_env: Optional environment variable for API key
        auto_approve: Skip permission prompts (Claude Code only, use with caution)
        description: Human-readable description (optional)
    """

    name: str
    provider: str
    model: str
    timeout: Optional[int] = None
    cli_path: Optional[str] = None
    api_key_env: Optional[str] = None
    auto_approve: bool = False
    description: str = ""

    def __post_init__(self):
        if not self.name:
            raise ValueError("Agent name cannot be empty")
        if not self.provider:
            raise ValueError("Agent provider cannot be empty")
        if not self.model:
            raise ValueError("Agent model cannot be empty")

    def to_adapter_config(self) -> dict:
        """
        Convert Agent to adapter configuration kwargs.

        Returns a dictionary suitable for passing to adapter constructors.
        """
        config = {
            "provider": self.provider,
            "model": self.model,
        }
        if self.timeout is not None:
            config["timeout"] = self.timeout
        if self.cli_path is not None:
            config["cli_path"] = self.cli_path
        if self.api_key_env is not None:
            config["api_key_env"] = self.api_key_env
        if self.auto_approve:
            config["auto_approve"] = self.auto_approve

        return config


@dataclass(eq=False)
class Channel(BaseElement):
    """
    Defines a communication channel for message passing between phases.

    Channels are the fundamental unit of communication in the syndicated workspace.
    Phases consume messages from channels and publish results to channels.

    Attributes:
        name: Unique channel identifier (human-readable)
        id: Stable UUID for identity (auto-generated if not provided)
        description: Human-readable description of the channel's purpose
        initial_value: Optional initial value for the channel
        schema: Optional schema/type metadata for validation and persistence
                Examples: "text", "json", "git_diff", "verdict", "dict", "list"
    """

    name: str
    id: Optional[str] = None
    description: str = ""
    initial_value: Any = None
    schema: Optional[str] = None

    def __post_init__(self):
        if not self.name:
            raise ValueError("Channel name cannot be empty")
        # Generate ID if not provided (from BaseElement)
        BaseElement.__post_init__(self)


@dataclass(eq=False)
class Phase(BaseElement):
    """
    Defines a workflow phase with channel-based message passing.

    Phases consume messages from input channels and publish results to output
    channels, enabling a syndicated workspace model where agents communicate
    through structured data rather than prompt templates.

    Sprint DSL-2+: Moving toward facet-based reactive execution. Metadata flags
    are being phased out in favor of explicit tool/step declarations via fluent API.

    Attributes:
        name: Unique phase identifier (human-readable)
        id: Stable UUID for identity (auto-generated if not provided)
        agent: Name of the agent that executes this phase
        consumes: List of Channel objects this phase reads from
        publishes: List of Channel objects this phase writes to
        description: Human-readable description of what this phase does
        is_terminal: Whether this phase ends the workflow
        tools: List of Tool instances attached to this phase
        metadata: Generic metadata dict (preserved for backward compat, no special keys enforced)
    """

    name: str
    agent: str
    id: Optional[str] = None
    description: str = ""
    is_terminal: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    steps: List[Any] = field(default_factory=list)  # List[PhaseStep] - facet script

    def __post_init__(self):
        if not self.name:
            raise ValueError("Phase name cannot be empty")
        if not self.agent:
            raise ValueError("Phase agent cannot be empty")

        # Generate ID if not provided (from BaseElement)
        BaseElement.__post_init__(self)

    # ──── Fluent Builder API (Sprint DSL-2) ────

    def describe(self, text: str) -> Phase:
        """
        Set phase description (fluent API).

        Returns a new Phase instance with description set (copy-on-write).
        """
        from dataclasses import replace
        return replace(self, description=text)

    def terminal(self) -> Phase:
        """
        Mark this phase as terminal (fluent API).

        Returns a new Phase instance marked as terminal (copy-on-write).
        """
        from dataclasses import replace
        return replace(self, is_terminal=True)

    def with_metadata(self, **kwargs) -> Phase:
        """
        Add metadata entries (fluent API).

        Returns a new Phase instance with metadata merged (copy-on-write).
        """
        from dataclasses import replace
        new_metadata = {**self.metadata, **kwargs}
        return replace(self, metadata=new_metadata)

    # ──── Policy Helpers (Sprint DSL-2+) ────

    def with_human(self, reason: str = "Human approval required") -> Phase:
        """
        Require human approval before proceeding from this phase (fluent API).

        Adds HumanStep to facet script.

        Returns a new Phase instance with HumanStep added.
        """
        return self.human(reason)

    def requires_git(self) -> Phase:
        """
        Require git changes from this phase (fluent API).

        Adds GitChangeTool as ToolStep to facet script.

        Returns a new Phase instance with GitChangeTool added.
        """
        from .tools import GitChangeTool

        return self.tool(GitChangeTool(require_changes=True))

    def counts_as_replan(self, loop_to: Optional[Phase] = None) -> Phase:
        """
        Mark transitions from this phase as replans (fluent API).

        Sprint DSL-2+: Deprecated. Replan logic will be replaced by conversation
        patterns in the dataspace model. Kept for API compatibility but has no runtime effect.

        Args:
            loop_to: Optional target phase that forms the replan loop

        Returns a new Phase instance (unchanged - this is a no-op now).
        """
        # No-op: replan tracking will be reimplemented as conversations
        return self


    # ──── Step-Based Fluent API (Sprint DSL-3) ────

    def read(self, *channels: Channel, into: Optional[List[str]] = None) -> Phase:
        """
        Add ReadStep to facet script (Sprint DSL-3).

        Reads channel values into local context for use by subsequent steps.

        Args:
            channels: Channels to read from
            into: Optional context keys to store values (defaults to channel names)

        Returns a new Phase instance with ReadStep appended.

        Example:
            phase.read(task, feedback, into=["task_input", "review_notes"])
        """
        from dataclasses import replace
        from .steps import ReadStep

        step = ReadStep(channels=list(channels), into=into)
        new_steps = list(self.steps) + [step]
        return replace(self, steps=new_steps)

    def tool(self, tool: Any, outputs: Optional[List[Channel]] = None, into_context: bool = True) -> Phase:
        """
        Add ToolStep to facet script (Sprint DSL-3).

        Executes deterministic tool. Results go into local context by default.
        If outputs specified, also writes to those channels.

        Args:
            tool: Tool instance to execute
            outputs: Channel objects to write tool results to (optional)
            into_context: Whether to merge tool results into local context (default: True)

        Returns a new Phase instance with ToolStep appended.

        Example:
            phase.tool(GitChangeTool())  # Context only
            phase.tool(ValidationTool(), outputs=[status_channel])  # Context + channel write
        """
        from dataclasses import replace
        from .steps import ToolStep

        step = ToolStep(tool=tool, outputs=outputs or [], into_context=into_context)
        new_steps = list(self.steps) + [step]
        return replace(self, steps=new_steps)

    def call_agent(self, agent_name: str, writes: List[Channel], prompt: Optional[str] = None, role: Optional[str] = None) -> Phase:
        """
        Add AgentStep to facet script (Sprint DSL-3).

        Invokes AI agent with context, writes response to specified channels.

        Args:
            agent_name: Name of agent to invoke
            writes: Channels to write agent response to
            prompt: Optional custom prompt template
            role: Optional role hint for prompt builder

        Returns a new Phase instance with AgentStep appended.

        Example:
            phase.call_agent("planner", writes=[plan_channel], role="planner")
        """
        from dataclasses import replace
        from .steps import AgentStep

        step = AgentStep(agent=agent_name, writes=writes, prompt_template=prompt, role=role)
        new_steps = list(self.steps) + [step]
        return replace(self, steps=new_steps, agent=agent_name)  # Also set phase.agent

    def human(self, reason: str, reads: Optional[List[Channel]] = None, timeout: Optional[int] = None) -> Phase:
        """
        Add HumanStep to facet script (Sprint DSL-3).

        Requires human interaction/approval. Suspends execution until human responds.

        Args:
            reason: Human-readable reason for approval
            reads: Channels to present to human
            timeout: Optional timeout in seconds

        Returns a new Phase instance with HumanStep appended.

        Example:
            phase.human("QA approval required", reads=[plan_channel, code])
        """
        from dataclasses import replace
        from .steps import HumanStep

        step = HumanStep(reason=reason, reads=reads or [], timeout=timeout)
        new_steps = list(self.steps) + [step]
        return replace(self, steps=new_steps)

    def write(self, channel: Channel, value_key: Optional[str] = None, value: Any = None) -> Phase:
        """
        Add WriteStep to facet script (Sprint DSL-3).

        Explicitly writes value to channel (direct fact assertion).

        Args:
            channel: Channel to write to
            value_key: Context key containing value to write
            value: Static value to write (alternative to value_key)

        Returns a new Phase instance with WriteStep appended.

        Example:
            phase.write(status_channel, value="complete")
        """
        from dataclasses import replace
        from .steps import WriteStep

        step = WriteStep(channel=channel, value_key=value_key, static_value=value)
        new_steps = list(self.steps) + [step]
        return replace(self, steps=new_steps)

    # ──── Step Validation (Sprint DSL-3+) ────

    def validate_step_ordering(self) -> List[str]:
        """
        Validate that steps are in a sensible order.

        Rules:
        - AgentStep/ToolStep should have prior ReadStep (need inputs)
        - Multiple AgentSteps need separation (ambiguous dataflow)
        - WriteStep after HumanStep is suspicious (human should be last gate)

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        try:
            from .steps import ReadStep, ToolStep, AgentStep, HumanStep, WriteStep
        except ImportError:
            return errors

        has_read = False
        agent_count = 0
        human_found = False

        for i, step in enumerate(self.steps):
            if isinstance(step, ReadStep):
                has_read = True

            elif isinstance(step, AgentStep):
                if not has_read:
                    errors.append(
                        f"Step {i} (AgentStep) should have prior ReadStep to load inputs"
                    )
                agent_count += 1
                if agent_count > 1:
                    errors.append(
                        f"Multiple AgentSteps in phase '{self.name}' - ambiguous dataflow. "
                        f"Use separate phases or explicit WriteSteps between agents."
                    )

            elif isinstance(step, ToolStep):
                # Tools can run without reads (e.g., git check), but warn if none
                pass

            elif isinstance(step, HumanStep):
                human_found = True

            elif isinstance(step, WriteStep):
                if human_found:
                    errors.append(
                        f"WriteStep after HumanStep in phase '{self.name}' - "
                        f"human approval should typically be the final gate"
                    )

        return errors

    # ──── Step Introspection (Sprint DSL-3) ────

    def get_reads(self) -> List[Channel]:
        """
        Extract channels read by this phase from its step list.

        Analyzes ReadStep, ToolStep.consumes, HumanStep.reads.

        Returns:
            List of Channel objects this phase reads from
        """
        reads = []
        seen = set()

        from .steps import ReadStep, ToolStep, HumanStep

        for step in self.steps:
            if isinstance(step, ReadStep):
                for channel in step.channels:
                    if channel.id not in seen:
                        reads.append(channel)
                        seen.add(channel.id)
            elif isinstance(step, ToolStep) and hasattr(step.tool, 'consumes'):
                for channel in step.tool.consumes:
                    if channel.id not in seen:
                        reads.append(channel)
                        seen.add(channel.id)
            elif isinstance(step, HumanStep):
                for channel in step.reads:
                    if channel.id not in seen:
                        reads.append(channel)
                        seen.add(channel.id)

        return reads

    def get_writes(self) -> List[Channel]:
        """
        Extract channels written by this phase from its step list.

        Analyzes ToolStep.outputs, AgentStep.writes, WriteStep.

        Returns:
            List of Channel objects this phase writes to
        """
        writes = []
        seen = set()

        from .steps import ToolStep, AgentStep, WriteStep

        for step in self.steps:
            if isinstance(step, ToolStep):
                for channel in step.outputs:
                    if channel.id not in seen:
                        writes.append(channel)
                        seen.add(channel.id)
            elif isinstance(step, AgentStep):
                for channel in step.writes:
                    if channel.id not in seen:
                        writes.append(channel)
                        seen.add(channel.id)
            elif isinstance(step, WriteStep):
                if step.channel.id not in seen:
                    writes.append(step.channel)
                    seen.add(step.channel.id)

        return writes

    # ──── Convenience Constructors (Sprint DSL-2) ────

    @classmethod
    def terminal_phase(cls, name: str, agent: str, description: str = "") -> Phase:
        """
        Create a terminal phase (convenience constructor).

        Args:
            name: Phase name
            agent: Agent name
            description: Optional description

        Returns:
            Terminal Phase instance
        """
        return cls(
            name=name,
            agent=agent,
            description=description,
            is_terminal=True,
        )


@dataclass
class Transition:
    """
    Defines a transition between phases with optional guard conditions.

    Attributes:
        from_phase: Source Phase object (not name string)
        to_phase: Target Phase object (not name string)
        when: Guard condition that must evaluate to True
        priority: Priority for conflict resolution (higher = preferred)
    """

    from_phase: Phase
    to_phase: Phase
    when: Guard = field(default_factory=AlwaysGuard)
    priority: int = 0

    def __post_init__(self):
        # Validate that phases are provided and are Phase objects
        if not self.from_phase:
            raise ValueError("Transition from_phase cannot be empty")
        if not self.to_phase:
            raise ValueError("Transition to_phase cannot be empty")

        # Strict type checking - require Phase objects
        if not isinstance(self.from_phase, Phase):
            raise TypeError(
                f"Transition from_phase must be Phase object, got {type(self.from_phase)}.\n"
                f"Migration: Define phases as variables, then reference them:\n"
                f"  plan = Phase(name='plan', ...)\n"
                f"  Transition(from_phase=plan, to_phase=...)"
            )

        if not isinstance(self.to_phase, Phase):
            raise TypeError(
                f"Transition to_phase must be Phase object, got {type(self.to_phase)}.\n"
                f"Migration: Define phases as variables, then reference them:\n"
                f"  implement = Phase(name='implement', ...)\n"
                f"  Transition(from_phase=..., to_phase=implement)"
            )

        if not isinstance(self.when, Guard):
            raise TypeError(f"Transition guard must be a Guard instance, got {type(self.when)}")


@dataclass
class Workflow:
    """
    Top-level workflow definition with syndicated workspace model.

    Defines agents, channels for message passing, phases that consume/publish
    to channels, and transitions between phases.

    Attributes:
        agents: List of agent definitions
        channels: List of channel definitions for message passing
        phases: List of phase definitions
        transitions: List of transitions between phases
        initial_phase: Phase object to start with (defaults to first phase)
        task_channel: Channel object to seed with initial task input (defaults to auto-detection)
        metadata: Additional workflow metadata
    """

    agents: List[Agent]
    channels: List[Channel]
    phases: List[Phase]
    transitions: List[Transition]
    initial_phase: Optional[Phase] = None
    task_channel: Optional[Channel] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.agents:
            raise ValueError("Workflow must have at least one agent")
        if not self.phases:
            raise ValueError("Workflow must have at least one phase")
        if not self.transitions:
            raise ValueError("Workflow must have at least one transition")
        # Note: channels can be empty for simple workflows

        # Validate initial_phase
        if self.initial_phase is None:
            # Default to first phase
            self.initial_phase = self.phases[0]
        elif not isinstance(self.initial_phase, Phase):
            raise TypeError(
                f"initial_phase must be Phase object, got {type(self.initial_phase)}. "
                f"Use Phase objects instead of strings."
            )

        # Validate task_channel
        if self.task_channel is not None and not isinstance(self.task_channel, Channel):
            raise TypeError(
                f"task_channel must be Channel object, got {type(self.task_channel)}. "
                f"Use Channel objects instead of strings."
            )

    def get_agent(self, name: str) -> Optional[Agent]:
        """Get agent by name."""
        for agent in self.agents:
            if agent.name == name:
                return agent
        return None

    def get_channel(self, name: str) -> Optional[Channel]:
        """Get channel by name."""
        for channel in self.channels:
            if channel.name == name:
                return channel
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
