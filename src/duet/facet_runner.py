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
        channel_state: Dict[str, Any],
        run_id: str,
        iteration: int,
        workspace_root: str,
        adapter=None,  # AssistantAdapter for AgentStep execution
    ) -> FacetExecutionResult:
        """
        Execute a facet script (phase with steps).

        Args:
            phase: Phase with step-based script
            channel_state: Current channel values (global dataspace state)
            run_id: Current run identifier
            iteration: Current iteration number
            workspace_root: Workspace directory path
            adapter: Optional adapter for AgentStep execution

        Returns:
            FacetExecutionResult with context, writes, and execution info
        """
        # Initialize facet context
        reads = phase.get_reads()
        channel_reads = {ch.name: channel_state.get(ch.name) for ch in reads}

        context = FacetContext(
            phase_name=phase.name,
            run_id=run_id,
            iteration=iteration,
            channel_reads=channel_reads,
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
                    result = self._execute_read_step(step, context)
                elif isinstance(step, ToolStep):
                    result = self._execute_tool_step(step, context)
                elif isinstance(step, AgentStep):
                    result = self._execute_agent_step(step, context, adapter)
                elif isinstance(step, HumanStep):
                    result = self._execute_human_step(step, context)
                elif isinstance(step, WriteStep):
                    result = self._execute_write_step(step, context)
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
                    # Not a failure, execution paused for human
                    return FacetExecutionResult(
                        context=context,
                        channel_writes=staged_writes,
                        human_approval_needed=True,
                        approval_reason=result.notes or "Approval required",
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
            channel_writes=staged_writes,
            success=True,
            step_logs=step_logs,
        )

    def _execute_read_step(self, step: ReadStep, context: FacetContext) -> StepResult:
        """Execute ReadStep - channels already loaded in context.channel_reads."""
        return step.execute(context)

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
        Execute AgentStep - invoke agent (stub for now).

        Sprint DSL-4: Agent execution integrated with orchestrator adapter system.
        For now, this step just marks that agent invocation is needed.
        """
        # Stub: actual agent invocation happens in orchestrator
        # This step prepares the agent call
        return StepResult(
            metadata={
                "agent_step": True,
                "agent": step.agent,
                "writes": [ch.name for ch in step.writes],
            },
            notes=f"Agent '{step.agent}' ready to invoke",
            success=True,
        )

    def _execute_human_step(self, step: HumanStep, context: FacetContext) -> StepResult:
        """
        Execute HumanStep - create approval request.

        Sprint DSL-4: Human steps pause execution and require manual intervention.
        """
        return step.execute(context)

    def _execute_write_step(self, step: WriteStep, context: FacetContext) -> StepResult:
        """Execute WriteStep - write value to channel."""
        return step.execute(context)
