"""Core orchestration loop that coordinates Codex and Claude."""

from __future__ import annotations

import datetime as dt
import json
import signal
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .adapters import REGISTRY, AssistantAdapter
from .adapters.base import StreamEvent
from .approval import ApprovalNotifier
from .artifacts import ArtifactStore
from .config import DuetConfig
from .dataspace import Dataspace
from .git_operations import GitWorkspace, GitError
from .logging import DuetLogger
from .models import (
    AssistantRequest,
    AssistantResponse,
    ReviewVerdict,
    RunSnapshot,
    TransitionDecision,
)
from .persistence import DuetDatabase, PersistenceError
from .scheduler import FacetScheduler
from .streaming import EnhancedStreamingDisplay

# ──────────────────────────────────────────────────────────────────────────────
# STATE MACHINE TRANSITION RULES
# ──────────────────────────────────────────────────────────────────────────────
# Explicit definition of valid phase transitions with conditions.
#
# PLAN → IMPLEMENT
#   - Condition: Valid response received with content
#   - Edge: Empty response → BLOCKED
#
# IMPLEMENT → REVIEW
#   - Condition: Valid response received
#   - Edge: Empty response → BLOCKED
#   - Edge: Adapter failure → BLOCKED
#
# REVIEW → DONE
#   - Condition: response.concluded == True
#   - Edge: Empty approval → requires_human = True → BLOCKED if approval enabled
#
# REVIEW → PLAN
#   - Condition: response.concluded == False (changes requested)
#   - Edge: Max iterations reached → BLOCKED
#
# ANY → BLOCKED
#   - Max iterations exceeded
#   - Manual stop (KeyboardInterrupt, SIGTERM)
#   - Empty/invalid response
#   - Human approval required (when workflow.require_human_approval enabled)
#   - Adapter exceptions
# ──────────────────────────────────────────────────────────────────────────────


