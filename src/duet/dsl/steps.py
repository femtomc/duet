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
    Step that reads facts/values from dataspace into local context.

    Supports both typed facts (new API) and legacy channel-based reads.

    **Typed Fact API (Preferred):**
        ReadStep(
            fact_type=PlanDoc,
            constraints={"task_id": "123"},
            into="plan"
        )

    **Legacy Channel API:**
        ReadStep(
            channels=[Channel("plan")],
            into=["plan"]
        )

    Attributes:
        channels: (Legacy) Channels to read from
        into: Context keys to store values
        fact_type: (New) Fact type to query from dataspace
        constraints: (New) Constraints for fact query
        latest_only: Whether to return only the latest fact by iteration
    """

    channels: Optional[List[Channel]] = None
    into: Optional[List[str]] = None
    fact_type: Optional[type] = None
    constraints: Optional[Dict[str, Any]] = None
    latest_only: bool = True

    def execute(self, context: FacetContext, dataspace=None) -> StepResult:
        """
        Read facts/values into context.

        Args:
            context: Facet execution context
            dataspace: Dataspace for querying facts (new API)

        Returns:
            StepResult with facts/values in context_updates
        """
        updates = {}

        # New API: query fact_type from dataspace
        if self.fact_type and dataspace:
            from ..dataspace import FactPattern

            pattern = FactPattern(
                fact_type=self.fact_type, constraints=self.constraints or {}
            )
            facts = dataspace.query(pattern, latest_only=self.latest_only)

            # Store facts in context
            if self.into:
                # Single key provided - store first fact or list of facts
                key = self.into[0] if isinstance(self.into, list) else self.into
                if self.latest_only and facts:
                    updates[key] = facts[0]
                else:
                    updates[key] = facts
            else:
                # No key provided - use fact type name
                key = self.fact_type.__name__.lower()
                if self.latest_only and facts:
                    updates[key] = facts[0]
                else:
                    updates[key] = facts

        # Legacy API: read from pre-loaded channel_reads
        elif self.channels:
            for i, channel in enumerate(self.channels):
                # Get value from pre-loaded channel_reads
                value = context.get_channel_value(channel.name)
                # Store in context with key
                key = self.into[i] if self.into and i < len(self.into) else channel.name
                updates[key] = value
        else:
            return StepResult.fail("ReadStep requires either fact_type or channels")

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

    def execute(self, context: FacetContext, dataspace=None) -> StepResult:
        """
        Request human approval by asserting ApprovalNeeded fact.

        Creates conversation: asserts ApprovalNeeded, waits for ApprovalGranted.

        Args:
            context: Facet execution context
            dataspace: Dataspace for asserting approval request

        Returns:
            StepResult with blocked=True (pause for approval)
        """
        if dataspace:
            # Syndicate-style conversation: assert request fact
            from ..dataspace import ApprovalRequest
            import uuid

            request_id = f"approval_{context.run_id}_{context.iteration}_{uuid.uuid4().hex[:8]}"
            request_fact = ApprovalRequest(
                fact_id=request_id,
                requester=context.phase_name,
                reason=self.reason,
                context={
                    "run_id": context.run_id,
                    "iteration": context.iteration,
                    "phase": context.phase_name,
                },
            )

            handle = dataspace.assert_fact(request_fact)
            context.add_handle(handle)

            # Return pause with request_id for orchestrator to track
            return StepResult.pause(f"Awaiting approval: {request_id}")
        else:
            # Legacy: just pause without fact
            return StepResult.pause(f"Human approval needed: {self.reason}")


@dataclass
class WriteStep:
    """
    Step that writes a fact to the dataspace or value to a channel.

    Supports both typed facts (new API) and legacy channel-based writes.

    **Typed Fact API (Preferred):**
        WriteStep(
            fact_type=ReviewVerdict,
            values={"verdict": "approve", "feedback": "Looks good!"},
            fact_id_key="verdict_id"  # Optional: get fact_id from context
        )

    **Legacy Channel API:**
        WriteStep(
            channel=Channel("verdict"),
            value_key="verdict_value"
        )

    Attributes:
        channel: (Legacy) Channel to write to
        value_key: (Legacy) Key in context containing value to write
        static_value: (Legacy) Static value to write
        fact_type: (New) Fact type to construct and assert
        values: (New) Dict of field values for the fact (fact_id is auto-generated if not provided)
        fact_id_key: (New) Context key containing fact_id (optional)
        store_handle_as: (New) Context key to store returned handle (optional)
    """

    channel: Optional[Channel] = None
    value_key: Optional[str] = None
    static_value: Any = None
    fact_type: Optional[type] = None
    values: Optional[Dict[str, Any]] = None
    fact_id_key: Optional[str] = None
    store_handle_as: Optional[str] = None

    def execute(self, context: FacetContext, dataspace=None) -> StepResult:
        """
        Write fact to dataspace or value to channel.

        Args:
            context: Facet execution context
            dataspace: Dataspace for asserting facts (new API)

        Returns:
            StepResult with channel write or fact assertion
        """
        # New API: construct and assert typed fact
        if self.fact_type and dataspace:
            import uuid

            # Build fact values from provided dict + context
            fact_values = {}
            if self.values:
                for key, value in self.values.items():
                    # If value is a string starting with "$", treat as context key
                    if isinstance(value, str) and value.startswith("$"):
                        context_key = value[1:]
                        fact_values[key] = context.get(context_key)
                    else:
                        fact_values[key] = value

            # Get or generate fact_id
            if self.fact_id_key:
                fact_id = context.get(self.fact_id_key)
                if not fact_id:
                    fact_id = f"{self.fact_type.__name__.lower()}_{uuid.uuid4().hex[:8]}"
            elif "fact_id" not in fact_values:
                fact_id = f"{self.fact_type.__name__.lower()}_{uuid.uuid4().hex[:8]}"
            else:
                fact_id = fact_values.get("fact_id")

            # Ensure fact_id is in values
            fact_values["fact_id"] = fact_id

            # Construct fact instance
            try:
                fact = self.fact_type(**fact_values)
            except TypeError as e:
                return StepResult.fail(f"Failed to construct {self.fact_type.__name__}: {e}")

            # Assert fact to dataspace
            handle = dataspace.assert_fact(fact)

            # Track handle in context
            context.add_handle(handle)

            # Optionally store handle in context for later use
            context_updates = {}
            if self.store_handle_as:
                context_updates[self.store_handle_as] = handle

            return StepResult(context_updates=context_updates, success=True)

        # Legacy API: write to channel
        elif self.channel:
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
        else:
            return StepResult.fail("WriteStep requires either fact_type or channel")
