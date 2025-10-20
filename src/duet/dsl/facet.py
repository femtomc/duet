"""
Facet builder - fluent API for constructing facets.

Provides .needs(), .agent(), .tool(), .emit(), .human() methods for
building typed fact-based workflows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from .steps import AgentStep, HumanStep, ReadStep, ToolStep, WriteStep
from .tools import Tool
from .workflow import Phase


@dataclass
class FacetDefinition:
    """
    Immutable facet definition produced by FacetBuilder.

    Contains:
    - name: Facet identifier
    - steps: Ordered execution steps
    - alias_map: Maps user aliases to fact types
    - emitted_facts: Fact types this facet emits
    - metadata: Additional facet metadata
    """

    name: str
    steps: List = field(default_factory=list)
    alias_map: Dict[str, Type] = field(default_factory=dict)
    emitted_facts: List[Type] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_phase(self) -> Phase:
        """
        Convert to Phase for execution by FacetRunner.

        Returns:
            Phase object with steps
        """
        return Phase(
            name=self.name,
            steps=self.steps,
            metadata=self.metadata
        )

    def validate(self) -> List[str]:
        """
        Validate facet definition.

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []

        # Must have at least one needs or emit (or be an intermediate facet with agent/tool)
        has_needs = any(isinstance(s, ReadStep) for s in self.steps)
        has_emit = any(isinstance(s, WriteStep) for s in self.steps)
        has_agent = any(isinstance(s, AgentStep) for s in self.steps)
        has_tool = any(isinstance(s, ToolStep) for s in self.steps)
        has_human = any(isinstance(s, HumanStep) for s in self.steps)

        if not (has_needs or has_emit or has_agent or has_tool or has_human):
            errors.append(f"Facet '{self.name}' must have at least one step")

        # Check WriteSteps reference valid aliases
        for step in self.steps:
            if isinstance(step, WriteStep):
                if step.values:
                    for key, value in step.values.items():
                        # Check for $alias references (extract just the first part before '.')
                        if isinstance(value, str) and value.startswith("$"):
                            # Extract base alias (before any dots)
                            alias_full = value[1:]
                            alias_base = alias_full.split('.')[0] if '.' in alias_full else alias_full

                            if alias_base not in self.alias_map and alias_base not in ["agent_response"]:
                                errors.append(
                                    f"WriteStep references undefined alias '${alias_base}' "
                                    f"(available: {list(self.alias_map.keys())})"
                                )

        return errors


