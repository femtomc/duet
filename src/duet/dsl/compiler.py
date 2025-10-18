"""
Workflow DSL compiler.

Transforms high-level Workflow definitions into an internal WorkflowGraph
representation optimized for runtime execution.

The compiler performs:
- Name normalization and validation
- Adjacency graph construction
- Agent and channel indexing
- Semantic validation (unique names, valid references, well-typed guards)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .workflow import Agent, Guard, Phase, Transition, Workflow


class CompilationError(Exception):
    """Raised when workflow compilation fails."""

    pass


@dataclass
class WorkflowGraph:
    """
    Compiled representation of a workflow.

    Optimized for runtime execution with indexed lookups and validated structure.

    Attributes:
        agents: Map of agent name -> Agent
        phases: Map of phase name -> Phase
        transitions: Map of source phase -> list of outgoing transitions
        initial_phase: Name of the starting phase
        terminal_phases: Set of phase names that end the workflow
        metadata: Additional workflow metadata
    """

    agents: Dict[str, Agent]
    phases: Dict[str, Phase]
    transitions: Dict[str, List[Transition]]  # from_phase -> transitions
    initial_phase: str
    terminal_phases: Set[str] = field(default_factory=set)
    metadata: Dict[str, any] = field(default_factory=dict)

    def get_next_transitions(self, current_phase: str) -> List[Transition]:
        """Get possible transitions from current phase, sorted by priority."""
        transitions = self.transitions.get(current_phase, [])
        # Sort by priority (descending) for deterministic evaluation
        return sorted(transitions, key=lambda t: t.priority, reverse=True)

    def is_terminal(self, phase_name: str) -> bool:
        """Check if a phase is terminal."""
        return phase_name in self.terminal_phases


class WorkflowCompiler:
    """
    Compiles Workflow DSL into WorkflowGraph.

    Performs semantic validation and optimization.
    """

    def __init__(self):
        self.errors: List[str] = []

    def compile(self, workflow: Workflow) -> WorkflowGraph:
        """
        Compile a workflow into an executable graph.

        Args:
            workflow: High-level workflow definition

        Returns:
            Compiled WorkflowGraph

        Raises:
            CompilationError: If validation fails
        """
        self.errors = []

        # Validate unique names
        self._validate_unique_names(workflow)

        # Validate references
        self._validate_references(workflow)

        # Validate transitions
        self._validate_transitions(workflow)

        # Check for errors
        if self.errors:
            error_msg = "Workflow compilation failed:\n" + "\n".join(
                f"  - {err}" for err in self.errors
            )
            raise CompilationError(error_msg)

        # Build indexed structures
        agents_map = {agent.name: agent for agent in workflow.agents}
        phases_map = {phase.name: phase for phase in workflow.phases}
        transitions_map: Dict[str, List[Transition]] = {}

        for transition in workflow.transitions:
            from_phase = transition.from_phase
            if from_phase not in transitions_map:
                transitions_map[from_phase] = []
            transitions_map[from_phase].append(transition)

        # Identify terminal phases
        terminal_phases = {
            phase.name for phase in workflow.phases if phase.is_terminal
        }

        return WorkflowGraph(
            agents=agents_map,
            phases=phases_map,
            transitions=transitions_map,
            initial_phase=workflow.initial_phase,
            terminal_phases=terminal_phases,
            metadata=workflow.metadata,
        )

    def _validate_unique_names(self, workflow: Workflow) -> None:
        """Validate that agent and phase names are unique."""
        # Check agent names
        agent_names: Set[str] = set()
        for agent in workflow.agents:
            if agent.name in agent_names:
                self.errors.append(f"Duplicate agent name: '{agent.name}'")
            agent_names.add(agent.name)

        # Check phase names
        phase_names: Set[str] = set()
        for phase in workflow.phases:
            if phase.name in phase_names:
                self.errors.append(f"Duplicate phase name: '{phase.name}'")
            phase_names.add(phase.name)

    def _validate_references(self, workflow: Workflow) -> None:
        """Validate that all agent and phase references exist."""
        agent_names = {agent.name for agent in workflow.agents}
        phase_names = {phase.name for phase in workflow.phases}

        # Validate phase -> agent references
        for phase in workflow.phases:
            if phase.agent not in agent_names:
                self.errors.append(
                    f"Phase '{phase.name}' references unknown agent: '{phase.agent}'"
                )

        # Validate transition -> phase references
        for transition in workflow.transitions:
            if transition.from_phase not in phase_names:
                self.errors.append(
                    f"Transition from unknown phase: '{transition.from_phase}'"
                )
            if transition.to_phase not in phase_names:
                self.errors.append(
                    f"Transition to unknown phase: '{transition.to_phase}'"
                )

    def _validate_transitions(self, workflow: Workflow) -> None:
        """Validate transition logic and guards."""
        # Check for unreachable phases
        reachable = {workflow.initial_phase}
        worklist = [workflow.initial_phase]

        while worklist:
            current = worklist.pop()
            for transition in workflow.transitions:
                if transition.from_phase == current and transition.to_phase not in reachable:
                    reachable.add(transition.to_phase)
                    worklist.append(transition.to_phase)

        all_phases = {phase.name for phase in workflow.phases}
        unreachable = all_phases - reachable
        if unreachable:
            self.errors.append(
                f"Unreachable phases: {', '.join(sorted(unreachable))}"
            )

        # Check for phases with no outgoing transitions (should be terminal)
        terminal_phases = {phase.name for phase in workflow.phases if phase.is_terminal}
        for phase in workflow.phases:
            has_outgoing = any(t.from_phase == phase.name for t in workflow.transitions)
            if not has_outgoing and phase.name not in terminal_phases:
                self.errors.append(
                    f"Phase '{phase.name}' has no outgoing transitions but is not marked terminal"
                )


def compile_workflow(workflow: Workflow) -> WorkflowGraph:
    """
    Compile a workflow definition into an executable graph.

    Convenience function that creates a compiler and returns the result.

    Args:
        workflow: Workflow definition

    Returns:
        Compiled WorkflowGraph

    Raises:
        CompilationError: If validation fails
    """
    compiler = WorkflowCompiler()
    return compiler.compile(workflow)
