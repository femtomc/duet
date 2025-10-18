"""
Workflow executor for graph-driven orchestration.

Executes workflows defined in the DSL with channel-based message passing
and guard-controlled transitions.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional

from rich.console import Console

from .channels import ChannelStore
from .dsl.compiler import WorkflowGraph
from .dsl.workflow import Guard, Transition
from .models import AssistantResponse, ReviewVerdict
from .prompt_builder import PromptContext, get_builder


@dataclass
class GuardEvaluationResult:
    """
    Result of evaluating transitions for a phase.

    Attributes:
        transition: The transition that passed (None if no guards passed)
        next_phase: Name of next phase (None if blocked)
        rationale: Human-readable explanation
        guard_results: Map of transition -> guard result for diagnostics
    """

    transition: Optional[Transition]
    next_phase: Optional[str]
    rationale: str
    guard_results: Dict[str, bool]  # transition description -> result


class GuardEvaluator:
    """
    Evaluates guards for transition decisions.

    Builds guard context from channel payloads, response metadata, and
    runtime state, then evaluates transitions in priority order.
    """

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()

    def build_guard_context(
        self,
        channel_store: ChannelStore,
        response: Optional[AssistantResponse] = None,
        git_changes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build context dictionary for guard evaluation.

        Context includes:
        - All channel payloads (by name)
        - Response metadata (verdict, etc.)
        - Git change information
        - Additional runtime data

        Args:
            channel_store: Current channel state
            response: Assistant response (if available)
            git_changes: Git change metadata

        Returns:
            Context dictionary for guard evaluation
        """
        context = dict(channel_store.get_all())

        # Add verdict from response if available
        if response:
            if response.verdict:
                context["verdict"] = response.verdict.value
            elif "verdict" in response.metadata:
                context["verdict"] = response.metadata["verdict"]

        # Add git changes
        if git_changes:
            context["git_changes"] = git_changes

        return context

    def evaluate_transitions(
        self,
        current_phase: str,
        workflow_graph: WorkflowGraph,
        guard_context: Dict[str, Any],
    ) -> GuardEvaluationResult:
        """
        Evaluate transitions from current phase, return first match.

        Transitions are evaluated in priority order (highest first).
        First guard that passes determines the next phase.

        Args:
            current_phase: Name of current phase
            workflow_graph: Compiled workflow graph
            guard_context: Context for guard evaluation

        Returns:
            GuardEvaluationResult with chosen transition and rationale
        """
        transitions = workflow_graph.get_next_transitions(current_phase)

        if not transitions:
            return GuardEvaluationResult(
                transition=None,
                next_phase=None,
                rationale=f"No transitions defined from phase '{current_phase}'",
                guard_results={},
            )

        # Evaluate guards in priority order
        guard_results = {}
        for transition in transitions:
            guard_desc = f"{transition.from_phase} → {transition.to_phase} (priority={transition.priority})"

            try:
                result = transition.when.evaluate(guard_context)
                guard_results[guard_desc] = result

                if result:
                    # First passing guard wins
                    self.console.log(
                        f"[dim]Guard passed:[/] {guard_desc} [{transition.when}]"
                    )
                    return GuardEvaluationResult(
                        transition=transition,
                        next_phase=transition.to_phase,
                        rationale=f"Guard passed: {guard_desc}",
                        guard_results=guard_results,
                    )
                else:
                    self.console.log(
                        f"[dim]Guard failed:[/] {guard_desc} [{transition.when}]"
                    )
            except Exception as exc:
                self.console.log(
                    f"[yellow]Guard evaluation error:[/] {guard_desc} - {exc}"
                )
                guard_results[guard_desc] = False

        # No guards passed
        return GuardEvaluationResult(
            transition=None,
            next_phase=None,
            rationale=f"No guards passed for phase '{current_phase}' (evaluated {len(transitions)} transitions)",
            guard_results=guard_results,
        )


@dataclass
class PhaseExecutionResult:
    """
    Result of executing a single phase.

    Attributes:
        phase_name: Name of phase executed
        response: Assistant response
        next_phase: Next phase determined by guards (None if blocked)
        guard_evaluation: Guard evaluation result
        channel_updates: Channels published by this phase
        error: Error message if execution failed
    """

    phase_name: str
    response: Optional[AssistantResponse]
    next_phase: Optional[str]
    guard_evaluation: GuardEvaluationResult
    channel_updates: Dict[str, Any]
    error: Optional[str] = None