class Orchestrator:
    """Coordinates the iterative workflow between Codex and Claude."""

    def __init__(
        self,
        config: DuetConfig,
        artifact_store: ArtifactStore,
        console: Optional[Console] = None,
        db: Optional[DuetDatabase] = None,
        workflow_path: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.artifacts = artifact_store
        self.console = console or Console()
        self.codex_adapter = self._build_adapter(config.codex)
        self.claude_adapter = self._build_adapter(config.claude)
        self.max_iterations = config.workflow.max_iterations
        self._stop_requested = False
        self._custom_workflow_path = workflow_path  # Store for workflow loading
        self._setup_signal_handlers()

        # Initialize logger
        jsonl_path = None
        if config.logging.enable_jsonl:
            config.logging.jsonl_dir.mkdir(parents=True, exist_ok=True)
            jsonl_path = config.logging.jsonl_dir / "duet.jsonl"

        self.logger = DuetLogger(
            console=self.console,
            jsonl_path=jsonl_path,
            enable_jsonl=config.logging.enable_jsonl,
        )

        # Initialize git workspace for change detection
        self.git = GitWorkspace(self.config.storage.workspace_root, console=self.console)

        # Initialize approval notifier
        self.approver = ApprovalNotifier(artifact_store, console=self.console)

        # Initialize database (optional - may be None for filesystem-only mode)
        self.db = db
        if self.db:
            self.console.log("[dim]Database persistence enabled[/]")

        # Load workflow (DSL-based execution - required)
        from .workflow_loader import load_workflow, WorkflowLoadError

        # Track workflow file for hot-reload
        self._workflow_path = self._resolve_workflow_path()
        self._workflow_mtime = self._get_workflow_mtime()

        try:
            self.workflow_graph = load_workflow(
                workflow_path=self._custom_workflow_path,
                workspace_root=config.storage.workspace_root
            )
        except WorkflowLoadError as exc:
            raise RuntimeError(
                f"Failed to load workflow definition:\n{exc}\n\n"
                f"Run 'duet init' to create .duet/workflow.py"
            ) from exc

        from .executor import WorkflowExecutor
        self.workflow_executor = WorkflowExecutor(self.workflow_graph, console=self.console)
        self.console.log(f"[dim]Loaded workflow:[/] {len(self.workflow_graph.phases)} phases")

        # Initialize dataspace for fact-based execution
        self.dataspace = Dataspace()
        self.console.log("[dim]Dataspace initialized for reactive execution[/]")

        # Initialize facet scheduler
        self.scheduler = FacetScheduler(self.dataspace, console=self.console)
        self.console.log("[dim]Reactive facet scheduler initialized[/]")

        # Register all workflow phases as facets with the scheduler
        for phase_name, phase_def in self.workflow_graph.phases.items():
            if not phase_def.is_terminal:
                facet_id = f"facet_{phase_name}"
                self.scheduler.register_facet(facet_id, phase_def)
                self.console.log(f"[dim]Registered facet:[/] {facet_id} with {len(phase_def.get_fact_reads())} dependencies")

    def load_persisted_facts(self, run_id: str) -> int:
        """
        Load persisted facts from database into dataspace.

        Called when resuming a run to restore approval grants and other
        persisted facts into the in-memory dataspace.

        Args:
            run_id: Run identifier

        Returns:
            Number of facts loaded
        """
        if not self.db:
            return 0

        from .dataspace import ApprovalGrant, ApprovalRequest, FactRegistry

        # Query all active facts from database
        fact_records = self.db.get_facts(run_id, active_only=True)
        loaded = 0

        for record in fact_records:
            fact_type_name = record["fact_type"]
            payload = record["payload"]

            # Get fact class from registry
            fact_class = FactRegistry.get(fact_type_name)
            if not fact_class:
                self.console.log(f"[yellow]Unknown fact type {fact_type_name}, skipping[/]")
                continue

            try:
                # Reconstruct fact from payload
                fact = fact_class(**payload)
                # Assert into dataspace
                self.dataspace.assert_fact(fact)
                loaded += 1
            except Exception as exc:
                self.console.log(f"[yellow]Failed to load fact {record['fact_id']}: {exc}[/]")

        if loaded > 0:
            self.console.log(f"[dim]Loaded {loaded} persisted facts from database[/]")

        return loaded

    def _resolve_workflow_path(self) -> Optional[Path]:
        """Resolve workflow file path for hot-reload tracking."""
        try:
            from .workflow_loader import _resolve_workflow_path
            return _resolve_workflow_path(self._custom_workflow_path, self.config.storage.workspace_root)
        except Exception:
            return None

    def _get_workflow_mtime(self) -> Optional[float]:
        """Get workflow file modification time."""
        if self._workflow_path and self._workflow_path.exists():
            return self._workflow_path.stat().st_mtime
        return None

    def _check_and_reload_workflow(self) -> None:
        """
        Check if workflow file has been modified and reload if needed.

        Aborts execution with friendly error if reload fails.
        """
        if not self._workflow_path or not self._workflow_path.exists():
            return

        current_mtime = self._workflow_path.stat().st_mtime
        if current_mtime == self._workflow_mtime:
            return  # No change

        # Workflow updated - reload
        self.console.print()
        self.console.print("[cyan]⟳ Workflow updated; reloading...[/]")

        from .workflow_loader import load_workflow, WorkflowLoadError
        from .executor import WorkflowExecutor

        try:
            new_graph = load_workflow(
                workflow_path=self._custom_workflow_path,
                workspace_root=self.config.storage.workspace_root
            )
            self.workflow_graph = new_graph
            self.workflow_executor = WorkflowExecutor(new_graph, console=self.console)
            self._workflow_mtime = current_mtime

            self.console.print(f"[green]✓ Workflow reloaded:[/] {len(self.workflow_graph.phases)} phases")
        except WorkflowLoadError as exc:
            self.console.print()
            self.console.print("[red bold]Workflow Reload Failed:[/]")
            error_lines = str(exc).split("\n")
            for line in error_lines[:5]:
                self.console.print(f"[red]{line}[/]")
            self.console.print()
            self.console.print("[yellow]Suggestions:[/]")
            self.console.print("  • Run 'duet lint' to validate your workflow")
            self.console.print("  • Fix .duet/workflow.py and try again")
            self.console.print("  • See docs/workflow_dsl.md for DSL reference")
            raise RuntimeError("Workflow reload failed - aborting execution") from exc

    def _build_adapter(self, assistant_cfg):
        """
        Build an adapter from configuration.

        Unpacks assistant config as kwargs and adds workspace_root for adapters
        that need it (e.g., Claude Code).

        Note: exclude_none=True ensures None values don't override adapter defaults.
        """
        adapter_name = assistant_cfg.provider
        # Exclude None values so adapter defaults apply (e.g., timeout=300 if not specified)
        adapter_kwargs = assistant_cfg.model_dump(exclude_none=True)

        # Add workspace_root for adapters that need workspace context
        adapter_kwargs["workspace_root"] = str(self.config.storage.workspace_root)

        # Unpack kwargs to pass individual parameters (model, timeout, cli_path, etc.)
        adapter = REGISTRY.resolve(adapter_name, **adapter_kwargs)
        return adapter

    def _setup_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown on manual stop."""

        def _handle_stop(signum, frame):
            self.console.log("[yellow]Manual stop requested (SIGINT/SIGTERM), halting gracefully...[/]")
            self._stop_requested = True

        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGTERM, _handle_stop)

    def _create_event_handler(
        self, run_id: str, iteration: int, phase: str, display: Optional[StreamingDisplay] = None
    ):
        """
        Create an event handler for streaming adapter events.

        The handler persists events to SQLite (if database available) and can
        optionally display them in the console for real-time feedback.

        Args:
            run_id: Current run identifier
            iteration: Current iteration number
            phase: Current phase
            display: Optional StreamingDisplay for console output

        Returns:
            Callable event handler for adapter streaming
        """
        def handle_event(event: StreamEvent) -> None:
            """Handle streaming event from adapter."""
            # Persist to SQLite if database available
            if self.db:
                try:
                    self.db.insert_event(
                        run_id=run_id,
                        event_type=event["event_type"],
                        payload=event["payload"],
                        iteration=iteration,
                        phase=phase,
                        timestamp=event["timestamp"].isoformat(),
                    )
                except Exception as e:
                    # Don't fail the run if event persistence fails
                    self.console.log(f"[yellow]Warning: Failed to persist event: {e}[/]")

            # Update live display if enabled (not in quiet mode)
            if display:
                display.add_event(event)

        return handle_event

    def run(self, run_id: Optional[str] = None) -> RunSnapshot:
        """
        Execute reactive scheduler-driven workflow loop.

        Facets are executed from the ready queue based on fact availability.
        Loop continues until no facets are ready or terminal state reached.
        """
        # Check for workflow updates and reload if needed
        self._check_and_reload_workflow()

        iteration = 0

        snapshot = RunSnapshot(
            run_id=run_id or self._derive_run_id(),
            iteration=iteration,
            phase="initializing",
            metadata={
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "execution_mode": "scheduler_driven",
            },
        )
        self.console.rule(f"Starting Duet run {snapshot.run_id}")
        self.artifacts.checkpoint(snapshot)

        # Clear dataspace for new run
        self.dataspace.clear()
        self.console.log("[dim]Dataspace cleared[/]")

        # ──── Database: Insert Run Record ────
        if self.db:
            try:
                self.db.insert_run(snapshot)
            except Exception as exc:
                self.console.log(f"[yellow]DB write failed: {exc}[/]")

        # ──── Load Seed Facts ────
        if self.db and run_id:
            loaded = self.load_persisted_facts(run_id)
            if loaded == 0:
                self.console.print()
                self.console.print("[yellow]⚠ No seed facts found[/]")
                self.console.print(f"Use: [white]duet seed FACT_TYPE --run-id {snapshot.run_id} --data '{{...}}'[/]")
                snapshot.phase = "awaiting_seed"
                snapshot.notes = "Waiting for seed facts"
                return snapshot
        else:
            self.console.print()
            self.console.print("[cyan]Seed facts required to start execution[/]")
            self.console.print(f"Use: [white]duet seed FACT_TYPE --run-id {snapshot.run_id} --data '{{...}}'[/]")
            snapshot.phase = "awaiting_seed"
            snapshot.notes = "Waiting for seed facts"
            return snapshot

        # ──── Git Baseline ────
        baseline_commit = None
        if self.git.is_git_repo():
            baseline_commit = self.git.get_current_commit()
            if baseline_commit:
                snapshot.metadata["baseline_commit"] = baseline_commit
                self.console.log(f"[dim]Git baseline: {baseline_commit[:8]}[/]")

        # ──── Reactive Scheduler Loop ────
        self.console.print()
        self.console.print("[bold cyan]═══ Reactive Execution ═══[/]")
        self.console.print(f"[dim]Ready facets: {len(self.scheduler.ready_queue)}[/]")
        self.console.print(f"[dim]Waiting facets: {len(self.scheduler.waiting)}[/]")
        self.console.print()

        facets_executed = 0

        while self.scheduler.has_ready_facets():
            # Stop check
            if self._stop_requested:
                snapshot.phase = "stopped"
                snapshot.notes = "Manual stop requested"
                self.console.log("[yellow]Manual stop detected[/]")
                break

            # Max iterations guard
            if facets_executed >= self.max_iterations:
                snapshot.phase = "blocked"
                snapshot.notes = f"Max iterations ({self.max_iterations}) reached"
                self.console.log("[yellow]Max iterations reached[/]")
                break

            # Get next ready facet
            facet_id = self.scheduler.next_ready()
            if not facet_id:
                break

            # Extract phase from facet_id
            phase_name = facet_id.replace("facet_", "")
            phase_def = self.workflow_graph.phases.get(phase_name)

            if not phase_def:
                self.console.log(f"[red]Unknown phase:[/] {phase_name}")
                continue

            # Check if terminal
            if phase_def.is_terminal:
                snapshot.phase = phase_name
                snapshot.notes = f"Terminal phase reached: {phase_name}"
                self.console.log(f"[green]✓ Terminal phase:[/] {phase_name}")
                break

            # Mark executing
            self.scheduler.mark_executing(facet_id)

            # Increment iteration
            iteration += 1
            facets_executed += 1
            snapshot.iteration = iteration
            snapshot.phase = phase_name
            current_phase = phase_name

            self.console.print()
            self.console.rule(f"[cyan]Facet {facets_executed}: {phase_name}[/] (iteration {iteration})")

            # Execute facet within turn
            from .facet_runner import FacetRunner
            runner = FacetRunner(console=self.console)

            with self.dataspace.in_turn():
                facet_result = runner.execute_facet(
                    phase=phase_def,
                    dataspace=self.dataspace,
                    run_id=snapshot.run_id,
                    iteration=iteration,
                    workspace_root=str(self.config.storage.workspace_root),
                    adapter=self._select_adapter(phase_name),
                    db=self.db,
                )

            # Handle failure
            if not facet_result.success:
                snapshot.phase = "blocked"
                snapshot.notes = facet_result.error or "Facet execution failed"
                self.console.log(f"[red]Facet failed:[/] {facet_result.error}")
                break

            # Handle approval pause
            if facet_result.human_approval_needed:
                snapshot.phase = "blocked"
                snapshot.notes = f"Awaiting approval: {facet_result.approval_reason}"

                if facet_result.approval_request_id:
                    self.scheduler.mark_waiting_for_approval(facet_id, facet_result.approval_request_id)
                    self.console.log(f"[yellow]⏸ Paused for approval:[/] {facet_result.approval_request_id}")
                else:
                    self.scheduler.mark_waiting(facet_id)

                # Continue to process other ready facets
                continue

            # Mark completed
            self.scheduler.mark_completed(facet_id)
            self.console.log(f"[green]✓ Facet completed:[/] {facet_id}")

            # Evaluate guards to potentially transition to terminal
            guard_result = self.workflow_executor.guard_evaluator.evaluate_transitions(
                current_phase=phase_name,
                workflow_graph=self.workflow_graph,
                dataspace=self.dataspace,
            )

            if guard_result.next_phase:
                next_phase = guard_result.next_phase
                if self.workflow_graph.is_terminal(next_phase):
                    snapshot.phase = next_phase
                    snapshot.notes = f"Terminal phase: {next_phase}"
                    self.console.log(f"[green]→ Terminal:[/] {next_phase}")
                    break
                else:
                    self.console.log(f"[dim]→ Transition:[/] {next_phase} (may wake new facets)")

        # ──── Completion ────
        if not self.scheduler.has_ready_facets():
            if len(self.scheduler.waiting) > 0:
                snapshot.phase = "waiting"
                snapshot.notes = f"No ready facets. {len(self.scheduler.waiting)} facets waiting on facts/approvals"
                self.console.log(f"[yellow]⏸ Workflow paused:[/] {len(self.scheduler.waiting)} facets waiting")
            else:
                # No ready, no waiting - workflow complete
                snapshot.phase = "done"
                snapshot.notes = "All facets completed"
                self.console.log("[green]✓ Workflow complete[/]")

        snapshot.metadata["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        snapshot.metadata["facets_executed"] = facets_executed
        self.artifacts.checkpoint(snapshot)

        # ──── Database: Update Run ────
        if self.db:
            try:
                self.db.update_run(snapshot)
            except Exception as exc:
                self.console.log(f"[yellow]DB update failed: {exc}[/]")

        self.console.print()
        self.console.print(f"[bold]Run Summary:[/] {snapshot.run_id}")
        self.console.print(f"  Status: {snapshot.phase}")
        self.console.print(f"  Facets executed: {facets_executed}")
        self.console.print(f"  Final iteration: {iteration}")

        self.logger.log_run_complete(
            snapshot.run_id, snapshot.phase, iteration, snapshot.phase
        )
        self.logger.close()

        return snapshot
    def _select_adapter(self, phase: str) -> AssistantAdapter:
        """
        Select adapter for a phase based on workflow definition.

        Uses the phase's agent assignment from the workflow, then builds
        an adapter from the agent's configuration merged with duet.yaml overrides.

        Raises:
            RuntimeError: If phase or agent not found in workflow (fail fast)
        """
        # Get phase definition from workflow
        phase_def = self.workflow_graph.phases.get(phase)
        if not phase_def:
            raise RuntimeError(
                f"Phase '{phase}' not found in workflow definition. "
                f"Available phases: {', '.join(sorted(self.workflow_graph.phases.keys()))}"
            )

        if not phase_def.agent:
            raise RuntimeError(
                f"Phase '{phase}' has no agent assigned. "
                f"Update .duet/workflow.py to specify an agent for this phase."
            )

        # Get agent from workflow
        agent_name = phase_def.agent
        agent_def = self.workflow_graph.agents.get(agent_name)
        if not agent_def:
            raise RuntimeError(
                f"Agent '{agent_name}' (required by phase '{phase}') not found in workflow. "
                f"Available agents: {', '.join(sorted(self.workflow_graph.agents.keys()))}\n"
                f"Update .duet/workflow.py to define this agent."
            )

        # Build adapter from DSL agent config
        adapter_config = agent_def.to_adapter_config()

        # Merge with duet.yaml overrides (yaml takes precedence for security-sensitive fields)
        # This allows credentials/timeouts to be centralized while workflow specifies capabilities
        yaml_override = None
        if agent_def.provider == "codex":
            yaml_override = self.config.codex
        elif agent_def.provider in ("claude-code", "claude"):
            yaml_override = self.config.claude

        if yaml_override:
            # YAML overrides DSL for security-sensitive fields
            if yaml_override.api_key_env is not None:
                adapter_config["api_key_env"] = yaml_override.api_key_env
            if yaml_override.timeout is not None:
                adapter_config["timeout"] = yaml_override.timeout
            if yaml_override.cli_path is not None:
                adapter_config["cli_path"] = yaml_override.cli_path
            # auto_approve can be overridden by YAML for safety
            if hasattr(yaml_override, 'auto_approve') and yaml_override.auto_approve:
                adapter_config["auto_approve"] = yaml_override.auto_approve

        # Add workspace_root
        adapter_config["workspace_root"] = str(self.config.storage.workspace_root)

        # Build adapter
        from .adapters import REGISTRY
        adapter = REGISTRY.resolve(adapter_config["provider"], **adapter_config)
        return adapter
    def _derive_run_id(self) -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("run-%Y%m%d-%H%M%S")

    def _execute_facet_script(
        self,
        phase: str,
        snapshot: RunSnapshot,
        phase_def,
    ) -> Optional[AssistantResponse]:
        """
        Execute phase as facet script (Sprint DSL-4).

        Uses FacetRunner to execute ordered steps, handles results, and
        returns compatible AssistantResponse for orchestrator loop.

        Args:
            phase: Phase name
            snapshot: Current run snapshot
            phase_def: Phase definition with steps

        Returns:
            AssistantResponse if execution completed, None if paused/blocked
        """
        from .facet_runner import FacetRunner

        # Initialize facet runner
        runner = FacetRunner(console=self.console)

        # Execute facet with dataspace (turn-based atomic publication)
        self.console.log(f"[cyan]Executing facet script:[/] {len(phase_def.steps)} steps")

        with self.dataspace.in_turn():
            facet_result = runner.execute_facet(
                phase=phase_def,
                dataspace=self.dataspace,
                run_id=snapshot.run_id,
                iteration=snapshot.iteration,
                workspace_root=str(self.config.storage.workspace_root),
                adapter=self._select_adapter(phase),
            )
        # Subscriptions triggered atomically after turn

        # Handle facet execution failure
        if not facet_result.success:
            snapshot.phase = "blocked"
            snapshot.notes = facet_result.error or "Facet execution failed"
            self.console.log(f"[red]Facet execution failed:[/] {facet_result.error}")
            return None

        # Handle human approval pause
        if facet_result.human_approval_needed:
            snapshot.phase = "blocked"
            snapshot.notes = f"Human approval required: {facet_result.approval_reason}"
            self.console.log(f"[yellow]Human approval needed:[/] {facet_result.approval_reason}")

            # Notify scheduler to wait for approval grant
            if facet_result.approval_request_id:
                facet_id = f"{snapshot.run_id}_{phase}_{snapshot.iteration}"
                self.scheduler.mark_waiting_for_approval(facet_id, facet_result.approval_request_id)
                self.console.log(
                    f"[dim]Scheduler listening for approval grant: {facet_result.approval_request_id}[/]"
                )

            return None

        # Channel writes already asserted as ChannelFacts in dataspace by FacetRunner
        # No need to apply separately

        # Persist step-by-step execution logs
        if self.db and facet_result.step_logs:
            for step_log in facet_result.step_logs:
                try:
                    self.db.insert_event(
                        run_id=snapshot.run_id,
                        event_type=f"step_{step_log['step_type'].lower()}",
                        payload=step_log,
                        iteration=snapshot.iteration,
                        phase=phase,
                        timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
                    )
                except Exception as exc:
                    self.console.log(f"[yellow]Failed to persist step log: {exc}[/]")

        # Build AssistantResponse from facet result
        response = AssistantResponse(
            content=f"Facet '{phase}' executed {len(facet_result.step_logs)} steps",
            metadata={
                "facet_execution": True,
                "steps_executed": len(facet_result.step_logs),
                "step_logs": facet_result.step_logs,
            },
        )

        return response

    # ──────────────────────────────────────────────────────────────────────────
    # Single-Phase Execution
    # ──────────────────────────────────────────────────────────────────────────
    def _parse_phase_status(self, phase_status: str) -> tuple[Optional[str], str]:
        """
        Parse phase status to determine phase and action.

        Supports arbitrary phase names from workflow definition.

        Returns:
            Tuple of (phase name, action) where action is 'execute', 'done', or 'blocked'
        """
        # Check if this is a terminal state indicator
        if self.workflow_graph.is_terminal(phase_status):
            return None, "done"

        # Check for blocked state
        if phase_status == "blocked":
            return None, "blocked"

        # Parse format: <phase>-ready or <phase>-complete
        # Strategy: split on last hyphen to handle phase names with hyphens
        if "-" not in phase_status:
            return None, "blocked"

        parts = phase_status.rsplit("-", 1)
        if len(parts) != 2:
            return None, "blocked"

        phase_name, status = parts

        # Verify phase exists in workflow
        if phase_name not in self.workflow_graph.phases:
            self.console.log(f"[yellow]Warning: phase_status '{phase_status}' references unknown phase '{phase_name}'[/]")
            return None, "blocked"

        if status == "ready":
            return phase_name, "execute"
        elif status == "complete":
            # Phase completed - this shouldn't normally happen as we create <next_phase>-ready directly
            # But handle gracefully by treating as ready
            return phase_name, "execute"

        return None, "blocked"

    def _derive_phase_status(self, executed_phase: str, next_phase: str) -> str:
        """
        Derive phase status string from executed phase and next phase.

        Generates status strings dynamically for arbitrary phase names.

        Args:
            executed_phase: Phase that was just executed
            next_phase: Next phase determined by decision logic

        Returns:
            Phase status string (e.g., 'triage-ready', 'qa-ready', 'done', 'blocked')
        """
        # Terminal phases return their name directly
        if self.workflow_graph.is_terminal(next_phase):
            return next_phase

        # Blocked state
        if next_phase == "blocked":
            return "blocked"

        # Verify next phase exists in workflow
        if next_phase not in self.workflow_graph.phases:
            self.console.log(f"[yellow]Warning: next_phase '{next_phase}' not found in workflow[/]")
            return "blocked"

        # Generate ready status for next phase
        return f"{next_phase}-ready"

    def _render_summary(self, snapshot: RunSnapshot) -> None:
        table = Table(title="Duet Run Summary")
        table.add_column("Field", style="bold")
        table.add_column("Value")
        table.add_row("Run ID", snapshot.run_id)
        table.add_row("Phase", snapshot.phase)
        table.add_row("Iteration", str(snapshot.iteration))
        table.add_row("Notes", snapshot.notes or "")
        table.add_row("Started", snapshot.metadata.get("started_at", ""))
        table.add_row("Completed", snapshot.metadata.get("completed_at", ""))
        self.console.print(table)
