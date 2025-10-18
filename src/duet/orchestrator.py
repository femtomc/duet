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
from .git_operations import GitWorkspace, GitError
from .logging import DuetLogger
from .models import (
    AssistantRequest,
    AssistantResponse,
    Phase,
    ReviewVerdict,
    RunSnapshot,
    TransitionDecision,
)
from .persistence import DuetDatabase, PersistenceError
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
    ) -> None:
        self.config = config
        self.artifacts = artifact_store
        self.console = console or Console()
        self.codex_adapter = self._build_adapter(config.codex)
        self.claude_adapter = self._build_adapter(config.claude)
        self.max_iterations = config.workflow.max_iterations
        self._stop_requested = False
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
        self, run_id: str, iteration: int, phase: Phase, display: Optional[StreamingDisplay] = None
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
                        phase=phase.value,
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
        iteration = 0
        snapshot = RunSnapshot(
            run_id=run_id or self._derive_run_id(),
            iteration=iteration,
            phase=Phase.PLAN,
            metadata={
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "consecutive_replans": 0,
            },
        )
        self.console.rule(f"Starting Duet run {snapshot.run_id}")
        self.artifacts.checkpoint(snapshot)

        # ──── Database: Insert Run Record ────
        if self.db:
            try:
                self.db.insert_run(snapshot)
            except Exception as exc:
                self.console.log(f"[yellow]DB write failed: {exc}[/]")

        # ──── Git Baseline Commit (for change detection) ────
        baseline_commit = None
        if self.git.is_git_repo():
            try:
                baseline_commit = self.git.get_current_commit()
                snapshot.metadata["baseline_commit"] = baseline_commit
                self.console.log(f"[dim]Baseline commit: {baseline_commit[:8]}[/]")
            except Exception:
                # No commits yet (empty repo)
                baseline_commit = None
                self.console.log("[dim]No commits yet (empty repository)[/]")

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

        current_phase = Phase.PLAN
        consecutive_replans = 0
        while True:
            # ──── Edge Case: Manual Stop ────
            if self._stop_requested:
                snapshot.phase = Phase.BLOCKED
                snapshot.notes = "Manual stop requested by user (SIGINT/SIGTERM)."
                self.console.log("[yellow]Manual stop detected, marking run as BLOCKED[/]")
                break

            # ──── Edge Case: Max Iterations ────
            if iteration >= self.max_iterations and current_phase != Phase.DONE:
                snapshot.phase = Phase.BLOCKED
                snapshot.notes = f"Max iterations ({self.max_iterations}) reached without completion."
                self.console.log("[yellow]Max iterations reached, marking run as BLOCKED[/]")
                break

            # ──── Terminal Phase Check ────
            if current_phase == Phase.DONE:
                snapshot.phase = Phase.DONE
                snapshot.notes = "Run completed successfully."
                self.console.log("[green]Run completed[/]")
                break

            # ──── Begin Iteration ────
            iteration += 1
            snapshot.iteration = iteration
            snapshot.phase = current_phase
            phase_start_time = dt.datetime.now(dt.timezone.utc)
            self.logger.log_iteration_start(snapshot.run_id, iteration, current_phase.value)

            # ──── Compose Request ────
            request = self._compose_request(current_phase, snapshot)
            adapter = self._select_adapter(current_phase)
            adapter_name = adapter.__class__.__name__
            self.logger.log_adapter_call(snapshot.run_id, iteration, current_phase.value, adapter_name)

            # ──── Streaming Display Setup (Sprint 7: EnhancedStreamingDisplay) ────
            from .models import StreamMode
            stream_mode = self.config.logging.stream_mode
            # Handle legacy quiet flag (maps to stream_mode="off")
            if self.config.logging.quiet:
                stream_mode = StreamMode.OFF

            streaming_display = None if stream_mode == StreamMode.OFF else EnhancedStreamingDisplay(
                console=self.console,
                phase=current_phase,
                iteration=iteration,
                mode=stream_mode.value,  # Pass string value to display
            )

            # ──── Create Event Handler for Streaming ────
            event_handler = self._create_event_handler(
                snapshot.run_id, iteration, current_phase, display=streaming_display
            )

            # ──── Edge Case: Adapter Failure ────
            try:
                # Use Rich Live display if not in quiet mode
                if streaming_display:
                    with Live(
                        streaming_display.render(),
                        console=self.console,
                        refresh_per_second=4,
                        transient=True,  # Remove display after completion
                    ) as live:
                        # Create wrapper that updates live display
                        def live_event_handler(event: StreamEvent) -> None:
                            event_handler(event)  # Persist + add to display
                            live.update(streaming_display.render())  # Refresh display

                        response = adapter.stream(request, on_event=live_event_handler)
                else:
                    response = adapter.stream(request, on_event=event_handler)
            except Exception as exc:
                snapshot.phase = Phase.BLOCKED
                snapshot.notes = f"Adapter failure during {current_phase.value}: {exc}"
                self.logger.log_error(
                    snapshot.run_id, iteration, current_phase.value, "adapter_failure", str(exc)
                )
                self._persist_interaction(
                    snapshot.run_id,
                    current_phase,
                    request,
                    AssistantResponse(content="", metadata={"error": str(exc)}),
                )
                break

            # ──── Extract Verdict from Metadata (REVIEW phase) ────
            if current_phase == Phase.REVIEW and "verdict" in response.metadata:
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

            # ──── Edge Case: Empty Response ────
            if not response.content or not response.content.strip():
                snapshot.phase = Phase.BLOCKED
                snapshot.notes = f"Empty response received during {current_phase.value}."
                self.console.log("[yellow]Empty response detected, marking run as BLOCKED[/]")
                self._persist_interaction(snapshot.run_id, current_phase, request, response)
                break

            # ──── Guardrail: Phase Runtime Limit ────
            if self.config.workflow.max_phase_runtime_seconds:
                phase_duration = (dt.datetime.now(dt.timezone.utc) - phase_start_time).total_seconds()
                if phase_duration > self.config.workflow.max_phase_runtime_seconds:
                    snapshot.phase = Phase.BLOCKED
                    snapshot.notes = (
                        f"Phase {current_phase.value} exceeded maximum runtime "
                        f"({self.config.workflow.max_phase_runtime_seconds}s)"
                    )
                    self.console.log(
                        f"[red]Phase runtime limit exceeded: {phase_duration:.1f}s[/]"
                    )
                    self._persist_interaction(snapshot.run_id, current_phase, request, response)
                    break

            # ──── Implementation Validation: Detect Repository Changes ────
            if current_phase == Phase.IMPLEMENT and self.config.workflow.require_git_changes:
                try:
                    if self.git.is_git_repo():
                        # Compare against baseline commit to detect new commits
                        # (not just working tree changes, which become empty after commit)
                        changes = self.git.detect_changes(baseline_commit=baseline_commit)

                        # Get current commit to check if new commit was created
                        current_commit = None
                        try:
                            current_commit = self.git.get_current_commit()
                        except GitError:
                            pass

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

                        # Fail iteration if no changes detected (guardrail enforcement)
                        if not changes.has_changes:
                            snapshot.phase = Phase.BLOCKED
                            snapshot.notes = (
                                "Implementation phase produced no repository changes. "
                                "Claude must modify files, stage changes, or create commits."
                            )
                            self.console.log(
                                "[red]No repository changes detected after IMPLEMENT phase[/]"
                            )
                            self._persist_interaction(snapshot.run_id, current_phase, request, response)
                            break

                        # Log detected changes
                        if new_commits_created:
                            self.console.log(
                                f"[green]New commit created:[/] {current_commit[:8]} "
                                f"({changes.files_changed} files, +{changes.insertions}/-{changes.deletions})"
                            )
                        else:
                            self.console.log(
                                f"[green]Detected changes:[/] {changes.files_changed} files, "
                                f"+{changes.insertions}/-{changes.deletions}"
                            )
                    else:
                        self.console.log("[yellow]Workspace is not a git repository, skipping change detection[/]")
                except Exception as exc:
                    # Git detection failure shouldn't block the run, just warn
                    # Escape square brackets to avoid Rich markup errors
                    error_msg = str(exc).replace("[", "\\[").replace("]", "\\]")
                    self.console.log(f"[yellow]Git change detection failed: {error_msg}[/]")
                    response.metadata["git_changes"] = {"error": str(exc)}

            # ──── Decide Next Phase ────
            decision = self._decide_next_phase(current_phase, response, iteration, consecutive_replans)
            self.console.log(f"[blue]Decision:[/] {decision.rationale}")

            # ──── Persist Complete Iteration Record ────
            self.artifacts.persist_iteration(
                run_id=snapshot.run_id,
                iteration=iteration,
                phase=current_phase,
                request=request,
                response=response,
                decision=decision,
            )
            # Keep legacy interactions for backward compatibility during transition
            self._persist_interaction(snapshot.run_id, current_phase, request, response)

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
                snapshot.phase = Phase.BLOCKED
                snapshot.notes = "Awaiting human approval."
                self.approver.request_approval(snapshot, decision.rationale)
                break

            # ──── Log State Transition & Track Replans ────
            prev_phase = current_phase
            current_phase = decision.next_phase

            # Track consecutive replans for guardrail enforcement
            if prev_phase == Phase.REVIEW and current_phase == Phase.PLAN:
                consecutive_replans += 1
                snapshot.metadata["consecutive_replans"] = consecutive_replans
                self.console.log(
                    f"[yellow]Consecutive replans: {consecutive_replans}/{self.config.workflow.max_consecutive_replans}[/]"
                )
            elif current_phase == Phase.DONE:
                consecutive_replans = 0
                snapshot.metadata["consecutive_replans"] = 0

            self.logger.log_state_transition(
                snapshot.run_id, iteration, prev_phase.value, current_phase.value, decision.rationale
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

        # Log run completion
        status_map = {
            Phase.DONE: "completed",
            Phase.BLOCKED: "blocked",
        }
        status = status_map.get(snapshot.phase, "unknown")
        self.logger.log_run_complete(
            snapshot.run_id, snapshot.phase.value, snapshot.iteration, status
        )

        self._render_summary(snapshot)
        self.logger.close()
        return snapshot

    def _compose_request(self, phase: Phase, snapshot: RunSnapshot) -> AssistantRequest:
        """
        Create the request payload based on the active phase.

        Each phase receives actionable inputs:
        - PLAN: Prior review feedback (if looping), iteration context, objectives
        - IMPLEMENT: Current plan, workspace path, repository context
        - REVIEW: Plan + implementation response, iteration history summary
        """
        role_map = {
            Phase.PLAN: "planner",
            Phase.IMPLEMENT: "implementer",
            Phase.REVIEW: "reviewer",
        }

        # ──── Base Context (common to all phases) ────
        context = {
            "iteration": snapshot.iteration,
            "run_id": snapshot.run_id,
            "phase": phase.value,
            "max_iterations": self.max_iterations,
            "workspace_root": str(self.config.storage.workspace_root),
            "run_metadata": snapshot.metadata,
        }

        # ──── Phase-Specific Context and Prompts ────
        if phase == Phase.PLAN:
            prompt, phase_context = self._compose_plan_request(snapshot)
        elif phase == Phase.IMPLEMENT:
            prompt, phase_context = self._compose_implement_request(snapshot)
        elif phase == Phase.REVIEW:
            prompt, phase_context = self._compose_review_request(snapshot)
        else:
            prompt = "No prompt defined for this phase."
            phase_context = {}

        context.update(phase_context)
        return AssistantRequest(role=role_map.get(phase, "system"), prompt=prompt, context=context)

    def _compose_plan_request(self, snapshot: RunSnapshot) -> tuple[str, dict]:
        """Compose request for PLAN phase with prior feedback if available."""
        prompt_parts = [
            "Draft the implementation plan for the next increment.",
            "",
            f"Iteration: {snapshot.iteration}/{self.max_iterations}",
        ]

        phase_context = {}

        # If this is not the first iteration, include prior review feedback
        if snapshot.iteration > 1:
            prior_review = self._load_prior_response(snapshot.run_id, Phase.REVIEW)
            if prior_review:
                prompt_parts.extend(
                    [
                        "",
                        "──── Prior Review Feedback ────",
                        prior_review.get("content", "No feedback available."),
                        "",
                        "Please revise the plan to address the review feedback above.",
                    ]
                )
                phase_context["prior_review_feedback"] = prior_review

        prompt = "\n".join(prompt_parts)
        return prompt, phase_context

    def _compose_implement_request(self, snapshot: RunSnapshot) -> tuple[str, dict]:
        """Compose request for IMPLEMENT phase with the current plan."""
        prompt_parts = [
            "Apply the plan to the repository and provide a commit summary.",
            "",
            f"Iteration: {snapshot.iteration}/{self.max_iterations}",
            f"Workspace: {self.config.storage.workspace_root}",
        ]

        phase_context = {}

        # Load the current plan from this iteration
        plan_response = self._load_prior_response(snapshot.run_id, Phase.PLAN)
        if plan_response:
            prompt_parts.extend(
                [
                    "",
                    "──── Implementation Plan ────",
                    plan_response.get("content", "No plan available."),
                    "",
                    "Follow the plan above to implement the changes.",
                ]
            )
            phase_context["current_plan"] = plan_response

        prompt = "\n".join(prompt_parts)
        return prompt, phase_context

    def _compose_review_request(self, snapshot: RunSnapshot) -> tuple[str, dict]:
        """Compose request for REVIEW phase with plan + implementation response."""
        prompt_parts = [
            "Review the latest changes and provide a structured verdict.",
            "",
            f"Iteration: {snapshot.iteration}/{self.max_iterations}",
        ]

        phase_context = {}

        # Load plan and implementation from current iteration
        plan_response = self._load_prior_response(snapshot.run_id, Phase.PLAN)
        impl_response = self._load_prior_response(snapshot.run_id, Phase.IMPLEMENT)

        if plan_response:
            prompt_parts.extend(
                ["", "──── Plan ────", plan_response.get("content", "No plan available.")]
            )
            phase_context["plan"] = plan_response

        if impl_response:
            prompt_parts.extend(
                [
                    "",
                    "──── Implementation ────",
                    impl_response.get("content", "No implementation available."),
                ]
            )
            phase_context["implementation"] = impl_response

        prompt_parts.extend(
            [
                "",
                "Assess whether the implementation meets the plan's requirements.",
                "",
                "Provide your verdict as one of:",
                "- APPROVE: Changes are acceptable, ready to proceed",
                "- CHANGES_REQUESTED: Revisions needed, will loop back to planning",
                "- BLOCKED: Critical issues requiring human intervention",
                "",
                "Include your verdict in the response metadata as 'verdict'.",
                "Set 'concluded' to True only if verdict is APPROVE.",
            ]
        )

        prompt = "\n".join(prompt_parts)
        return prompt, phase_context

    def _load_prior_response(self, run_id: str, phase: Phase) -> dict | None:
        """Load the most recent response for a given phase from artifacts."""
        try:
            interactions_dir = self.artifacts.run_dir(run_id) / "interactions"
            if not interactions_dir.exists():
                return None

            # Find most recent interaction file for this phase
            phase_files = sorted(interactions_dir.glob(f"*-{phase.value}.json"), reverse=True)
            if not phase_files:
                return None

            with phase_files[0].open("r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("response")
        except Exception as exc:
            self.console.log(f"[yellow]Warning: Could not load prior {phase.value} response: {exc}[/]")
            return None

    def _select_adapter(self, phase: Phase) -> AssistantAdapter:
        if phase in (Phase.PLAN, Phase.REVIEW):
            return self.codex_adapter
        if phase == Phase.IMPLEMENT:
            return self.claude_adapter
        return self.codex_adapter

    def _decide_next_phase(
        self,
        phase: Phase,
        response: AssistantResponse,
        iteration: int,
        consecutive_replans: int = 0,
    ) -> TransitionDecision:
        """
        Determine next phase based on current phase and response.

        Transition Rules:
        - PLAN → IMPLEMENT (always, if valid response)
        - IMPLEMENT → REVIEW (always, if valid response)
        - REVIEW → DONE (if response.verdict == APPROVE or concluded == True)
        - REVIEW → PLAN (if response.verdict == CHANGES_REQUESTED)
        - REVIEW → BLOCKED (if response.verdict == BLOCKED)

        Guardrails:
        - Approaching max iterations: set requires_human = True
        - Max consecutive replans exceeded: set requires_human = True
        - Invalid/ambiguous response: could set requires_human = True
        """
        # ──── PLAN Phase ────
        if phase == Phase.PLAN:
            rationale = "Plan drafted, proceeding to implementation."
            return TransitionDecision(next_phase=Phase.IMPLEMENT, rationale=rationale)

        # ──── IMPLEMENT Phase ────
        if phase == Phase.IMPLEMENT:
            rationale = "Implementation response recorded, proceeding to review."
            return TransitionDecision(next_phase=Phase.REVIEW, rationale=rationale)

        # ──── REVIEW Phase ────
        if phase == Phase.REVIEW:
            # Honor structured verdict if provided
            if response.verdict:
                if response.verdict == ReviewVerdict.APPROVE:
                    rationale = "Review verdict: APPROVE - marking run as DONE."
                    return TransitionDecision(next_phase=Phase.DONE, rationale=rationale)

                elif response.verdict == ReviewVerdict.BLOCKED:
                    rationale = "Review verdict: BLOCKED - critical issues require human intervention."
                    return TransitionDecision(
                        next_phase=Phase.BLOCKED,
                        rationale=rationale,
                        requires_human=True,
                    )

                elif response.verdict == ReviewVerdict.CHANGES_REQUESTED:
                    # ──── Guardrail: Check Consecutive Replans ────
                    approaching_iteration_limit = iteration >= self.max_iterations - 1
                    exceeds_replan_limit = (
                        consecutive_replans >= self.config.workflow.max_consecutive_replans - 1
                    )
                    requires_human = approaching_iteration_limit or exceeds_replan_limit

                    if exceeds_replan_limit:
                        rationale = (
                            f"Review verdict: CHANGES_REQUESTED - consecutive replans "
                            f"{consecutive_replans + 1}/{self.config.workflow.max_consecutive_replans} "
                            "(replan limit reached, human approval needed)."
                        )
                    elif approaching_iteration_limit:
                        rationale = (
                            f"Review verdict: CHANGES_REQUESTED - iteration {iteration}/{self.max_iterations} "
                            "(approaching limit, human approval may be needed)."
                        )
                    else:
                        rationale = "Review verdict: CHANGES_REQUESTED - looping back to planning."

                    return TransitionDecision(
                        next_phase=Phase.PLAN, rationale=rationale, requires_human=requires_human
                    )

            # Fallback to legacy 'concluded' field if no verdict
            if response.concluded:
                rationale = "Review approved (legacy concluded=True); marking run as DONE."
                return TransitionDecision(next_phase=Phase.DONE, rationale=rationale)

            # Default: request changes and loop back (legacy path)
            approaching_iteration_limit = iteration >= self.max_iterations - 1
            exceeds_replan_limit = (
                consecutive_replans >= self.config.workflow.max_consecutive_replans - 1
            )
            requires_human = approaching_iteration_limit or exceeds_replan_limit

            if exceeds_replan_limit:
                rationale = (
                    f"Review requested changes - consecutive replans "
                    f"{consecutive_replans + 1}/{self.config.workflow.max_consecutive_replans} "
                    "(replan limit reached, human approval needed)."
                )
            elif approaching_iteration_limit:
                rationale = (
                    f"Review requested changes; iteration {iteration}/{self.max_iterations} "
                    "(approaching limit, human approval may be needed)."
                )
            else:
                rationale = "Review requested changes; looping back to planning for revisions."

            return TransitionDecision(
                next_phase=Phase.PLAN, rationale=rationale, requires_human=requires_human
            )

        # ──── Fallback ────
        return TransitionDecision(next_phase=Phase.DONE, rationale="Reached unexpected phase.")

    def _persist_interaction(
        self, run_id: str, phase: Phase, request: AssistantRequest, response: AssistantResponse
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        timestamp_iso = now.isoformat()  # For payload
        timestamp_safe = now.strftime("%Y%m%dT%H%M%S%fZ")  # For filename (Windows-safe)
        payload = {
            "phase": phase.value,
            "timestamp": timestamp_iso,
            "request": request.model_dump(),
            "response": response.model_dump(),
        }
        filename = f"{timestamp_safe}-{phase.value}.json"
        self.artifacts.write_json(run_id, f"interactions/{filename}", payload)

    def _derive_run_id(self) -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("run-%Y%m%d-%H%M%S")

    # ──────────────────────────────────────────────────────────────────────────
    # Single-Phase Execution (Sprint 8)
    # ──────────────────────────────────────────────────────────────────────────

    def run_next_phase(
        self,
        run_id: Optional[str] = None,
        feedback: Optional[str] = None,
    ) -> dict:
        """
        Execute the next phase for a stateful run (Sprint 8).

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
                    # Create initial state
                    state_id = f"{run_id}-plan-ready"
                    baseline = None
                    if self.git.is_git_repo():
                        try:
                            baseline = self.git.get_current_commit()
                        except GitError:
                            pass

                    self.db.insert_state(
                        state_id=state_id,
                        run_id=run_id,
                        phase_status="plan-ready",
                        baseline_commit=baseline,
                        notes="Initial state",
                    )
                    self.db.update_active_state(run_id, state_id)
                    active_state = self.db.get_state(state_id)

            snapshot = RunSnapshot(
                run_id=run_id,
                iteration=run["iteration"],
                phase=Phase(run["phase"]),
                metadata={"started_at": run["started_at"]},
            )
        else:
            # Create new run
            run_id = self._derive_run_id()
            snapshot = RunSnapshot(
                run_id=run_id,
                iteration=0,
                phase=Phase.PLAN,
                metadata={
                    "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "consecutive_replans": 0,
                },
            )

            # Insert run
            self.db.insert_run(snapshot)

            # Create initial state
            state_id = f"{run_id}-plan-ready"
            baseline = None
            if self.git.is_git_repo():
                try:
                    baseline = self.git.get_current_commit()
                except GitError:
                    pass

            self.db.insert_state(
                state_id=state_id,
                run_id=run_id,
                phase_status="plan-ready",
                baseline_commit=baseline,
                notes="Initial state",
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
                "phase": current_phase.value if current_phase else "blocked",
                "phase_status": phase_status,
                "next_action": "blocked",
                "message": "Run is blocked, requires intervention",
            }

        # ──── Execute Phase ────
        snapshot.iteration += 1
        snapshot.phase = current_phase
        phase_start_time = dt.datetime.now(dt.timezone.utc)

        self.console.rule(f"[bold cyan]{current_phase.value.upper()}[/] (Iteration {snapshot.iteration})")

        # Compose request (include feedback if provided)
        request = self._compose_request(current_phase, snapshot)
        if feedback and current_phase == Phase.PLAN:
            # Inject user feedback into plan request
            request.prompt += f"\n\n──── User Feedback ────\n{feedback}\n"
            request.context["user_feedback"] = feedback

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
                notes=f"Phase {current_phase.value} failed: {exc}",
            )
            self.db.update_active_state(run_id, new_state_id)

            return {
                "run_id": run_id,
                "state_id": new_state_id,
                "phase": current_phase.value,
                "phase_status": "blocked",
                "next_action": "blocked",
                "message": f"Phase failed: {exc}",
            }

        # Extract verdict if REVIEW phase
        if current_phase == Phase.REVIEW and "verdict" in response.metadata:
            verdict_str = response.metadata["verdict"]
            if isinstance(verdict_str, str):
                verdict_lower = verdict_str.lower().strip()
                if verdict_lower in ("approve", "approved"):
                    response.verdict = ReviewVerdict.APPROVE
                elif verdict_lower in ("changes_requested", "changes requested", "revise"):
                    response.verdict = ReviewVerdict.CHANGES_REQUESTED
                elif verdict_lower in ("blocked", "block"):
                    response.verdict = ReviewVerdict.BLOCKED

        # Check for empty response
        if not response.content or not response.content.strip():
            self.console.log("[yellow]Empty response detected[/]")
            new_state_id = f"{run_id}-blocked"
            self.db.insert_state(
                state_id=new_state_id,
                run_id=run_id,
                phase_status="blocked",
                parent_state_id=active_state["state_id"],
                notes=f"Empty response in {current_phase.value}",
            )
            self.db.update_active_state(run_id, new_state_id)

            return {
                "run_id": run_id,
                "state_id": new_state_id,
                "phase": current_phase.value,
                "phase_status": "blocked",
                "next_action": "blocked",
                "message": "Empty response received",
            }

        # ──── Decide Next Phase ────
        decision = self._decide_next_phase(
            current_phase,
            response,
            snapshot.iteration,
            snapshot.metadata.get("consecutive_replans", 0)
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
        if self.git.is_git_repo():
            try:
                baseline_info = self.git.create_state_baseline(new_state_id)
                baseline = baseline_info["commit"]
            except GitError as exc:
                self.console.log(f"[yellow]Git baseline creation failed: {exc}[/]")

        self.db.insert_state(
            state_id=new_state_id,
            run_id=run_id,
            phase_status=new_phase_status,
            baseline_commit=baseline,
            parent_state_id=active_state["state_id"],
            notes=decision.rationale,
            verdict=response.verdict.value if response.verdict else None,
            feedback=feedback,
        )
        self.db.update_active_state(run_id, new_state_id)

        # Update run record
        snapshot.phase = decision.next_phase
        snapshot.iteration = snapshot.iteration
        self.db.update_run(snapshot)

        # ──── Determine Next Action ────
        next_action = "continue"
        if decision.next_phase == Phase.DONE:
            next_action = "done"
        elif decision.next_phase == Phase.BLOCKED or decision.requires_human:
            next_action = "blocked"

        message = f"Phase {current_phase.value} complete. {decision.rationale}"

        self.console.log(f"[green]State created:[/] {new_state_id}")
        self.console.log(f"[blue]{message}[/]")

        return {
            "run_id": run_id,
            "state_id": new_state_id,
            "phase": current_phase.value,
            "phase_status": new_phase_status,
            "next_action": next_action,
            "message": message,
        }

    def _parse_phase_status(self, phase_status: str) -> tuple[Optional[Phase], str]:
        """
        Parse phase status to determine phase and action.

        Returns:
            Tuple of (Phase, action) where action is 'execute', 'done', or 'blocked'
        """
        if phase_status == "done":
            return None, "done"
        if phase_status == "blocked":
            return None, "blocked"

        # Parse format: <phase>-ready or <phase>-complete
        parts = phase_status.split("-")
        if len(parts) != 2:
            return None, "blocked"

        phase_name, status = parts
        if phase_name == "plan":
            phase = Phase.PLAN
        elif phase_name == "implement":
            phase = Phase.IMPLEMENT
        elif phase_name == "review":
            phase = Phase.REVIEW
        else:
            return None, "blocked"

        if status == "ready":
            return phase, "execute"
        elif status == "complete":
            # Phase completed, need to transition to next
            if phase == Phase.PLAN:
                return Phase.IMPLEMENT, "execute"
            elif phase == Phase.IMPLEMENT:
                return Phase.REVIEW, "execute"
            elif phase == Phase.REVIEW:
                # Review complete typically means done
                return None, "done"

        return None, "blocked"

    def _derive_phase_status(self, executed_phase: Phase, next_phase: Phase) -> str:
        """
        Derive phase status string from executed phase and next phase.

        Args:
            executed_phase: Phase that was just executed
            next_phase: Phase determined by decision logic

        Returns:
            Phase status string (e.g., 'plan-complete', 'implement-ready')
        """
        if next_phase == Phase.DONE:
            return "done"
        if next_phase == Phase.BLOCKED:
            return "blocked"

        # Map next phase to status
        status_map = {
            Phase.PLAN: "plan-ready",
            Phase.IMPLEMENT: "implement-ready",
            Phase.REVIEW: "review-ready",
        }
        return status_map.get(next_phase, "blocked")

    def _render_summary(self, snapshot: RunSnapshot) -> None:
        table = Table(title="Duet Run Summary")
        table.add_column("Field", style="bold")
        table.add_column("Value")
        table.add_row("Run ID", snapshot.run_id)
        table.add_row("Phase", snapshot.phase.value)
        table.add_row("Iteration", str(snapshot.iteration))
        table.add_row("Notes", snapshot.notes or "")
        table.add_row("Started", snapshot.metadata.get("started_at", ""))
        table.add_row("Completed", snapshot.metadata.get("completed_at", ""))
        self.console.print(table)
