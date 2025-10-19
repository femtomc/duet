"""
Facet execution runtime (Sprint DSL-4).

Executes phase step scripts sequentially, managing local context and
channel updates with explicit dataflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from rich.console import Console

from .dsl.steps import (
    AgentStep,
    FacetContext,
    HumanStep,
    ReadStep,
    StepResult,
    ToolStep,
    WriteStep,
)
from .dsl.workflow import Phase
from .models import AssistantRequest, AssistantResponse


@dataclass
class FacetExecutionResult:
    """
    Result of executing a complete facet script.

    Contains:
    - Final context state
    - Channel writes to apply
    - Agent invocation info (if AgentStep present)
    - Human approval needed flag
    - Success/failure status
    """

    context: FacetContext
    channel_writes: Dict[str, Any]
    agent_request: Optional[AssistantRequest] = None
    agent_response: Optional[AssistantResponse] = None
    human_approval_needed: bool = False
    approval_reason: Optional[str] = None
    approval_request_id: Optional[str] = None  # For scheduler tracking
    success: bool = True
    error: Optional[str] = None
    step_logs: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.step_logs is None:
            self.step_logs = []


class FacetRunner:
    """
    Executes facet scripts (ordered phase steps).

    Replaces old sequential phase execution with explicit step-by-step
    execution using local context and deterministic dataflow.
    """

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()

    def execute_facet(
        self,
        phase: Phase,
        dataspace,  # Dataspace (required - no more channel_state dict)
        run_id: str,
        iteration: int,
        workspace_root: str,
        adapter=None,  # AssistantAdapter for AgentStep execution
        db=None,  # DuetDatabase for persisting facts
    ) -> FacetExecutionResult:
        """
        Execute a facet script (phase with steps).

        Reads from dataspace, executes steps, writes back as facts.

        Args:
            phase: Phase with step-based script
            dataspace: Dataspace for fact queries and assertions
            run_id: Current run identifier
            iteration: Current iteration number
            workspace_root: Workspace directory path
            adapter: Optional adapter for AgentStep execution

        Returns:
            FacetExecutionResult with context, writes, and execution info
        """
        # Initialize facet context (facts loaded on-demand by ReadStep)
        context = FacetContext(
            phase_name=phase.name,
            run_id=run_id,
            iteration=iteration,
            workspace_root=workspace_root,
        )

        # Accumulated channel writes (staged until end)
        staged_writes = {}
        step_logs = []

        # Execute steps in order
        for i, step in enumerate(phase.steps):
            step_name = f"{step.__class__.__name__}[{i}]"
            self.console.log(f"[dim]Executing step {i+1}/{len(phase.steps)}: {step_name}[/]")

            try:
                if isinstance(step, ReadStep):
                    result = self._execute_read_step(step, context, dataspace)
                elif isinstance(step, ToolStep):
                    result = self._execute_tool_step(step, context)
                elif isinstance(step, AgentStep):
                    result = self._execute_agent_step(step, context, adapter)
                elif isinstance(step, HumanStep):
                    result = self._execute_human_step(step, context, dataspace)
                elif isinstance(step, WriteStep):
                    result = self._execute_write_step(step, context, dataspace)
                else:
                    result = StepResult.fail(f"Unknown step type: {type(step)}")

                # Log step execution
                step_logs.append({
                    "step_index": i,
                    "step_type": step.__class__.__name__,
                    "success": result.success,
                    "blocked": result.blocked,
                    "error": result.error,
                    "notes": result.notes,
                })

                # Check for blocked state (human approval pause)
                if result.blocked:
                    # Extract approval request ID from metadata
                    request_id = result.metadata.get("approval_request_id")

                    # Persist ApprovalRequest to database if available
                    if db and request_id:
                        from .dataspace import ApprovalRequest, FactPattern

                        # Query for ApprovalRequest facts created by this execution
                        pattern = FactPattern(fact_type=ApprovalRequest)
                        requests = dataspace.query(pattern)

                        # Persist all approval requests for this run
                        for req in requests:
                            if req.context.get("run_id") == run_id:
                                db.save_fact(run_id, req)

                    # Not a failure, execution paused for human
                    return FacetExecutionResult(
                        context=context,
                        channel_writes=staged_writes,
                        human_approval_needed=True,
                        approval_reason=result.notes or "Approval required",
                        approval_request_id=request_id,
                        success=True,
                        step_logs=step_logs,
                    )

                # Check for failure
                if not result.success:
                    # Step failed - return early
                    return FacetExecutionResult(
                        context=context,
                        channel_writes=staged_writes,
                        success=False,
                        error=result.error or f"Step {step_name} failed",
                        step_logs=step_logs,
                    )

                # Merge step results
                for key, value in result.context_updates.items():
                    context.set(key, value)

                # Stage channel writes
                staged_writes.update(result.channel_writes)

            except Exception as exc:
                self.console.log(f"[red]Step {step_name} raised exception: {exc}[/]")
                return FacetExecutionResult(
                    context=context,
                    channel_writes=staged_writes,
                    success=False,
                    error=f"Step {step_name} exception: {exc}",
                    step_logs=step_logs,
                )

        # All steps completed successfully
        return FacetExecutionResult(
            context=context,
            channel_writes={},  # Empty - facts asserted directly by WriteStep
            success=True,
            step_logs=step_logs,
        )

    def _execute_read_step(
        self, step: ReadStep, context: FacetContext, dataspace
    ) -> StepResult:
        """
        Execute ReadStep - query facts from dataspace or use preloaded channel reads.

        Supports both typed fact queries (new API) and legacy channel reads.
        """
        return step.execute(context, dataspace)

    def _execute_tool_step(self, step: ToolStep, context: FacetContext) -> StepResult:
        """Execute ToolStep - run tool and merge results."""
        return step.execute(context)

    def _execute_agent_step(
        self,
        step: AgentStep,
        context: FacetContext,
        adapter=None,
    ) -> StepResult:
        """
        Execute AgentStep - invoke agent and write response to channels.

        Builds prompt from FacetContext, calls adapter, writes response
        to declared channels.

        Args:
            step: AgentStep configuration
            context: Facet execution context
            adapter: Adapter to invoke

        Returns:
            StepResult with agent response in channel_writes
        """
        if not adapter:
            return StepResult.fail("No adapter provided for AgentStep")

        # Build prompt from context
        prompt_parts = [f"Phase: {context.phase_name}"]

        # Include channel reads
        if context.channel_reads:
            prompt_parts.append("\n──── Channel Inputs ────")
            for key, value in context.channel_reads.items():
                value_str = str(value)[:500]  # Truncate long values
                prompt_parts.append(f"{key}: {value_str}")

        # Include local context state
        if context.local_state:
            prompt_parts.append("\n──── Context State ────")
            for key, value in context.local_state.items():
                value_str = str(value)[:500]
                prompt_parts.append(f"{key}: {value_str}")

        # Use custom prompt or default
        if step.prompt_template:
            prompt_parts.append(f"\n{step.prompt_template}")

        prompt = "\n".join(prompt_parts)

        # Build request
        from .models import AssistantRequest

        request = AssistantRequest(
            role=step.role or step.agent,
            prompt=prompt,
            context={
                "phase": context.phase_name,
                "run_id": context.run_id,
                "iteration": context.iteration,
                "facet_execution": True,
            },
        )

        # Invoke adapter
        try:
            response = adapter.stream(request, on_event=lambda e: None)
        except Exception as exc:
            return StepResult.fail(f"Agent '{step.agent}' failed: {exc}")

        if not response.content or not response.content.strip():
            return StepResult.fail(f"Agent '{step.agent}' returned empty response")

        # Write response to declared channels
        channel_writes = {}
        if step.writes:
            # Try to match channels to response metadata first (for structured outputs)
            for channel in step.writes:
                if channel.name in response.metadata:
                    # Use metadata value if available (e.g., verdict from echo adapter)
                    channel_writes[channel.name] = response.metadata[channel.name]
                elif channel == step.writes[0]:
                    # Primary output: use response content
                    channel_writes[channel.name] = response.content

        return StepResult(
            context_updates={"agent_response": response.content},
            channel_writes=channel_writes,
            metadata=response.metadata,
            success=True,
            notes=f"Agent '{step.agent}' completed",
        )

    def _execute_human_step(self, step: HumanStep, context: FacetContext, dataspace) -> StepResult:
        """
        Execute HumanStep - assert ApprovalNeeded fact and pause.

        Creates Syndicate-style conversation by asserting ApprovalRequest fact.
        Facet pauses until ApprovalGranted appears.

        Args:
            step: HumanStep configuration
            context: Facet execution context
            dataspace: Dataspace for approval conversation

        Returns:
            StepResult with blocked=True and approval request ID
        """
        return step.execute(context, dataspace)

    def _execute_write_step(
        self, step: WriteStep, context: FacetContext, dataspace
    ) -> StepResult:
        """
        Execute WriteStep - assert typed fact or write to channel.

        Supports both typed fact assertions (new API) and legacy channel writes.
        """
        return step.execute(context, dataspace)
