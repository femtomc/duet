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
    - Metadata to merge
    - Success/failure status
    - Blocked flag (for human approval pauses)
    """

    context_updates: Dict[str, Any] = field(default_factory=dict)
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
    def pause(cls, reason: str, request_id: Optional[str] = None) -> StepResult:
        """
        Create paused result (human approval needed).

        Args:
            reason: Human-readable reason for pause
            request_id: Optional approval request ID for scheduler tracking
        """
        metadata = {}
        if request_id:
            metadata["approval_request_id"] = request_id
        return cls(success=True, blocked=True, notes=reason, metadata=metadata)


# ──────────────────────────────────────────────────────────────────────────────
# Concrete Step Types
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ReadStep:
    """
    Step that reads typed facts from dataspace into local context.

    **Usage:**
        ReadStep(
            fact_type=PlanDoc,
            constraints={"task_id": "123"},
            into="plan"
        )

    Attributes:
        fact_type: Fact type to query from dataspace (required)
        into: Context key to store value (defaults to lowercase fact type name)
        constraints: Constraints for fact query
        latest_only: Whether to return only the latest fact by iteration
    """

    fact_type: type
    into: Optional[str] = None
    constraints: Optional[Dict[str, Any]] = None
    latest_only: bool = True

    def execute(self, context: FacetContext, dataspace) -> StepResult:
        """
        Read facts into context.

        Args:
            context: Facet execution context
            dataspace: Dataspace for querying facts (required)

        Returns:
            StepResult with facts in context_updates
        """
        if not dataspace:
            return StepResult.fail("ReadStep requires dataspace")

        from ..dataspace import FactPattern

        pattern = FactPattern(
            fact_type=self.fact_type, constraints=self.constraints or {}
        )
        facts = dataspace.query(pattern, latest_only=self.latest_only)

        # Store facts in context
        if self.into:
            key = self.into
            if self.latest_only and facts:
                updates = {key: facts[0]}
            else:
                updates = {key: facts}
        else:
            # No key provided - use fact type name
            key = self.fact_type.__name__.lower()
            if self.latest_only and facts:
                updates = {key: facts[0]}
            else:
                updates = {key: facts}

        return StepResult(context_updates=updates, success=True)


@dataclass
class ToolStep:
    """
    Step that executes a deterministic tool.

    Tools read from context, perform logic, and write results back to context.
    Use WriteStep to assert tool results as typed facts.

    Attributes:
        tool: Tool instance to execute
        into_context: Whether to merge tool results into local context (default: True)
    """

    tool: Any  # Type: Tool - avoid circular import
    into_context: bool = True

    def execute(self, context: FacetContext) -> StepResult:
        """
        Execute tool and merge results into context.

        Tool results go into local context for use by subsequent steps.
        Use WriteStep to assert tool results as typed facts.

        Args:
            context: Facet execution context

        Returns:
            StepResult with tool outputs in context_updates
        """
        # Import here to avoid circular dependency
        from .tools import ToolContext

        # Build tool context from facet context
        tool_context = ToolContext(
            run_id=context.run_id,
            iteration=context.iteration,
            phase_name=context.phase_name,
            channel_state=context.fact_reads,
            workspace_root=context.workspace_root,
            metadata=context.metadata,
        )

        # Execute tool
        tool_result = self.tool.run(tool_context)

        if not tool_result.success:
            return StepResult.fail(tool_result.error or "Tool execution failed")

        # Merge context updates if requested
        context_updates = tool_result.context_updates if self.into_context else {}

        return StepResult(
            context_updates=context_updates,
            metadata=tool_result.metadata,
            success=True,
            notes=tool_result.notes,
        )


@dataclass
class AgentStep:
    """
    Step that invokes an AI agent/assistant.

    Reads from context, builds prompt, calls agent. Agent response stored
    in context as 'agent_response' for use by subsequent WriteSteps.

    Attributes:
        agent: Name of agent to invoke (references workflow agent)
        prompt_template: Optional custom prompt (uses default builder if None)
        role: Optional role hint for prompt builder
    """

    agent: str
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
    HumanStep pauses facet execution by returning StepResult.pause() (blocked=True).
    This signals the orchestrator to suspend the workflow and wait for approval.

    Creates ApprovalRequest fact in dataspace, which is persisted to DB.
    Scheduler subscribes to matching ApprovalGrant fact to resume execution.

    Attributes:
        reason: Human-readable reason for approval
        timeout: Optional timeout in seconds
    """

    reason: str
    timeout: Optional[int] = None

    def execute(self, context: FacetContext, dataspace) -> StepResult:
        """
        Request human approval by asserting ApprovalRequest fact.

        Creates conversation: asserts ApprovalRequest, waits for ApprovalGrant.

        Args:
            context: Facet execution context
            dataspace: Dataspace for asserting approval request (required)

        Returns:
            StepResult with blocked=True and request_id in metadata
        """
        if not dataspace:
            return StepResult.fail("HumanStep requires dataspace for approval requests")

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

        # Return pause with request_id in metadata for scheduler tracking
        return StepResult.pause(
            reason=f"Awaiting approval: {request_id}",
            request_id=request_id
        )


@dataclass
class WriteStep:
    """
    Step that writes a typed fact to the dataspace.

    **Usage:**
        WriteStep(
            fact_type=ReviewVerdict,
            values={"verdict": "approve", "feedback": "Looks good!"},
            fact_id_key="verdict_id"  # Optional: get fact_id from context
        )

    Attributes:
        fact_type: Fact type to construct and assert (required)
        values: Dict of field values for the fact (fact_id auto-generated if not provided)
        fact_id_key: Context key containing fact_id (optional)
        store_handle_as: Context key to store returned handle (optional)
    """

    fact_type: type
    values: Optional[Dict[str, Any]] = None
    fact_id_key: Optional[str] = None
    store_handle_as: Optional[str] = None

    def execute(self, context: FacetContext, dataspace) -> StepResult:
        """
        Write fact to dataspace.

        Args:
            context: Facet execution context
            dataspace: Dataspace for asserting facts (required)

        Returns:
            StepResult with fact assertion
        """
        if not dataspace:
            return StepResult.fail("WriteStep requires dataspace")

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
