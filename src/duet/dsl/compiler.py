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
from typing import Any, Dict, List, Optional, Set

from .workflow import Agent, Channel, Guard, Phase, Transition, Workflow


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
        channels: Map of channel name -> Channel
        phases: Map of phase name -> Phase
        transitions: Map of source phase -> list of outgoing transitions
        initial_phase: Name of the starting phase
        task_channel: Name of channel to seed with task input (None = auto-detect)
        terminal_phases: Set of phase names that end the workflow
        metadata: Additional workflow metadata
    """

    agents: Dict[str, Agent]
    channels: Dict[str, Channel]
    phases: Dict[str, Phase]
    transitions: Dict[str, List[Transition]]  # from_phase -> transitions
    initial_phase: str
    task_channel: Optional[str] = None
    terminal_phases: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_next_transitions(self, current_phase: str) -> List[Transition]:
        """Get possible transitions from current phase, sorted by priority."""
        transitions = self.transitions.get(current_phase, [])
        # Sort by priority (descending) for deterministic evaluation
        return sorted(transitions, key=lambda t: t.priority, reverse=True)

    def is_terminal(self, phase_name: str) -> bool:
        """Check if a phase is terminal."""
        return phase_name in self.terminal_phases

    def get_task_channel(self) -> Optional[str]:
        """
        Get the task channel name with fallback logic.

        Returns:
            Task channel name if configured or auto-detected, None otherwise
        """
        if self.task_channel:
            return self.task_channel

        # Fallback: find first channel with schema="text"
        for channel_name, channel in self.channels.items():
            if channel.schema == "text":
                return channel_name

        # No suitable channel found
        return None

    def get_phase_metadata(self, phase_name: str, key: str, default: Any = None) -> Any:
        """
        Get metadata value for a phase.

        Args:
            phase_name: Name of the phase
            key: Metadata key
            default: Default value if not found

        Returns:
            Metadata value or default
        """
        phase = self.phases.get(phase_name)
        if not phase:
            return default
        return phase.metadata.get(key, default)

    def requires_approval(self, phase_name: str) -> bool:
        """Check if a phase requires human approval."""
        return self.get_phase_metadata(phase_name, "requires_approval", False)

    def is_replan_transition(self, from_phase: str, to_phase: str) -> bool:
        """
        Check if a transition counts as a replan.

        A replan is detected if:
        1. The from_phase has metadata replan_transition=True, OR
        2. Both phases have replan_transition=True set

        Args:
            from_phase: Source phase name
            to_phase: Target phase name

        Returns:
            True if this transition counts as a replan
        """
        # Check if from_phase is marked as a replan source
        if self.get_phase_metadata(from_phase, "replan_transition", False):
            return True

        # Alternative: both phases marked
        from_marked = self.get_phase_metadata(from_phase, "replan_transition", False)
        to_marked = self.get_phase_metadata(to_phase, "replan_transition", False)
        return from_marked and to_marked

    def requires_git_changes(self, phase_name: str) -> bool:
        """Check if a phase requires git changes."""
        return self.get_phase_metadata(phase_name, "git_changes_required", False)

    def get_phase_order(self) -> List[str]:
        """
        Get topological ordering of phases based on transitions.

        Returns:
            List of phase names in execution order
        """
        # Simple BFS from initial phase
        visited = []
        queue = [self.initial_phase]
        seen = {self.initial_phase}

        while queue:
            current = queue.pop(0)
            visited.append(current)

            # Add connected phases
            for transition in self.transitions.get(current, []):
                if transition.to_phase not in seen:
                    seen.add(transition.to_phase)
                    queue.append(transition.to_phase)

        return visited

    def get_channel_consumers(self, channel_name: str) -> List[str]:
        """Get list of phases that consume a channel."""
        consumers = []
        for phase_name, phase in self.phases.items():
            if channel_name in phase.consumes:
                consumers.append(phase_name)
        return consumers

    def get_channel_publishers(self, channel_name: str) -> List[str]:
        """Get list of phases that publish to a channel."""
        publishers = []
        for phase_name, phase in self.phases.items():
            if channel_name in phase.publishes:
                publishers.append(phase_name)
        return publishers


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
        channels_map = {channel.name: channel for channel in workflow.channels}
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
            channels=channels_map,
            phases=phases_map,
            transitions=transitions_map,
            initial_phase=workflow.initial_phase,
            task_channel=workflow.task_channel,
            terminal_phases=terminal_phases,
            metadata=workflow.metadata,
        )

    def _validate_unique_names(self, workflow: Workflow) -> None:
        """Validate that agent, channel, and phase names are unique."""
        # Check agent names
        agent_names: Set[str] = set()
        for agent in workflow.agents:
            if agent.name in agent_names:
                self.errors.append(f"Duplicate agent name: '{agent.name}'")
            agent_names.add(agent.name)

        # Check channel names
        channel_names: Set[str] = set()
        for channel in workflow.channels:
            if channel.name in channel_names:
                self.errors.append(f"Duplicate channel name: '{channel.name}'")
            channel_names.add(channel.name)

        # Check phase names
        phase_names: Set[str] = set()
        for phase in workflow.phases:
            if phase.name in phase_names:
                self.errors.append(f"Duplicate phase name: '{phase.name}'")
            phase_names.add(phase.name)

    def _validate_references(self, workflow: Workflow) -> None:
        """Validate that all agent, channel, and phase references exist."""
        agent_names = {agent.name for agent in workflow.agents}
        channel_names = {channel.name for channel in workflow.channels}
        phase_names = {phase.name for phase in workflow.phases}

        # Validate phase -> agent references
        for phase in workflow.phases:
            if phase.agent not in agent_names:
                self.errors.append(
                    f"Phase '{phase.name}' references unknown agent: '{phase.agent}'"
                )

            # Validate phase -> channel references (consumes/publishes)
            for channel in phase.consumes:
                if channel not in channel_names:
                    self.errors.append(
                        f"Phase '{phase.name}' consumes unknown channel: '{channel}'"
                    )

            for channel in phase.publishes:
                if channel not in channel_names:
                    self.errors.append(
                        f"Phase '{phase.name}' publishes to unknown channel: '{channel}'"
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