class FacetBuilder:
    """
    Fluent builder for constructing facets.

    Usage:
        facet = (
            FacetBuilder("planner")
            .needs(TaskRequest, alias="task")
            .agent("planner", prompt="Create implementation plan")
            .emit(PlanDoc, values={"content": "$agent_response", "task_id": "$task.fact_id"})
            .build()
        )

    Methods:
        .needs(fact_type, alias=None, **constraints) - Declare fact dependency
        .agent(name, *, prompt=None, role=None) - Invoke AI agent
        .tool(tool_instance) - Execute deterministic tool
        .emit(fact_type, *, values, fact_id_from=None, store_handle_as=None) - Emit fact
        .human(reason, timeout=None) - Request human approval
        .build() - Produce FacetDefinition
    """

    def __init__(self, name: str, description: Optional[str] = None):
        """
        Initialize facet builder.

        Args:
            name: Facet identifier (must be unique in workflow)
            description: Human-readable description
        """
        self.name = name
        self.description = description
        self._steps: List = []
        self._alias_map: Dict[str, Type] = {}
        self._emitted_facts: List[Type] = []
        self._metadata: Dict[str, Any] = {}

    def needs(
        self,
        fact_type: Type,
        alias: Optional[str] = None,
        **constraints
    ) -> FacetBuilder:
        """
        Declare fact dependency - adds ReadStep.

        The fact will be read from dataspace and stored in context.
        Use alias to reference fact fields in .emit() calls.

        Args:
            fact_type: Fact type to read (e.g., PlanDoc, TaskRequest)
            alias: Context key to store fact (defaults to lowercase type name)
            **constraints: Fact query constraints (e.g., task_id="123")

        Returns:
            Self for chaining

        Example:
            .needs(TaskRequest, alias="task", priority=1)
            # Later: .emit(PlanDoc, values={"task_id": "$task.fact_id"})
        """
        # Determine alias
        if alias is None:
            alias = fact_type.__name__.lower()

        # Add to alias map
        self._alias_map[alias] = fact_type

        # Create ReadStep
        step = ReadStep(
            fact_type=fact_type,
            into=alias,
            constraints=constraints if constraints else None,
            latest_only=True
        )
        self._steps.append(step)

        return self

    def agent(
        self,
        name: str,
        *,
        prompt: Optional[str] = None,
        role: Optional[str] = None
    ) -> FacetBuilder:
        """
        Invoke AI agent - adds AgentStep.

        Agent response stored in context as 'agent_response' for use in .emit().

        Args:
            name: Agent identifier (references config)
            prompt: Optional custom prompt template
            role: Optional role hint for prompt building

        Returns:
            Self for chaining

        Example:
            .agent("planner", prompt="Create a plan for: $task.description")
        """
        step = AgentStep(
            agent=name,
            prompt_template=prompt,
            role=role
        )
        self._steps.append(step)

        return self

    def tool(self, tool_instance: Tool) -> FacetBuilder:
        """
        Execute deterministic tool - adds ToolStep.

        Tool results merged into context for use by subsequent steps.

        Args:
            tool_instance: Tool to execute

        Returns:
            Self for chaining

        Example:
            .tool(GitChangeTool(require_changes=True))
        """
        step = ToolStep(tool=tool_instance, into_context=True)
        self._steps.append(step)

        return self

    def emit(
        self,
        fact_type: Type,
        *,
        values: Optional[Dict[str, Any]] = None,
        fact_id_from: Optional[str] = None,
        store_handle_as: Optional[str] = None
    ) -> FacetBuilder:
        """
        Emit typed fact to dataspace - adds WriteStep.

        Values can reference context using $alias syntax.

        Args:
            fact_type: Fact type to construct and emit
            values: Field values (supports $alias references)
            fact_id_from: Context key containing fact_id (optional)
            store_handle_as: Context key to store handle (optional)

        Returns:
            Self for chaining

        Examples:
            # Simple emit with literal values
            .emit(ReviewVerdict, values={"verdict": "approve", "feedback": "LGTM"})

            # Reference agent response
            .emit(PlanDoc, values={"content": "$agent_response", "task_id": "task_1"})

            # Reference fact field from needs()
            .needs(TaskRequest, alias="task")
            .emit(PlanDoc, values={"task_id": "$task.fact_id"})
        """
        # Track emitted fact type
        if fact_type not in self._emitted_facts:
            self._emitted_facts.append(fact_type)

        step = WriteStep(
            fact_type=fact_type,
            values=values,
            fact_id_key=fact_id_from,
            store_handle_as=store_handle_as
        )
        self._steps.append(step)

        return self

    def human(self, reason: str, timeout: Optional[int] = None) -> FacetBuilder:
        """
        Request human approval - adds HumanStep.

        Facet will pause and assert ApprovalRequest fact.
        Scheduler will resume when ApprovalGrant appears.

        Args:
            reason: Human-readable approval reason
            timeout: Optional timeout in seconds

        Returns:
            Self for chaining

        Example:
            .human("Review code changes before deployment", timeout=3600)
        """
        step = HumanStep(reason=reason, timeout=timeout)
        self._steps.append(step)

        return self

    def with_metadata(self, **metadata) -> FacetBuilder:
        """
        Add metadata to facet definition.

        Args:
            **metadata: Key-value pairs to add

        Returns:
            Self for chaining
        """
        self._metadata.update(metadata)
        return self

    def build(self) -> FacetDefinition:
        """
        Build immutable FacetDefinition.

        Validates facet and returns definition.

        Returns:
            FacetDefinition

        Raises:
            ValueError: If validation fails
        """
        definition = FacetDefinition(
            name=self.name,
            steps=self._steps.copy(),
            alias_map=self._alias_map.copy(),
            emitted_facts=self._emitted_facts.copy(),
            metadata=self._metadata.copy()
        )

        # Validate
        errors = definition.validate()
        if errors:
            error_msg = f"Facet validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ValueError(error_msg)

        return definition


# Convenience factory function
def facet(name: str, description: Optional[str] = None) -> FacetBuilder:
    """
    Create a new facet builder.

    Convenience factory for FacetBuilder.

    Args:
        name: Facet identifier
        description: Optional description

    Returns:
        FacetBuilder instance

    Example:
        plan = (
            facet("planner")
            .needs(TaskRequest)
            .agent("planner")
            .emit(PlanDoc, values={"content": "$agent_response"})
            .build()
        )
    """
    return FacetBuilder(name, description)
