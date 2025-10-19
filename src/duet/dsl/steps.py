"""
Phase step model for facet-based execution (Sprint DSL-3).

Phases are now explicit facet scripts - ordered sequences of steps:
  read → tool → agent → human → write

Each step declares its inputs/outputs explicitly, enabling deterministic
execution and clear dataflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from .workflow import Channel


# ──────────────────────────────────────────────────────────────────────────────
# Facet Context (Local Execution State)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class FacetContext:
    """
    Local execution context for a facet script.

    Tracks intermediate results as steps execute, separate from global
    dataspace. Steps query dataspace for facts, accumulate results
    in local context, then assert facts back to dataspace.

    Attributes:
        phase_name: Name of the phase executing
        run_id: Current run identifier
        iteration: Current iteration number
        local_state: Dict storing step results (key -> value)
        fact_reads: Facts read from dataspace at start (fact snapshots)
        workspace_root: Workspace directory path
        metadata: Additional context metadata
        handles: Handles for facts asserted by this facet (for retraction)
    """

    phase_name: str
    run_id: str
    iteration: int
    local_state: Dict[str, Any] = field(default_factory=dict)
    fact_reads: Dict[str, Any] = field(default_factory=dict)  # channel_name -> fact/value
    workspace_root: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    handles: List[Any] = field(default_factory=list)  # List[Handle] for retraction

    def get(self, key: str, default: Any = None) -> Any:
        """Get value from local state."""
        return self.local_state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set value in local state."""
        self.local_state[key] = value

    def get_fact(self, channel_name: str, default: Any = None) -> Any:
        """Get fact/value read from dataspace."""
        return self.fact_reads.get(channel_name, default)

    def add_handle(self, handle: Any) -> None:
        """Track a handle for later retraction."""
        self.handles.append(handle)

    # Backward compat alias
    def get_channel_value(self, channel_name: str, default: Any = None) -> Any:
        """Alias for get_fact (backward compat)."""
        return self.get_fact(channel_name, default)

    # Backward compat alias
    @property
    def channel_reads(self) -> Dict[str, Any]:
        """Alias for fact_reads (backward compat)."""
        return self.fact_reads


# ──────────────────────────────────────────────────────────────────────────────
# Phase Step Protocol
# ──────────────────────────────────────────────────────────────────────────────


class PhaseStep(Protocol):
    """
    Protocol for phase execution steps.

    Steps are executed in order by the facet runner. Each step can:
    - Read from channels (via context.channel_reads)
    - Execute deterministic logic
    - Update local context
    - Declare channel writes (applied after step completes)
    """

    def execute(self, context: FacetContext) -> StepResult:
        """
        Execute this step.

        Args:
            context: Local facet execution context

        Returns:
            StepResult with updates and metadata
        """
        ...


@dataclass
class StepResult:
    """
    Result from executing a phase step.

    Contains:
    - Context updates (local state changes)
    - Channel writes to apply
    - Metadata to merge
    - Success/failure status
    - Blocked flag (for human approval pauses)
    """

    context_updates: Dict[str, Any] = field(default_factory=dict)
    channel_writes: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    success: bool = True
    blocked: bool = False  # True for human approval pauses (not failures)
    error: Optional[str] = None
    notes: Optional[str] = None

    @classmethod
    def ok(cls, **context_updates) -> StepResult:
        """Create successful result with context updates."""
        return cls(context_updates=context_updates, success=True)

    @classmethod
    def fail(cls, error: str) -> StepResult:
        """Create failed result."""
        return cls(success=False, error=error)

    @classmethod
    def pause(cls, reason: str) -> StepResult:
        """Create paused result (human approval needed)."""
        return cls(success=True, blocked=True, notes=reason)


# ──────────────────────────────────────────────────────────────────────────────
# Concrete Step Types
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ReadStep:
    """
    Step that reads values from channels into local context.

    Declarative subscription to channel facts. Values are materialized
    at step execution time from the global channel/dataspace state.

    Attributes:
        channels: Channels to read from
        into: Optional context keys to store values (defaults to channel names)
    """

    channels: List[Channel]
    into: Optional[List[str]] = None

    def execute(self, context: FacetContext) -> StepResult:
        """
        Read channel values into context.

        Args:
            context: Facet execution context

        Returns:
            StepResult with channel values in context_updates
        """
        updates = {}
        for i, channel in enumerate(self.channels):
            # Get value from pre-loaded channel_reads
            value = context.get_channel_value(channel.name)
            # Store in context with key
            key = self.into[i] if self.into and i < len(self.into) else channel.name
            updates[key] = value

        return StepResult(context_updates=updates, success=True)


