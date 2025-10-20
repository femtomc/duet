"""
Facet execution runtime (Sprint DSL-4).

Executes phase step scripts sequentially, managing local context and
channel updates with explicit dataflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from rich.console import Console

from .dsl.steps import (
    AgentStep,
    FacetContext,
    HumanStep,
    ReadStep,
    ReceiveMessageStep,
    StepResult,
    SendMessageStep,
    ToolStep,
    WriteStep,
)
from .dsl.workflow import Phase
from .models import AssistantRequest, AssistantResponse

if TYPE_CHECKING:
    from .dataspace import Dataspace, MessageEvent

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
    child_dataspace: Optional["Dataspace"] = None

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
        message_events: Optional[List["MessageEvent"]] = None,
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
        if dataspace is None:
            return FacetExecutionResult(
                context=FacetContext(
                    facet_id=phase.name,
                    phase_name=phase.name,
                    run_id=run_id,
                    iteration=iteration,
                    workspace_root=workspace_root,
                ),
                channel_writes={},
                success=False,
                error="Dataspace is required for facet execution",
            )

        child_space = dataspace.ensure_child(phase.name)
        result: Optional[FacetExecutionResult] = None
        cleanup_mode = "remove_retract"

        try:
            with child_space.in_turn():
                context = FacetContext(
                    facet_id=phase.name,
                    phase_name=phase.name,
                    run_id=run_id,
                    iteration=iteration,
                    workspace_root=workspace_root,
                )
                if message_events:
                    context.metadata["message_events"] = list(message_events)
                else:
                    context.metadata.setdefault("message_events", [])
                step_logs: List[Dict[str, Any]] = []

                for i, step in enumerate(phase.steps):
                    step_name = f"{step.__class__.__name__}[{i}]"
                    self.console.log(f"[dim]Executing step {i+1}/{len(phase.steps)}: {step_name}[/]")

                    try:
                        if isinstance(step, ReadStep):
                            step_result = self._execute_read_step(step, context, dataspace)
                        elif isinstance(step, ReceiveMessageStep):
                            step_result = self._execute_receive_message_step(step, context, child_space)
                        elif isinstance(step, SendMessageStep):
                            step_result = self._execute_send_message_step(step, context, child_space)
                        elif isinstance(step, ToolStep):
                            step_result = self._execute_tool_step(step, context)
                        elif isinstance(step, AgentStep):
                            step_result = self._execute_agent_step(step, context, adapter)
                        elif isinstance(step, HumanStep):
                            step_result = self._execute_human_step(step, context, child_space)
                        elif isinstance(step, WriteStep):
                            step_result = self._execute_write_step(step, context, child_space)
                        else:
                            step_result = StepResult.fail(f"Unknown step type: {type(step)}")

                        step_logs.append({
                            "step_index": i,
                            "step_type": step.__class__.__name__,
                            "success": step_result.success,
                            "blocked": step_result.blocked,
                            "error": step_result.error,
                            "notes": step_result.notes,
                        })

                        if step_result.blocked:
                            request_id = step_result.metadata.get("approval_request_id")

                            if db and request_id:
                                from .dataspace import ApprovalRequest, FactPattern

                                pattern = FactPattern(fact_type=ApprovalRequest)
                                requests = dataspace.query(pattern)
                                for req in requests:
                                    if req.context.get("run_id") == run_id:
                                        db.save_fact(run_id, req)

                            result = FacetExecutionResult(
                                context=context,
                                channel_writes={},
                                human_approval_needed=True,
                                approval_reason=step_result.notes or "Approval required",
                                approval_request_id=request_id,
                                success=True,
                                step_logs=step_logs,
                                child_dataspace=child_space,
                            )
                            cleanup_mode = "keep"
                            break

                        if not step_result.success:
                            result = FacetExecutionResult(
                                context=context,
                                channel_writes={},
                                success=False,
                                error=step_result.error or f"Step {step_name} failed",
                                step_logs=step_logs,
                            )
                            cleanup_mode = "remove_retract"
                            break

                        for key, value in step_result.context_updates.items():
                            context.set(key, value)

                    except Exception as exc:
                        self.console.log(f"[red]Step {step_name} raised exception: {exc}[/]")
                        result = FacetExecutionResult(
                            context=context,
                            channel_writes={},
                            success=False,
                            error=f"Step {step_name} exception: {exc}",
                            step_logs=step_logs,
                        )
                        cleanup_mode = "remove_retract"
                        break
                else:
                    result = FacetExecutionResult(
                        context=context,
                        channel_writes={},
                        success=True,
                        step_logs=step_logs,
                    )
                    cleanup_mode = "remove_no_retract"

        finally:
            if cleanup_mode == "remove_retract":
                dataspace.remove_child(phase.name, retract=True)
            elif cleanup_mode == "remove_no_retract":
                dataspace.remove_child(phase.name, retract=False)

        if result is None:
            raise RuntimeError("Facet execution did not produce a result")

        return result

    def _execute_read_step(
        self, step: ReadStep, context: FacetContext, dataspace
    ) -> StepResult:
        """
        Execute ReadStep - query facts from dataspace or use preloaded channel reads.

        Supports both typed fact queries (new API) and legacy channel reads.
        """
        return step.execute(context, dataspace)

    def _execute_receive_message_step(
        self, step: ReceiveMessageStep, context: FacetContext, dataspace
    ) -> StepResult:
        """Execute ReceiveMessageStep - load delivered message into context."""
        return step.execute(context, dataspace)

    def _execute_send_message_step(
        self, step: SendMessageStep, context: FacetContext, dataspace
    ) -> StepResult:
        """Execute SendMessageStep - emit message through dataspace."""
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

        # Include fact reads
        if context.fact_reads:
            prompt_parts.append("\n──── Fact Inputs ────")
            for key, value in context.fact_reads.items():
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

        # Store response in context for use by subsequent WriteSteps
        return StepResult(
            context_updates={"agent_response": response.content},
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
