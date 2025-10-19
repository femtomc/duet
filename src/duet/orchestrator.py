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
        """Execute orchestration loop until termination."""
        # Check for workflow updates and reload if needed
        self._check_and_reload_workflow()

        iteration = 0

        # Use workflow's initial phase instead of hardcoded PLAN
        initial_phase_name = self.workflow_graph.initial_phase
        initial_phase = initial_phase_name

        snapshot = RunSnapshot(
            run_id=run_id or self._derive_run_id(),
            iteration=iteration,
            phase=initial_phase,
            metadata={
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "consecutive_replans": 0,
            },
        )
        self.console.rule(f"Starting Duet run {snapshot.run_id}")
        self.artifacts.checkpoint(snapshot)

        # ──── Seed Dataspace with Task Fact ────
        # Clear dataspace for new run
        self.dataspace.clear()

        # Task seeding is now workflow-specific
        # Workflows should define their own initial fact in steps or via custom initialization
        self.console.log("[dim]Dataspace cleared for new run[/]")
        self.console.log("[dim]Initial facts should be asserted by workflow or via CLI[/]")

        # ──── Database: Insert Run Record ────
        if self.db:
            try:
                self.db.insert_run(snapshot)
            except Exception as exc:
                self.console.log(f"[yellow]DB write failed: {exc}[/]")

        # ──── Git Baseline Commit (for change detection) ────
        baseline_commit = None
        if self.git.is_git_repo():
            baseline_commit = self.git.get_current_commit()
            if baseline_commit:
                snapshot.metadata["baseline_commit"] = baseline_commit
                self.console.log(f"[dim]Baseline commit: {baseline_commit[:8]}[/]")
            else:
                # No commits yet (fresh repository) - warn about missing time travel
                self.console.print()
                self.console.print("[yellow bold]⚠ Warning: No git commits found[/]")
                self.console.print(
                    "[yellow]Workspace changes won't be reverted when using 'duet back'.[/]"
                )
                self.console.print("[dim]Run: [cyan]duet init --init-git --force[/dim] [dim]or commit manually[/]")
                self.console.print()

        # ──── Git Feature Branch Creation ────
        original_branch = None
        if self.config.workflow.use_feature_branches and self.git.is_git_repo():
            try:
                original_branch = self.git.get_current_branch()
                snapshot.metadata["original_branch"] = original_branch

                # Create and checkout feature branch
                feature_branch = f"duet/{snapshot.run_id}"
                if self.git.branch_exists(feature_branch):
                    self.console.log(
                        f"[yellow]Feature branch already exists:[/] {feature_branch}"
                    )
                    self.git.checkout_branch(feature_branch)
                else:
                    self.git.checkout_branch(feature_branch, create=True)

                snapshot.metadata["feature_branch"] = feature_branch
                self.console.log(f"[green]Working on feature branch:[/] {feature_branch}")
            except Exception as exc:
                self.console.log(f"[yellow]Git branch setup failed: {exc}[/]")
                self.console.log("[yellow]Continuing on current branch[/]")

        # Start with initial phase from workflow
        current_phase = self.workflow_graph.initial_phase
        consecutive_replans = 0
        while True:
            # ──── Edge Case: Manual Stop ────
            if self._stop_requested:
                snapshot.phase = "blocked"
                snapshot.notes = "Manual stop requested by user (SIGINT/SIGTERM)."
                self.console.log("[yellow]Manual stop detected, marking run as BLOCKED[/]")
                break

            # ──── Edge Case: Max Iterations ────
            if iteration >= self.max_iterations and not self.workflow_graph.is_terminal(current_phase):
                snapshot.phase = "blocked"
                snapshot.notes = f"Max iterations ({self.max_iterations}) reached without completion."
                self.console.log("[yellow]Max iterations reached, marking run as BLOCKED[/]")
                break

            # ──── Terminal Phase Check ────
            if self.workflow_graph.is_terminal(current_phase):
                snapshot.phase = current_phase
                snapshot.notes = f"Run reached terminal phase: {current_phase}"
                self.console.log(f"[green]Run completed at terminal phase: {current_phase}[/]")
                break

            # ──── Begin Iteration ────
            iteration += 1
            snapshot.iteration = iteration
            snapshot.phase = current_phase
            phase_start_time = dt.datetime.now(dt.timezone.utc)
            self.logger.log_iteration_start(snapshot.run_id, iteration, current_phase)

            # ──── Execute Facet Script ────
            phase_def = self.workflow_graph.phases.get(current_phase)
            if not phase_def:
                snapshot.phase = "blocked"
                snapshot.notes = f"Phase '{current_phase}' not found in workflow"
                break

            # All phases must use step-based execution now
            response = self._execute_facet_script(current_phase, snapshot, phase_def)
            if response is None:
                # Human approval or error - already handled
                break

            # ──── Extract Verdict from Metadata (if phase writes verdict channel) ────
            publishes_verdict = phase_def and any(ch.name == "verdict" for ch in phase_def.get_writes())
            if publishes_verdict and "verdict" in response.metadata:
                verdict_str = response.metadata["verdict"]
                if isinstance(verdict_str, str):
                    # Normalize verdict string (handle case variations)
                    verdict_lower = verdict_str.lower().strip()
                    if verdict_lower in ("approve", "approved"):
                        response.verdict = ReviewVerdict.APPROVE
                    elif verdict_lower in ("changes_requested", "changes requested", "revise"):
                        response.verdict = ReviewVerdict.CHANGES_REQUESTED
                    elif verdict_lower in ("blocked", "block"):
                        response.verdict = ReviewVerdict.BLOCKED
                    else:
                        self.console.log(
                            f"[yellow]Unknown verdict in metadata: {verdict_str}[/]"
                        )

            # ──── Update Channels ────
            self._update_channels_from_response(current_phase, response)

            # ──── Permission Check ────
            permission_denials = response.metadata.get("permission_denials") if response.metadata else None
            permission_required = response.metadata.get("permission_required") if response.metadata else False
            if permission_required and permission_denials:
                self.console.log(
                    "[yellow]Claude Code requested permission; review required before continuing.[/]"
                )
                permission_message = "Claude Code requested permission to proceed with implementation."
            else:
                permission_message = None

            # ──── Persist Channel Messages ────
            if self.db and self.workflow_executor:
                self._persist_channel_messages(
                    run_id=snapshot.run_id,
                    phase=current_phase,
                    iteration=iteration,
                )

            # ──── Edge Case: Empty Response ────
            if not response.content or not response.content.strip():
                snapshot.phase = "blocked"
                snapshot.notes = f"Empty response received during {current_phase}."
                self.console.log("[yellow]Empty response detected, marking run as BLOCKED[/]")
                self._persist_interaction(snapshot.run_id, current_phase, request, response)
                break

            # ──── Guardrail: Phase Runtime Limit ────
            if self.config.workflow.max_phase_runtime_seconds:
                phase_duration = (dt.datetime.now(dt.timezone.utc) - phase_start_time).total_seconds()
                if phase_duration > self.config.workflow.max_phase_runtime_seconds:
                    snapshot.phase = "blocked"
                    snapshot.notes = (
                        f"Phase {current_phase} exceeded maximum runtime "
                        f"({self.config.workflow.max_phase_runtime_seconds}s)"
                    )
                    self.console.log(
                        f"[red]Phase runtime limit exceeded: {phase_duration:.1f}s[/]"
                    )
                    self._persist_interaction(snapshot.run_id, current_phase, request, response)
                    break

            # ──── Git Change Detection (for metadata only, no enforcement) ────
            # Sprint DSL-2+: Git validation moved to tools, but still detect changes for logging
            if self.git.is_git_repo():
                try:
                    # Detect changes for logging and metadata
                    changes = self.git.detect_changes(baseline_commit=baseline_commit)
                    current_commit = self.git.get_current_commit()

                    # Check if new commits were created
                    new_commits_created = (
                        baseline_commit
                        and current_commit
                        and baseline_commit != current_commit
                    )

                    # Store change summary in response metadata
                    response.metadata["git_changes"] = {
                        "has_changes": changes.has_changes,
                        "files_changed": changes.files_changed,
                        "insertions": changes.insertions,
                        "deletions": changes.deletions,
                        "staged_files": changes.staged_files,
                        "commit_sha": changes.commit_sha,
                        "diff_stat": changes.diff_stat,
                        "baseline_commit": baseline_commit,
                        "new_commits_created": new_commits_created,
                    }

                    # Update baseline if new commits were created
                    if new_commits_created:
                        baseline_commit = current_commit
                        snapshot.metadata["latest_commit"] = current_commit

                    # Log detected changes (informational only - no blocking)
                    if new_commits_created:
                        self.console.log(
                            f"[green]New commit created:[/] {current_commit[:8]} "
                            f"({changes.files_changed} files, +{changes.insertions}/-{changes.deletions})"
                        )
                    elif changes.has_changes:
                        self.console.log(
                            f"[green]Detected changes:[/] {changes.files_changed} files, "
                            f"+{changes.insertions}/-{changes.deletions}"
                        )
                except Exception as exc:
                    # Git detection failure shouldn't block the run, just warn
                    error_msg = str(exc).replace("[", "\\[").replace("]", "\\]")
                    self.console.log(f"[yellow]Git change detection failed: {error_msg}[/]")
                    response.metadata["git_changes"] = {"error": str(exc)}

            # ──── Decide Next Phase ────
            # Sprint 10: Use WorkflowExecutor guard evaluation (required)
            if not self.workflow_executor:
                raise RuntimeError("Workflow executor required (ensure .duet/workflow.py exists)")

            # Build guard context from dataspace
            guard_context = self.workflow_executor.guard_evaluator.build_guard_context(
                self.dataspace,
                response=response,
                git_changes=response.metadata.get("git_changes"),
            )

            # Evaluate transitions
            guard_result = self.workflow_executor.guard_evaluator.evaluate_transitions(
                current_phase=current_phase,
                workflow_graph=self.workflow_graph,
                guard_context=guard_context,
            )

            # Convert to TransitionDecision format
            if guard_result.next_phase:
                next_phase_name = guard_result.next_phase
                decision = TransitionDecision(
                    next_phase=next_phase_name,
                    rationale=guard_result.rationale,
                    requires_human=False,  # Guards determine this
                )
            else:
                # No guards passed - blocked
                decision = TransitionDecision(
                    next_phase="blocked",
                    rationale=guard_result.rationale,
                    requires_human=True,
                )

            # ──── Guardrail Enforcement ────
            # Sprint DSL-2+: Metadata-based guardrails being phased out
            # Approvals and validation will be handled by tools attached to phases

            # Global approval requirement for terminal phases (keep for now)
            if self.workflow_graph.is_terminal(decision.next_phase) and self.config.workflow.require_human_approval:
                decision = TransitionDecision(
                    next_phase="blocked",
                    rationale="Human approval required before completion (global config).",
                    requires_human=True,
                )

            self.console.log(f"[blue]Decision:[/] {decision.rationale}")

            # ──── Persist Complete Iteration Record ────
            # Build dummy request for persistence compatibility
            dummy_request = AssistantRequest(
                role=phase_def.agent,
                prompt=f"Facet execution: {current_phase}",
                context={"facet": True},
            )
            self.artifacts.persist_iteration(
                run_id=snapshot.run_id,
                iteration=iteration,
                phase=current_phase,
                request=dummy_request,
                response=response,
                decision=decision,
            )
            # Keep JSON interaction artifacts for debugging
            self._persist_interaction(snapshot.run_id, current_phase, dummy_request, response)

            # ──── Database: Insert Iteration Record ────
            if self.db:
                try:
                    # Extract git metadata
                    git_meta = response.metadata.get("git_changes", {})

                    # Extract usage metadata
                    usage_meta = {}
                    if "input_tokens" in response.metadata:
                        usage_meta["input_tokens"] = response.metadata["input_tokens"]
                    if "output_tokens" in response.metadata:
                        usage_meta["output_tokens"] = response.metadata["output_tokens"]
                    if "cached_input_tokens" in response.metadata:
                        usage_meta["cached_input_tokens"] = response.metadata["cached_input_tokens"]

                    # Extract stream metadata
                    stream_meta = {}
                    if "stream_events" in response.metadata:
                        stream_meta["stream_events"] = response.metadata["stream_events"]
                    if "thread_id" in response.metadata:
                        stream_meta["thread_id"] = response.metadata["thread_id"]

                    self.db.insert_iteration(
                        run_id=snapshot.run_id,
                        iteration=iteration,
                        phase=current_phase,
                        prompt=request.prompt,
                        response_content=response.content,
                        verdict=response.verdict,
                        concluded=response.concluded,
                        next_phase=decision.next_phase,
                        requires_human=decision.requires_human,
                        decision_rationale=decision.rationale,
                        git_metadata=git_meta if git_meta else None,
                        usage_metadata=usage_meta if usage_meta else None,
                        stream_metadata=stream_meta if stream_meta else None,
                    )
                except Exception as exc:
                    self.console.log(f"[yellow]DB iteration write failed: {exc}[/]")

            # ──── Edge Case: Human Approval Required ────
            if decision.requires_human and self.config.workflow.require_human_approval:
                snapshot.phase = "blocked"
                snapshot.notes = "Awaiting human approval."
                self.approver.request_approval(snapshot, decision.rationale)
                break

            # ──── Log State Transition ────
            prev_phase = current_phase
            current_phase = decision.next_phase

            self.logger.log_state_transition(
                snapshot.run_id, iteration, prev_phase, current_phase, decision.rationale
            )
            self.artifacts.checkpoint(snapshot)

        snapshot.metadata["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.artifacts.checkpoint(snapshot)

        # ──── Database: Update Run with Final State ────
        if self.db:
            try:
                self.db.update_run(snapshot)
            except Exception as exc:
                self.console.log(f"[yellow]DB final update failed: {exc}[/]")

        # ──── Git Branch Cleanup ────
        if (
            original_branch
            and self.config.workflow.restore_branch_on_complete
            and self.git.is_git_repo()
        ):
            try:
                current_branch = self.git.get_current_branch()
                if current_branch != original_branch:
                    self.console.log(
                        f"[cyan]Restoring original branch:[/] {original_branch}"
                    )
                    self.git.checkout_branch(original_branch)
                    self.console.log(
                        f"[green]Restored to:[/] {original_branch} "
                        f"(feature branch '{current_branch}' preserved)"
                    )
            except Exception as exc:
                self.console.log(
                    f"[yellow]Failed to restore original branch: {exc}[/]"
                )

        # Generate and save run summary
        self.artifacts.save_run_summary(snapshot.run_id)

        # Log run completion (handle arbitrary terminal phases)
        if self.workflow_graph.is_terminal(snapshot.phase):
            status = "completed"
        elif snapshot.phase == "blocked":
            status = "blocked"
        else:
            status = "unknown"

        self.logger.log_run_complete(
            snapshot.run_id, snapshot.phase, snapshot.iteration, status
        )

        self._render_summary(snapshot)
        self.logger.close()
        return snapshot

    def _compose_request(self, phase: str, snapshot: RunSnapshot) -> AssistantRequest:
        """
        Create the request payload based on the active phase.

        Sprint 10: Uses WorkflowExecutor + PromptBuilder for DSL-driven execution.
        Workflow must be loaded (required from Sprint 10 forward).

        Each phase receives inputs from channels:
        - PLAN: task, feedback channels
        - IMPLEMENT: plan channel
        - REVIEW: plan, code channels
        """
        if not self.workflow_executor:
            raise RuntimeError(
                "Workflow executor required. Ensure .duet/workflow.py exists.\n"
                "Run 'duet init' to generate default workflow."
            )

        from .prompt_builder import PromptContext, get_builder

        # Get agent name from workflow
        phase_def = self.workflow_graph.phases.get(phase)
        agent_name = phase_def.agent if phase_def else "system"

        # Build prompt context from snapshot and channels
        prompt_context = PromptContext(
            run_id=snapshot.run_id,
            iteration=snapshot.iteration,
            phase=phase,
            agent=agent_name,
            max_iterations=self.max_iterations,
            channel_payloads=self.workflow_executor.get_current_channels(),
            consecutive_replans=snapshot.metadata.get("consecutive_replans", 0),
            workspace_root=str(self.config.storage.workspace_root),
            metadata=snapshot.metadata,
        )

        # Build request using phase-specific builder (uses role_hint from phase metadata)
        builder = get_builder(phase, phase_def)
        return builder.build(prompt_context)

    def _update_channels_from_response(
        self,
        phase: str,
        response: AssistantResponse,
    ) -> None:
        """
        Update channel store with outputs from response (Sprint 10+).

        Automatically maps response content and metadata to channels based on the
        phase's declared 'publishes' list in the workflow definition.

        Mapping strategy:
        1. For single-channel publishes: content -> channel
        2. For multi-channel publishes: metadata keys -> channels, content -> first channel
        3. Special handling for verdict (from response.verdict or metadata["verdict"])

        Args:
            phase: Phase that was executed
            response: Assistant response with content/metadata
        """
        if not self.workflow_executor:
            return  # No channel store available

        # Get the phase definition from the workflow graph to find published channels
        phase_def = self.workflow_graph.phases.get(phase)
        if not phase_def:
            return

        published_channels = phase_def.get_writes()
        if not published_channels:
            return  # No channels declared for this phase

        # Strategy: Map response to declared channels
        for channel in published_channels:
            channel_name = channel.name  # Extract name from Channel object

            # Special handling for verdict channel (from ReviewVerdict enum or metadata)
            if channel_name == "verdict":
                if response.verdict:
                    self.workflow_executor.channel_store.set(channel_name, response.verdict.value)
                elif "verdict" in response.metadata:
                    self.workflow_executor.channel_store.set(channel_name, response.metadata["verdict"])
                continue

            # Check if metadata has this channel as a key (explicit mapping)
            if channel_name in response.metadata:
                self.workflow_executor.channel_store.set(channel_name, response.metadata[channel_name])
                continue

            # Fallback: Use response content for the first non-verdict channel
            # (Assumes content is the primary output)
            if channel == published_channels[0] or (
                len(published_channels) == 1 and channel_name != "verdict"
            ):
                self.workflow_executor.channel_store.set(channel_name, response.content)

    def _persist_channel_messages(
        self,
        run_id: str,
        phase: str,
        iteration: int,
        state_id: Optional[str] = None,
    ) -> None:
        """
        Persist channel updates as messages in database.

        Saves current channel payloads with metadata for replay and audit.

        Args:
            run_id: Current run ID
            phase: Phase that published messages
            iteration: Iteration number
            state_id: Optional state ID for checkpoint association
        """
        if not self.workflow_executor or not self.db:
            return

        from .channels import serialize_channel_message

        # Get all current channels
        channels = self.workflow_executor.get_current_channels()

        for channel_name, value in channels.items():
            if value is None:
                continue  # Skip empty channels

            # Get schema from workflow
            schema = self.workflow_executor.channel_store.get_schema(channel_name)

            # Serialize message
            serialized = serialize_channel_message(
                channel_name=channel_name,
                value=value,
                schema=schema,
                source_phase=phase,
            )

            # Insert into database
            try:
                self.db.insert_message(
                    run_id=run_id,
                    channel=channel_name,
                    payload=serialized["payload"],
                    state_id=state_id,
                    iteration=iteration,
                    phase=phase,
                    metadata=serialized["metadata"],
                )
            except Exception as exc:
                self.console.log(f"[yellow]Failed to persist message for channel '{channel_name}': {exc}[/]")

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

    def _persist_interaction(
        self, run_id: str, phase: str, request: AssistantRequest, response: AssistantResponse
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        timestamp_iso = now.isoformat()  # For payload
        timestamp_safe = now.strftime("%Y%m%dT%H%M%S%fZ")  # For filename (Windows-safe)
        payload = {
            "phase": phase,
            "timestamp": timestamp_iso,
            "request": request.model_dump(),
            "response": response.model_dump(),
        }
        filename = f"{timestamp_safe}-{phase}.json"
        self.artifacts.write_json(run_id, f"interactions/{filename}", payload)

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
        # For now, use a synthetic response (real agent integration coming)
        response = AssistantResponse(
            content=f"Facet '{phase}' executed {len(facet_result.step_logs)} steps",
            metadata={
                "facet_execution": True,
                "steps_executed": len(facet_result.step_logs),
                "channel_writes": list(facet_result.channel_writes.keys()),
                "step_logs": facet_result.step_logs,
            },
        )

        return response

    # ──────────────────────────────────────────────────────────────────────────
    # Single-Phase Execution
    # ──────────────────────────────────────────────────────────────────────────

    def run_next_phase(
        self,
        run_id: Optional[str] = None,
        feedback: Optional[str] = None,
    ) -> dict:
        """
        Execute the next phase for a stateful run.

        Args:
            run_id: Run identifier (creates new run if None)
            feedback: Optional user feedback to include in prompt

        Returns:
            Dictionary with:
            - run_id: Run identifier
            - state_id: New state identifier
            - phase: Phase that was executed
            - phase_status: New phase status
            - next_action: Suggested next action (continue/blocked/done)
            - message: Human-readable status message
        """
        # Check for workflow updates and reload if needed
        self._check_and_reload_workflow()

        if not self.db:
            raise RuntimeError("Database required for stateful workflow. Initialize with DuetDatabase.")

        # ──── Load or Create Run ────
        if run_id:
            # Load existing run
            run = self.db.get_run(run_id)
            if not run:
                raise ValueError(f"Run not found: {run_id}")

            # Get active state
            active_state = self.db.get_active_state(run_id)
            if not active_state:
                # No active state, get latest state or start fresh
                active_state = self.db.get_latest_state(run_id)
                if not active_state:
                    # Create initial state using workflow's initial phase
                    initial_phase = self.workflow_graph.initial_phase
                    phase_status = f"{initial_phase}-ready"
                    state_id = f"{run_id}-{phase_status}"
                    baseline = None
                    initial_metadata = {}
                    if self.git.is_git_repo():
                        try:
                            baseline_info = self.git.create_state_baseline(state_id)
                            baseline = baseline_info["commit"]
                            initial_metadata.update(baseline_info)
                        except GitError:
                            pass

                    # Add initial channel snapshot
                    if self.workflow_executor:
                        initial_metadata["channel_snapshot"] = self.workflow_executor.get_current_channels()

                    self.db.insert_state(
                        state_id=state_id,
                        run_id=run_id,
                        phase_status=phase_status,
                        baseline_commit=baseline,
                        notes="Initial state",
                        metadata=initial_metadata if initial_metadata else None,
                    )
                    self.db.update_active_state(run_id, state_id)
                    active_state = self.db.get_state(state_id)

            # Load persisted facts into dataspace (e.g., ApprovalGrants)
            self.load_persisted_facts(run_id)

            # Check if any waiting facets can resume due to grants
            resumed = self.scheduler.check_approvals()
            if resumed > 0:
                self.console.log(f"[green]Resumed {resumed} facet(s) with pending approvals[/]")

            # Restore channel snapshot if available
            if self.workflow_executor and active_state.get("metadata"):
                channel_snapshot = active_state["metadata"].get("channel_snapshot")
                if channel_snapshot:
                    self.workflow_executor.restore_channels(channel_snapshot)
                    self.console.log(f"[dim]Restored channel snapshot:[/] {len(channel_snapshot)} channels")
                else:
                    # Fallback: replay from message history
                    state_messages = self.db.get_state_messages(active_state["state_id"])
                    if state_messages:
                        self.workflow_executor.replay_from_messages(state_messages)
                        self.console.log(f"[dim]Replayed {len(state_messages)} messages to restore channels[/]")

            snapshot = RunSnapshot(
                run_id=run_id,
                iteration=run["iteration"],
                phase=run["phase"],
                metadata={
                    "started_at": run["started_at"],
                    "consecutive_replans": run.get("consecutive_replans", 0),
                    "original_branch": run.get("original_branch"),
                    "feature_branch": run.get("feature_branch"),
                    "baseline_commit": run.get("baseline_commit"),
                },
            )
        else:
            # Create new run
            run_id = self._derive_run_id()
            snapshot = RunSnapshot(
                run_id=run_id,
                iteration=0,
                phase=self.workflow_graph.initial_phase,
                metadata={
                    "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "consecutive_replans": 0,
                },
            )

            # Insert run
            self.db.insert_run(snapshot)

            # Reset and seed channels for new run
            if self.workflow_executor:
                self.workflow_executor.channel_store.clear()

                # Seed task channel using workflow configuration
                task_channel_name = self.workflow_graph.get_task_channel()
                if task_channel_name:
                    task_input = snapshot.metadata.get("task")
                    if not task_input:
                        # Fallback for tests/backwards compatibility
                        task_input = "Execute workflow"
                        self.console.log("[dim]No task input provided, using default[/]")

                    self.workflow_executor.seed_channel(task_channel_name, task_input)
                    self.console.log(f"[dim]Initialized channels, seeded '{task_channel_name}'[/]")
                else:
                    self.console.log("[dim]No task channel configured (workflow may not need one)[/]")

            # Create initial state using workflow's initial phase
            initial_phase = self.workflow_graph.initial_phase
            phase_status = f"{initial_phase}-ready"
            state_id = f"{run_id}-{phase_status}"
            baseline = None
            initial_metadata = {}
            if self.git.is_git_repo():
                try:
                    baseline_info = self.git.create_state_baseline(state_id)
                    baseline = baseline_info["commit"]
                    initial_metadata.update(baseline_info)
                except GitError:
                    pass

            # Add initial channel snapshot
            if self.workflow_executor:
                initial_metadata["channel_snapshot"] = self.workflow_executor.get_current_channels()

            self.db.insert_state(
                state_id=state_id,
                run_id=run_id,
                phase_status=phase_status,
                baseline_commit=baseline,
                notes="Initial state",
                metadata=initial_metadata if initial_metadata else None,
            )
            self.db.update_active_state(run_id, state_id)
            active_state = self.db.get_state(state_id)

        # ──── Determine Phase to Execute ────
        phase_status = active_state["phase_status"]
        current_phase, action_needed = self._parse_phase_status(phase_status)

        if action_needed == "done":
            return {
                "run_id": run_id,
                "state_id": active_state["state_id"],
                "phase": "done",
                "phase_status": phase_status,
                "next_action": "done",
                "message": "Run already completed",
            }

        if action_needed == "blocked":
            return {
                "run_id": run_id,
                "state_id": active_state["state_id"],
                "phase": current_phase if current_phase else "blocked",
                "phase_status": phase_status,
                "next_action": "blocked",
                "message": "Run is blocked, requires intervention",
            }

        # ──── Execute Phase ────
        snapshot.iteration += 1
        snapshot.phase = current_phase
        phase_start_time = dt.datetime.now(dt.timezone.utc)

        self.console.rule(f"[bold cyan]{current_phase.upper()}[/] (Iteration {snapshot.iteration})")

        # Compose request (include feedback if provided and phase consumes a feedback-like channel)
        request = self._compose_request(current_phase, snapshot)
        phase_def = self.workflow_graph.phases.get(current_phase)
        if feedback and phase_def:
            # Find a feedback channel in phase reads
            # Look for: 1) channel named "feedback", 2) channel with schema "text" consumed by phase
            feedback_channel_name = None
            for channel in phase_def.get_reads():
                if channel.name == "feedback":
                    feedback_channel_name = channel.name
                    break
                # Fallback: use any text channel that might accept feedback
                if channel.schema == "text" and not feedback_channel_name:
                    feedback_channel_name = channel.name

            if feedback_channel_name:
                # Inject user feedback into request for phases that consume it
                request.prompt += f"\n\n──── User Feedback ────\n{feedback}\n"
                request.context["user_feedback"] = feedback
                # Seed the feedback channel
                if self.workflow_executor:
                    self.workflow_executor.seed_channel(feedback_channel_name, feedback)
                    self.console.log(f"[dim]Seeded '{feedback_channel_name}' channel with user feedback[/]")

        adapter = self._select_adapter(current_phase)
        adapter_name = adapter.__class__.__name__

        # Streaming display setup
        from .models import StreamMode
        stream_mode = self.config.logging.stream_mode
        if self.config.logging.quiet:
            stream_mode = StreamMode.OFF

        streaming_display = None if stream_mode == StreamMode.OFF else EnhancedStreamingDisplay(
            console=self.console,
            phase=current_phase,
            iteration=snapshot.iteration,
            mode=stream_mode.value,
        )

        # Create event handler
        event_handler = self._create_event_handler(
            snapshot.run_id, snapshot.iteration, current_phase, display=streaming_display
        )

        # Execute phase
        try:
            if streaming_display:
                with Live(
                    streaming_display.render(),
                    console=self.console,
                    refresh_per_second=4,
                    transient=True,
                ) as live:
                    def live_event_handler(event: StreamEvent) -> None:
                        event_handler(event)
                        live.update(streaming_display.render())

                    response = adapter.stream(request, on_event=live_event_handler)
            else:
                response = adapter.stream(request, on_event=event_handler)
        except Exception as exc:
            self.console.log(f"[red]Phase execution failed: {exc}[/]")
            # Create blocked state
            new_state_id = f"{run_id}-blocked"
            self.db.insert_state(
                state_id=new_state_id,
                run_id=run_id,
                phase_status="blocked",
                parent_state_id=active_state["state_id"],
                notes=f"Phase {current_phase} failed: {exc}",
            )
            self.db.update_active_state(run_id, new_state_id)

            return {
                "run_id": run_id,
                "state_id": new_state_id,
                "phase": current_phase,
                "phase_status": "blocked",
                "next_action": "blocked",
                "message": f"Phase failed: {exc}",
            }

        # Extract verdict if phase writes verdict channel
        phase_def = self.workflow_graph.phases.get(current_phase)
        publishes_verdict = phase_def and any(ch.name == "verdict" for ch in phase_def.get_writes())
        if publishes_verdict and "verdict" in response.metadata:
            verdict_str = response.metadata["verdict"]
            if isinstance(verdict_str, str):
                verdict_lower = verdict_str.lower().strip()
                if verdict_lower in ("approve", "approved"):
                    response.verdict = ReviewVerdict.APPROVE
                elif verdict_lower in ("changes_requested", "changes requested", "revise"):
                    response.verdict = ReviewVerdict.CHANGES_REQUESTED
                elif verdict_lower in ("blocked", "block"):
                    response.verdict = ReviewVerdict.BLOCKED

        # ──── Update Channels ────
        self._update_channels_from_response(current_phase, response)

        permission_denials = response.metadata.get("permission_denials")
        permission_required = response.metadata.get("permission_required")
        permission_message = None
        if permission_required and permission_denials:
            permission_message = "Claude Code requested permission to proceed with implementation."
            self.console.log(
                "[yellow]Claude Code requested permission; review required before continuing.[/]"
            )

        # Check for empty response
        if not response.content or not response.content.strip():
            self.console.log("[yellow]Empty response detected[/]")
            new_state_id = f"{run_id}-blocked"
            self.db.insert_state(
                state_id=new_state_id,
                run_id=run_id,
                phase_status="blocked",
                parent_state_id=active_state["state_id"],
                notes=f"Empty response in {current_phase}",
            )
            self.db.update_active_state(run_id, new_state_id)

            return {
                "run_id": run_id,
                "state_id": new_state_id,
                "phase": current_phase,
                "phase_status": "blocked",
                "next_action": "blocked",
                "message": "Empty response received",
            }

        if permission_required and permission_denials:
            decision = TransitionDecision(
                next_phase="blocked",
                rationale=permission_message or "Claude Code requested permission to proceed.",
                requires_human=True,
            )
            response.metadata.setdefault("notes", permission_message)
            response.metadata["permission_denials"] = permission_denials
        else:
            if not self.workflow_executor:
                raise RuntimeError("Workflow executor required (ensure .duet/workflow.py exists)")

            guard_context = self.workflow_executor.guard_evaluator.build_guard_context(
                self.dataspace,
                response=response,
                git_changes=response.metadata.get("git_changes"),
            )

            guard_result = self.workflow_executor.guard_evaluator.evaluate_transitions(
                current_phase=current_phase,
                workflow_graph=self.workflow_graph,
                guard_context=guard_context,
            )

            if guard_result.next_phase:
                next_phase_name = guard_result.next_phase
                decision = TransitionDecision(
                    next_phase=next_phase_name,
                    rationale=guard_result.rationale,
                    requires_human=False,
                )
            else:
                decision = TransitionDecision(
                    next_phase="blocked",
                    rationale=guard_result.rationale,
                    requires_human=True,
                )

        # Apply guardrails
        # Sprint DSL-2+: Metadata-based guardrails removed, validation moves to tools

        # Global approval requirement for terminal phases (keep for now)
        if self.workflow_graph.is_terminal(decision.next_phase) and self.config.workflow.require_human_approval:
            decision = TransitionDecision(
                next_phase="blocked",
                rationale="Human approval required before completion (global config).",
                requires_human=True,
            )

        # ──── Persist Iteration ────
        git_meta = response.metadata.get("git_changes", {})
        usage_meta = {
            "input_tokens": response.metadata.get("input_tokens"),
            "output_tokens": response.metadata.get("output_tokens"),
            "cached_input_tokens": response.metadata.get("cached_input_tokens"),
        }
        stream_meta = {
            "stream_events": response.metadata.get("stream_events"),
            "thread_id": response.metadata.get("thread_id"),
        }

        self.db.insert_iteration(
            run_id=snapshot.run_id,
            iteration=snapshot.iteration,
            phase=current_phase,
            prompt=request.prompt,
            response_content=response.content,
            verdict=response.verdict,
            concluded=response.concluded,
            next_phase=decision.next_phase,
            requires_human=decision.requires_human,
            decision_rationale=decision.rationale,
            git_metadata=git_meta if git_meta else None,
            usage_metadata=usage_meta if any(usage_meta.values()) else None,
            stream_metadata=stream_meta if any(stream_meta.values()) else None,
        )

        # ──── Create New State ────
        new_phase_status = self._derive_phase_status(current_phase, decision.next_phase)
        new_state_id = f"{run_id}-{new_phase_status}"

        # Create git baseline if needed
        baseline = active_state.get("baseline_commit")
        state_metadata = {}
        if self.git.is_git_repo():
            try:
                baseline_info = self.git.create_state_baseline(new_state_id)
                baseline = baseline_info["commit"]
                # Store full baseline info (branch, state_branch, clean) for duet back
                state_metadata.update(baseline_info)

                # Warn if no baseline (no commits)
                if baseline is None:
                    self.console.log(
                        "[yellow]⚠ No git commits - 'duet back' won't restore changes. "
                        "Run: [cyan]duet init --init-git --force[/cyan] or commit manually[/]"
                    )
            except GitError as exc:
                self.console.log(f"[yellow]Git baseline creation failed: {exc}[/]")

        # Add channel snapshot to state metadata
        if self.workflow_executor:
            channel_snapshot = self.workflow_executor.get_current_channels()
            state_metadata["channel_snapshot"] = channel_snapshot
            self.console.log(f"[dim]Saved channel snapshot:[/] {len(channel_snapshot)} channels")

        # Insert state first (required for FK constraint)
        self.db.insert_state(
            state_id=new_state_id,
            run_id=run_id,
            phase_status=new_phase_status,
            baseline_commit=baseline,
            parent_state_id=active_state["state_id"],
            notes=decision.rationale,
            verdict=response.verdict.value if response.verdict else None,
            feedback=feedback,
            metadata=state_metadata if state_metadata else None,
        )
        self.db.update_active_state(run_id, new_state_id)

        # Persist channel messages AFTER state exists (FK constraint)
        if self.db and self.workflow_executor:
            self._persist_channel_messages(
                run_id=run_id,
                phase=current_phase,
                iteration=snapshot.iteration,
                state_id=new_state_id,
            )
            self.console.log(f"[dim]Persisted channel messages for state[/]")

        # Update run record
        snapshot.phase = decision.next_phase
        snapshot.iteration = snapshot.iteration

        self.db.update_run(snapshot)

        # ──── Determine Next Action ────
        next_action = "continue"
        if self.workflow_graph.is_terminal(decision.next_phase):
            next_action = "done"
        elif decision.next_phase == "blocked" or decision.requires_human:
            next_action = "blocked"

        message = f"Phase {current_phase} complete. {decision.rationale}"

        self.console.log(f"[green]State created:[/] {new_state_id}")
        self.console.log(f"[blue]{message}[/]")

        return {
            "run_id": run_id,
            "state_id": new_state_id,
            "phase": current_phase,
            "phase_status": new_phase_status,
            "next_action": next_action,
            "message": message,
        }

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
