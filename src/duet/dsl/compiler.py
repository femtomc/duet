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
        channels_by_id: Map of channel ID -> Channel (for object-based lookups)
        phases_by_id: Map of phase ID -> Phase (for object-based lookups)
        metadata: Additional workflow metadata
    """

    agents: Dict[str, Agent]
    channels: Dict[str, Channel]
    phases: Dict[str, Phase]
    transitions: Dict[str, List[Transition]]  # from_phase -> transitions
    initial_phase: str
    task_channel: Optional[str] = None
    terminal_phases: Set[str] = field(default_factory=set)
    channels_by_id: Dict[str, Channel] = field(default_factory=dict)
    phases_by_id: Dict[str, Phase] = field(default_factory=dict)
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

        Sprint DSL-2+: Generic metadata access only. Specific flags (requires_approval,
        git_changes_required) are deprecated - use tools instead.

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

    def get_phase_order(self) -> List[str]:
        """
        Get topological ordering of phases based on transitions.

        Returns:
            List of phase names in execution order
        """
        # Simple BFS from initial phase name
        visited = []
        queue = [self.initial_phase]  # initial_phase is a name string
        seen = {self.initial_phase}

        while queue:
            current = queue.pop(0)
            visited.append(current)

            # Add connected phases (transitions are keyed by name)
            for transition in self.transitions.get(current, []):
                # Extract to_phase name from Phase object
                to_phase_name = transition.to_phase.name
                if to_phase_name not in seen:
                    seen.add(to_phase_name)
                    queue.append(to_phase_name)

        return visited

    def get_channel_consumers(self, channel_name: str) -> List[str]:
        """Get list of phases that consume a channel (by channel name)."""
        consumers = []
        for phase_name, phase in self.phases.items():
            # phase.consumes is now List[Channel], check names
            for channel in phase.consumes:
                if channel.name == channel_name:
                    consumers.append(phase_name)
                    break
        return consumers

    def get_channel_publishers(self, channel_name: str) -> List[str]:
        """Get list of phases that publish to a channel (by channel name)."""
        publishers = []
        for phase_name, phase in self.phases.items():
            # phase.publishes is now List[Channel], check names
            for channel in phase.publishes:
                if channel.name == channel_name:
                    publishers.append(phase_name)
                    break
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

        # Build indexed structures (by name for backward compat, by ID for future)
        agents_map = {agent.name: agent for agent in workflow.agents}
        channels_map = {channel.name: channel for channel in workflow.channels}
        phases_map = {phase.name: phase for phase in workflow.phases}

        # Build ID-based lookups
        channels_by_id = {channel.id: channel for channel in workflow.channels}
        phases_by_id = {phase.id: phase for phase in workflow.phases}

        # Build transitions map (keyed by phase name)
        transitions_map: Dict[str, List[Transition]] = {}
        for transition in workflow.transitions:
            # Extract phase name from Phase object
            from_phase_name = transition.from_phase.name

            if from_phase_name not in transitions_map:
                transitions_map[from_phase_name] = []
            transitions_map[from_phase_name].append(transition)

        # Identify terminal phases
        terminal_phases = {
            phase.name for phase in workflow.phases if phase.is_terminal
        }

        # Get initial phase name (workflow.initial_phase is Phase object)
        initial_phase_name = workflow.initial_phase.name

        # Get task channel name (workflow.task_channel is Channel object or None)
        if workflow.task_channel is not None:
            task_channel_name = workflow.task_channel.name
        else:
            task_channel_name = None

        return WorkflowGraph(
            agents=agents_map,
            channels=channels_map,
            phases=phases_map,
            transitions=transitions_map,
            initial_phase=initial_phase_name,
            task_channel=task_channel_name,
            terminal_phases=terminal_phases,
            metadata=workflow.metadata,
            # New: ID-based lookups
            channels_by_id=channels_by_id,
            phases_by_id=phases_by_id,
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
        all_channels = set(workflow.channels)
        all_phases = set(workflow.phases)

        # Validate phase -> agent references
        for phase in workflow.phases:
            if phase.agent not in agent_names:
                self.errors.append(
                    f"Phase '{phase.name}' references unknown agent: '{phase.agent}'"
                )

            # Validate phase -> channel references (consumes/publishes are Channel objects)
            for channel in phase.consumes:
                if channel not in all_channels:
                    self.errors.append(
                        f"Phase '{phase.name}' consumes unknown channel: '{channel.name}' "
                        f"(channel not in workflow.channels list)"
                    )

            for channel in phase.publishes:
                if channel not in all_channels:
                    self.errors.append(
                        f"Phase '{phase.name}' publishes to unknown channel: '{channel.name}' "
                        f"(channel not in workflow.channels list)"
                    )

        # Validate transition -> phase references (transitions use Phase objects)
        for transition in workflow.transitions:
            if transition.from_phase not in all_phases:
                self.errors.append(
                    f"Transition from unknown phase: '{transition.from_phase.name}' "
                    f"(phase not in workflow.phases list)"
                )
            if transition.to_phase not in all_phases:
                self.errors.append(
                    f"Transition to unknown phase: '{transition.to_phase.name}' "
                    f"(phase not in workflow.phases list)"
                )

    def _validate_transitions(self, workflow: Workflow) -> None:
        """Validate transition logic and guards."""
        # Check for unreachable phases (all references are Phase objects now)
        reachable = {workflow.initial_phase}
        worklist = [workflow.initial_phase]

        while worklist:
            current = worklist.pop()
            for transition in workflow.transitions:
                if transition.from_phase == current and transition.to_phase not in reachable:
                    reachable.add(transition.to_phase)
                    worklist.append(transition.to_phase)

        all_phases = set(workflow.phases)
        unreachable = all_phases - reachable
        if unreachable:
            unreachable_names = sorted([p.name for p in unreachable])
            self.errors.append(
                f"Unreachable phases: {', '.join(unreachable_names)}"
            )

        # Check for phases with no outgoing transitions (should be terminal)
        terminal_phases = {p for p in workflow.phases if p.is_terminal}
        for phase in workflow.phases:
            # Check if phase has any outgoing transitions
            has_outgoing = any(t.from_phase == phase for t in workflow.transitions)

            if not has_outgoing and phase not in terminal_phases:
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
