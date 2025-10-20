"""
Facet-based orchestrator for reactive workflow execution.

Orchestrates facet execution using compiled FacetProgram, reactive scheduler,
and dataspace for fact-based coordination.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console

from .artifacts import ArtifactStore
from .config import DuetConfig
from .dataspace import Dataspace
from .dsl.combinators import FacetProgram
from .dsl.compiler import compile_program
from .facet_runner import FacetRunner
from .persistence import DuetDatabase
from .scheduler import FacetScheduler


@dataclass
class OrchestrationResult:
    """
    Result of orchestration execution.

    Attributes:
        success: Whether orchestration completed successfully
        facets_executed: Number of facets that ran
        iterations: Total iterations across all facets
        error: Error message if failed
        completed_facets: List of facet IDs that completed
        waiting_facets: List of facet IDs still waiting
    """

    success: bool
    facets_executed: int = 0
    iterations: int = 0
    error: Optional[str] = None
    completed_facets: list[str] = field(default_factory=list)
    waiting_facets: list[str] = field(default_factory=list)
    canceled_facets: list[str] = field(default_factory=list)


@dataclass
class Orchestrator:
    """
    Facet-based orchestrator for reactive workflows.

    Coordinates facet execution using:
    - FacetProgram: Compiled from DSL (seq, loop, etc.)
    - Dataspace: Fact storage and subscriptions
    - Scheduler: Reactive facet scheduling
    - FacetRunner: Step-by-step facet execution

    Attributes:
        config: Duet configuration
        artifact_store: Storage for run artifacts
        console: Rich console for output
        db: Database for persistence
        workspace_root: Workspace directory path
    """

    config: DuetConfig
    artifact_store: ArtifactStore
    console: Console = field(default_factory=Console)
    db: Optional[DuetDatabase] = None
    workspace_root: str = "."

    def run(
        self,
        program: FacetProgram,
        run_id: str,
        adapter=None,
        max_iterations: int = 100,
        initial_facts: Optional[list] = None,
        cancel_facet_id: Optional[str] = None,
    ) -> OrchestrationResult:
        """
        Execute a facet program reactively.

        Compiles the program, registers facets with the scheduler, and executes
        the reactive loop until completion or max iterations.

        Args:
            program: FacetProgram from combinators
            run_id: Unique run identifier
            adapter: Assistant adapter for AgentSteps
            max_iterations: Maximum total iterations (safety limit)
            initial_facts: Optional list of facts to seed dataspace before execution
            cancel_facet_id: Optional facet identifier to cancel if it becomes waiting

        Returns:
            OrchestrationResult with execution summary

        Example:
            program = seq(
                facet("plan").needs(TaskRequest).emit(PlanDoc).build(),
                facet("implement").needs(PlanDoc).emit(CodeArtifact).build()
            )

            task = TaskRequest(fact_id="task_1", description="Build feature", priority=1)

            orchestrator = Orchestrator(config, artifact_store)
            result = orchestrator.run(
                program,
                run_id="run_1",
                adapter=echo_adapter,
                initial_facts=[task]
            )
        """
        self.console.log(f"[bold]Starting orchestration: {run_id}[/]")

        # Initialize components
        dataspace = Dataspace()
        scheduler = FacetScheduler(dataspace, console=self.console)
        runner = FacetRunner(console=self.console)

        canceled_facets: list[str] = []

        # Seed initial facts
        if initial_facts:
            self.console.log(f"Seeding {len(initial_facts)} initial fact(s)")
            with dataspace.in_turn():
                for fact in initial_facts:
                    dataspace.assert_fact(fact, facet_id="__seed__")
                    self.console.log(f"  - {fact.__class__.__name__}: {fact.fact_id}")

        # Compile program to registrations
        try:
            registrations = compile_program(program)
            self.console.log(f"Compiled {len(registrations)} facet(s)")
        except ValueError as e:
            self.console.log(f"[red]Compilation failed: {e}[/]")
            return OrchestrationResult(success=False, error=str(e))

        # Register facets with scheduler
        for reg in registrations:
            scheduler.register(reg)
            self.console.log(f"Registered facet: {reg.facet_id} (policy: {reg.policy.value})")

        # Execute reactive loop
        iteration_count = 0
        facets_executed = 0
        executed_facet_ids = set()

        while scheduler.has_ready_facets() and iteration_count < max_iterations:
            facet_id = scheduler.next_ready()
            if not facet_id:
                break

            self.console.log(f"\n[cyan]→ Executing facet: {facet_id}[/]")
            scheduler.mark_executing(facet_id)

            # Get phase from scheduler
            phase = scheduler.get_phase(facet_id)
            if not phase:
                error_msg = f"Phase not found for facet: {facet_id}"
                self.console.log(f"[red]{error_msg}[/]")
                return OrchestrationResult(
                    success=False,
                    error=error_msg,
                    facets_executed=facets_executed,
                    iterations=iteration_count
                )

            # Execute facet
            message_events = scheduler.pop_pending_messages(facet_id)
            result = runner.execute_facet(
                phase=phase,
                dataspace=dataspace,
                run_id=run_id,
                iteration=iteration_count,
                workspace_root=self.workspace_root,
                adapter=adapter,
                db=self.db,
                message_events=message_events,
            )

            iteration_count += 1

            if not result.success:
                self.console.log(f"[red]✗ Facet '{facet_id}' failed: {result.error}[/]")
                scheduler.mark_completed(facet_id)
                return OrchestrationResult(
                    success=False,
                    error=result.error,
                    facets_executed=facets_executed,
                    iterations=iteration_count
                )

            if result.human_approval_needed:
                self.console.log(f"[yellow]⏸ Facet '{facet_id}' waiting for approval[/]")
                if result.approval_request_id:
                    scheduler.mark_waiting_for_approval(facet_id, result.approval_request_id)
                else:
                    scheduler.mark_waiting(facet_id)
                if result.child_dataspace:
                    scheduler.set_waiting_child_dataspace(facet_id, result.child_dataspace)

                if cancel_facet_id and facet_id == cancel_facet_id:
                    if scheduler.cancel_facet(facet_id):
                        canceled_facets.append(facet_id)
                        self.console.log(f"[cyan]Facet '{facet_id}' canceled by request[/]")
                        continue

                continue

            # Mark completed
            scheduler.mark_completed(facet_id)
            executed_facet_ids.add(facet_id)
            facets_executed += 1
            self.console.log(f"[green]✓ Facet '{facet_id}' completed[/]")

        # Determine final status
        completed_facets = list(executed_facet_ids)
        waiting_facets = list(scheduler.waiting)

        if cancel_facet_id and cancel_facet_id in scheduler.waiting:
            if scheduler.cancel_facet(cancel_facet_id):
                if cancel_facet_id not in canceled_facets:
                    canceled_facets.append(cancel_facet_id)
                self.console.log(f"[cyan]Facet '{cancel_facet_id}' canceled by request[/]")
            waiting_facets = [fid for fid in scheduler.waiting if fid != cancel_facet_id]

        if iteration_count >= max_iterations:
            error_msg = f"Max iterations ({max_iterations}) reached"
            self.console.log(f"[yellow]{error_msg}[/]")
            return OrchestrationResult(
                success=False,
                error=error_msg,
                facets_executed=facets_executed,
                iterations=iteration_count,
                completed_facets=completed_facets,
                waiting_facets=waiting_facets
            )

        # Success only if:
        # 1. No facets waiting (all completed), OR
        # 2. All waiting facets are waiting for approval (well-defined pause state)
        if len(waiting_facets) == 0:
            # All facets completed
            success = True
        elif len(scheduler.approval_requests) == len(waiting_facets):
            # All waiting facets are approval-waiting (valid pause state)
            success = True
        else:
            # Some facets blocked waiting for facts that may never arrive
            success = False

        self.console.log(f"\n[bold green]Orchestration complete[/]")
        self.console.log(f"  Facets executed: {facets_executed}")
        self.console.log(f"  Total iterations: {iteration_count}")
        self.console.log(f"  Waiting: {len(waiting_facets)}")

        return OrchestrationResult(
            success=success,
            facets_executed=facets_executed,
            iterations=iteration_count,
            completed_facets=completed_facets,
            waiting_facets=waiting_facets,
            canceled_facets=canceled_facets,
        )

    def run_next_phase(self, *args, **kwargs) -> OrchestrationResult:  # type: ignore[no-untyped-def]
        """
        Legacy method for compatibility.

        Use run() with FacetProgram instead.
        """
        raise NotImplementedError(
            "run_next_phase() is deprecated. Use run(program: FacetProgram) instead."
        )

    def cancel_waiting_facet(self, scheduler: FacetScheduler, facet_id: str) -> bool:
        """Cancel a waiting facet safely, retracting its outstanding assertions."""
        return scheduler.cancel_facet(facet_id)