class WorkflowExecutor:
    """
    Executes workflow graphs with channel-based message passing.

    Replaces the hardcoded orchestrator loop with a graph-driven executor
    that uses DSL workflow definitions.

    Attributes:
        workflow_graph: Compiled workflow definition
        channel_store: Channel payload storage
        guard_evaluator: Guard evaluation engine
        console: Rich console for logging
    """

    def __init__(
        self,
        workflow_graph: WorkflowGraph,
        console: Optional[Console] = None,
    ):
        self.workflow_graph = workflow_graph
        self.console = console or Console()
        self.guard_evaluator = GuardEvaluator(console=self.console)

        # Initialize channel store from workflow
        self.channel_store = ChannelStore(channels=workflow_graph.channels)

    def seed_channel(self, channel_name: str, value: Any) -> None:
        """
        Seed a channel with an initial value (e.g., task from CLI).

        Args:
            channel_name: Name of channel to seed
            value: Initial value
        """
        self.channel_store.set(channel_name, value)

    def execute_phase(
        self,
        phase_name: str,
        adapter,  # AssistantAdapter
        context: PromptContext,
        git_changes: Optional[Dict[str, Any]] = None,
    ) -> PhaseExecutionResult:
        """
        Execute a single phase.

        Args:
            phase_name: Name of phase to execute
            adapter: Adapter to invoke for this phase
            context: Prompt context with run metadata
            git_changes: Git change metadata (if available)

        Returns:
            PhaseExecutionResult with response and next phase
        """
        phase_def = self.workflow_graph.phases.get(phase_name)
        if not phase_def:
            return PhaseExecutionResult(
                phase_name=phase_name,
                response=None,
                next_phase=None,
                guard_evaluation=GuardEvaluationResult(
                    transition=None,
                    next_phase=None,
                    rationale=f"Phase '{phase_name}' not found in workflow",
                    guard_results={},
                ),
                channel_updates={},
                error=f"Unknown phase: {phase_name}",
            )

        # Update context with consumed channels
        for channel_name in phase_def.consumes:
            value = self.channel_store.get(channel_name)
            context.channel_payloads[channel_name] = value

        # Build prompt using builder
        try:
            builder = get_builder(phase_name)
            request = builder.build(context)
        except Exception as exc:
            return PhaseExecutionResult(
                phase_name=phase_name,
                response=None,
                next_phase=None,
                guard_evaluation=GuardEvaluationResult(
                    transition=None,
                    next_phase=None,
                    rationale=f"Prompt builder failed: {exc}",
                    guard_results={},
                ),
                channel_updates={},
                error=f"Prompt build error: {exc}",
            )

        # Execute adapter
        try:
            # Use stream() method (adapters don't have execute())
            # Streaming events handled by caller if needed
            response = adapter.stream(request, on_event=lambda e: None)
        except Exception as exc:
            return PhaseExecutionResult(
                phase_name=phase_name,
                response=None,
                next_phase=None,
                guard_evaluation=GuardEvaluationResult(
                    transition=None,
                    next_phase=None,
                    rationale=f"Adapter execution failed: {exc}",
                    guard_results={},
                ),
                channel_updates={},
                error=f"Adapter error: {exc}",
            )

        # Extract channel outputs (published channels)
        channel_updates = self._extract_channel_outputs(
            response, phase_def.publishes
        )

        # Update channel store with published values
        self.channel_store.update(channel_updates)

        # Build guard context
        guard_context = self.guard_evaluator.build_guard_context(
            self.channel_store,
            response=response,
            git_changes=git_changes,
        )

        # Evaluate transitions
        guard_result = self.guard_evaluator.evaluate_transitions(
            current_phase=phase_name,
            workflow_graph=self.workflow_graph,
            guard_context=guard_context,
        )

        return PhaseExecutionResult(
            phase_name=phase_name,
            response=response,
            next_phase=guard_result.next_phase,
            guard_evaluation=guard_result,
            channel_updates=channel_updates,
        )

    def _extract_channel_outputs(
        self,
        response: AssistantResponse,
        publishes: list[str],
    ) -> Dict[str, Any]:
        """
        Extract channel outputs from assistant response.

        Maps response content and metadata to published channels.

        Args:
            response: Assistant response
            publishes: List of channels this phase publishes to

        Returns:
            Dictionary of channel_name -> value
        """
        outputs = {}

        # Map common outputs
        for channel_name in publishes:
            if channel_name == "plan":
                # Plan phase publishes plan text
                outputs["plan"] = response.content

            elif channel_name == "code":
                # Implement phase publishes code summary
                outputs["code"] = response.content

            elif channel_name == "verdict":
                # Review phase publishes verdict
                if response.verdict:
                    outputs["verdict"] = response.verdict.value
                elif "verdict" in response.metadata:
                    outputs["verdict"] = response.metadata["verdict"]
                else:
                    outputs["verdict"] = "unknown"

            elif channel_name == "feedback":
                # Review phase may publish feedback
                # Extract from response content or metadata
                if "feedback" in response.metadata:
                    outputs["feedback"] = response.metadata["feedback"]
                else:
                    # Fallback: use response content if it's feedback
                    outputs["feedback"] = response.content

            else:
                # Generic: use response content
                outputs[channel_name] = response.content

        return outputs

    def get_current_channels(self) -> Dict[str, Any]:
        """Get current channel payloads (for persistence)."""
        return self.channel_store.get_all()

    def restore_channels(self, snapshot: Dict[str, Any]) -> None:
        """Restore channel state from snapshot."""
        self.channel_store.restore(snapshot)
