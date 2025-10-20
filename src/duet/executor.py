"""
Guard evaluation for typed fact-based transitions.

Evaluates transition guards by querying the dataspace for facts.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional

from rich.console import Console

from .dsl.compiler import WorkflowGraph
from .dsl.workflow import Guard, Transition


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

    Queries dataspace for facts to evaluate guard predicates.
    """

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()

    def evaluate_transitions(
        self,
        current_phase: str,
        workflow_graph: WorkflowGraph,
        dataspace,
    ) -> GuardEvaluationResult:
        """
        Evaluate transitions from current phase, return first match.

        Transitions are evaluated in priority order (highest first).
        First guard that passes determines the next phase.

        Args:
            current_phase: Name of current phase
            workflow_graph: Compiled workflow graph
            dataspace: Dataspace for fact-based guard evaluation (required)

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
            # Extract names from Phase objects for logging
            from_name = transition.from_phase.name
            to_name = transition.to_phase.name
            guard_desc = f"{from_name} → {to_name} (priority={transition.priority})"

            try:
                result = transition.when.evaluate(dataspace)
                guard_results[guard_desc] = result

                if result:
                    # First passing guard wins
                    self.console.log(
                        f"[dim]Guard passed:[/] {guard_desc} [{transition.when}]"
                    )
                    return GuardEvaluationResult(
                        transition=transition,
                        next_phase=transition.to_phase.name,  # Extract name from Phase object
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


class WorkflowExecutor:
    """
    Workflow guard evaluator (legacy wrapper).

    Maintains guard_evaluator for transition evaluation.
    All execution logic removed - use FacetRunner + Scheduler.
    """

    def __init__(
        self,
        workflow_graph: WorkflowGraph,
        console: Optional[Console] = None,
    ):
        self.workflow_graph = workflow_graph
        self.console = console or Console()
        self.guard_evaluator = GuardEvaluator(console=self.console)