@dataclass
class ToolStep:
    """
    Step that executes a deterministic tool.

    Tools read from context, perform logic, and write results back to context
    or channels explicitly via outputs parameter.

    Attributes:
        tool: Tool instance to execute
        outputs: Channels to write tool results to (explicit channel writes)
        into_context: Whether to merge tool results into local context (default: True)
    """

    tool: Any  # Type: Tool - avoid circular import
    outputs: List[Channel] = field(default_factory=list)
    into_context: bool = True

    def execute(self, context: FacetContext) -> StepResult:
        """
        Execute tool and merge results.

        Tool results go into local context by default. If outputs are specified,
        also stage channel writes. This allows tools to enrich context without
        forcing external writes.

        Args:
            context: Facet execution context

        Returns:
            StepResult with tool outputs
        """
        # Import here to avoid circular dependency
        from .tools import ToolContext

        # Build tool context from facet context
        tool_context = ToolContext(
            run_id=context.run_id,
            iteration=context.iteration,
            phase_name=context.phase_name,
            channel_state=context.channel_reads,
            workspace_root=context.workspace_root,
            metadata=context.metadata,
        )

        # Execute tool
        tool_result = self.tool.run(tool_context)

        if not tool_result.success:
            return StepResult.fail(tool_result.error or "Tool execution failed")

        # Tool results are now split:
        # - context_updates: enrich local facet context (for prompt building, etc.)
        # - channel_updates: write to global dataspace (if tool declares them)

        # Merge context updates if requested
        context_updates = tool_result.context_updates if self.into_context else {}

        # Channel writes from tool's channel_updates OR explicit outputs mapping
        channel_writes = {}
        if self.outputs:
            # Map tool channel_updates to declared output channels
            for channel in self.outputs:
                if channel.name in tool_result.channel_updates:
                    channel_writes[channel.name] = tool_result.channel_updates[channel.name]
        else:
            # No outputs declared - use tool's channel_updates directly
            channel_writes = tool_result.channel_updates

        return StepResult(
            context_updates=context_updates,
            channel_writes=channel_writes,
            metadata=tool_result.metadata,
            success=True,
            notes=tool_result.notes,
        )


@dataclass
class AgentStep:
    """
    Step that invokes an AI agent/assistant.

    Reads from context, builds prompt, calls agent, writes response to channels.

    Attributes:
        agent: Name of agent to invoke (references workflow agent)
        writes: Channels to write agent response to
        prompt_template: Optional custom prompt (uses default builder if None)
        role: Optional role hint for prompt builder
    """

    agent: str
    writes: List[Channel]
    prompt_template: Optional[str] = None
    role: Optional[str] = None

    def execute(self, context: FacetContext) -> StepResult:
        """
        Invoke agent (stub - real execution in orchestrator).

        Args:
            context: Facet execution context

        Returns:
            StepResult indicating agent should be called
        """
        # Stub: actual agent invocation happens in orchestrator
        # This step just declares intent
        return StepResult(
            metadata={"agent_step": True, "agent": self.agent},
            notes=f"Agent '{self.agent}' invocation pending",
            success=True,
        )


@dataclass
class HumanStep:
    """
    Step that requires human interaction/approval.

    **Pause Semantics:**
    HumanStep intentionally pauses facet execution by returning
    StepResult.pause() (blocked=True). This is NOT a failure - it signals
    the orchestrator to suspend the workflow and wait for manual intervention.

    The orchestrator will:
    1. Mark run as blocked with approval_reason
    2. Stop executing further steps
    3. Wait for 'duet next' with approval/feedback

    **Future:** Will create ApprovalRequest/ApprovalGrant conversation facts
    in the dataspace for reactive approval workflows.

    Attributes:
        reason: Human-readable reason for approval
        reads: Channels to present to human for context
        timeout: Optional timeout in seconds
    """

    reason: str
    reads: List[Channel] = field(default_factory=list)
    timeout: Optional[int] = None

    def execute(self, context: FacetContext) -> StepResult:
        """
        Request human approval by pausing execution.

        Args:
            context: Facet execution context

        Returns:
            StepResult with blocked=True (pause, not failure)
        """
        # Use pause() to distinguish from failures
        return StepResult.pause(f"Human approval needed: {self.reason}")


@dataclass
class WriteStep:
    """
    Step that writes value to a channel explicitly.

    Direct channel assertion. In future sprints, this becomes assert_fact().

    Attributes:
        channel: Channel to write to
        value_key: Key in context containing value to write
        static_value: Optional static value to write (instead of context lookup)
    """

    channel: Channel
    value_key: Optional[str] = None
    static_value: Any = None

    def execute(self, context: FacetContext) -> StepResult:
        """
        Write value to channel.

        Args:
            context: Facet execution context

        Returns:
            StepResult with channel write
        """
        # Get value from context or use static
        if self.static_value is not None:
            value = self.static_value
        elif self.value_key:
            value = context.get(self.value_key)
        else:
            # Default: use channel name as context key
            value = context.get(self.channel.name)

        return StepResult(
            channel_writes={self.channel.name: value},
            success=True,
        )
