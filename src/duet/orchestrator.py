"""Core orchestration loop that coordinates Codex and Claude."""

from __future__ import annotations

import datetime as dt
import json
import signal
from typing import Optional

from rich.console import Console
from rich.table import Table

from .adapters import REGISTRY, AssistantAdapter
from .artifacts import ArtifactStore
from .config import DuetConfig
from .logging import DuetLogger
from .models import AssistantRequest, AssistantResponse, Phase, RunSnapshot, TransitionDecision

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

    def _build_adapter(self, assistant_cfg):
        adapter_name = assistant_cfg.provider
        adapter = REGISTRY.resolve(adapter_name, config=assistant_cfg.dict())
        return adapter

    def _setup_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown on manual stop."""

        def _handle_stop(signum, frame):
            self.console.log("[yellow]Manual stop requested (SIGINT/SIGTERM), halting gracefully...[/]")
            self._stop_requested = True

        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGTERM, _handle_stop)

    def run(self, run_id: Optional[str] = None) -> RunSnapshot:
        """Execute orchestration loop until termination."""
        iteration = 0
        snapshot = RunSnapshot(
            run_id=run_id or self._derive_run_id(),
            iteration=iteration,
            phase=Phase.PLAN,
            metadata={"started_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        )
        self.console.rule(f"Starting Duet run {snapshot.run_id}")
        self.artifacts.checkpoint(snapshot)

        current_phase = Phase.PLAN
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
            self.logger.log_iteration_start(snapshot.run_id, iteration, current_phase.value)

            # ──── Compose Request ────
            request = self._compose_request(current_phase, snapshot)
            adapter = self._select_adapter(current_phase)
            adapter_name = adapter.__class__.__name__
            self.logger.log_adapter_call(snapshot.run_id, iteration, current_phase.value, adapter_name)

            # ──── Edge Case: Adapter Failure ────
            try:
                response = adapter.generate(request)
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

            # ──── Edge Case: Empty Response ────
            if not response.content or not response.content.strip():
                snapshot.phase = Phase.BLOCKED
                snapshot.notes = f"Empty response received during {current_phase.value}."
                self.console.log("[yellow]Empty response detected, marking run as BLOCKED[/]")
                self._persist_interaction(snapshot.run_id, current_phase, request, response)
                break

            # ──── Decide Next Phase ────
            decision = self._decide_next_phase(current_phase, response, iteration)
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

            # ──── Edge Case: Human Approval Required ────
            if decision.requires_human and self.config.workflow.require_human_approval:
                snapshot.phase = Phase.BLOCKED
                snapshot.notes = "Awaiting human approval."
                self.console.log("[yellow]Human intervention requested; halting run[/]")
                break

            # ──── Log State Transition ────
            prev_phase = current_phase
            current_phase = decision.next_phase
            self.logger.log_state_transition(
                snapshot.run_id, iteration, prev_phase.value, current_phase.value, decision.rationale
            )
            self.artifacts.checkpoint(snapshot)

        snapshot.metadata["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.artifacts.checkpoint(snapshot)

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
            "Review the latest changes and provide approval status.",
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
                "Set 'concluded' to True if approved, False if changes needed.",
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
        self, phase: Phase, response: AssistantResponse, iteration: int
    ) -> TransitionDecision:
        """
        Determine next phase based on current phase and response.

        Transition Rules:
        - PLAN → IMPLEMENT (always, if valid response)
        - IMPLEMENT → REVIEW (always, if valid response)
        - REVIEW → DONE (if response.concluded == True)
        - REVIEW → PLAN (if response.concluded == False and iterations remain)

        Edge Cases:
        - Approaching max iterations: set requires_human = True
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
            if response.concluded:
                rationale = "Review approved; marking run as DONE."
                return TransitionDecision(next_phase=Phase.DONE, rationale=rationale)

            # Review requested changes - loop back to PLAN
            approaching_limit = iteration >= self.max_iterations - 1
            requires_human = approaching_limit

            if approaching_limit:
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
            "request": request.dict(),
            "response": response.dict(),
        }
        filename = f"{timestamp_safe}-{phase.value}.json"
        self.artifacts.write_json(run_id, f"interactions/{filename}", payload)

    def _derive_run_id(self) -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("run-%Y%m%d-%H%M%S")

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
