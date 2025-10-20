"""
Facet combinators for building workflows.

Provides seq(), loop(), branch(), once() for composing facets into
reactive workflows with trigger patterns and execution policies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type

from ..dataspace import FactPattern
from .facet import FacetDefinition


class RunPolicy(Enum):
    """Execution policy for facet registration."""

    RUN_ONCE = "run_once"  # Execute once when triggers satisfied
    LOOP_UNTIL = "loop_until"  # Re-execute until predicate true
    ON_FACT = "on_fact"  # Execute whenever new matching fact appears
    WAIT_APPROVAL = "wait_approval"  # Wait for human approval


@dataclass
class FacetHandle:
    """
    Handle to a facet with trigger and policy metadata.

    Produced by combinators, consumed by compiler to generate
    scheduler registrations.

    Attributes:
        definition: Facet definition (steps, emits, etc.)
        triggers: Fact patterns that activate this facet
        policy: Execution policy (run_once, loop_until, etc.)
        guard: Optional predicate for conditional execution
        metadata: Additional metadata
    """

    definition: FacetDefinition
    triggers: List[FactPattern] = field(default_factory=list)
    policy: RunPolicy = RunPolicy.RUN_ONCE
    guard: Optional[Callable[[Any], bool]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def with_trigger(self, pattern: FactPattern) -> FacetHandle:
        """Add trigger pattern."""
        self.triggers.append(pattern)
        return self

    def with_policy(self, policy: RunPolicy) -> FacetHandle:
        """Set execution policy."""
        self.policy = policy
        return self

    def with_guard(self, predicate: Callable[[Any], bool]) -> FacetHandle:
        """Set guard predicate."""
        self.guard = predicate
        return self


@dataclass
class FacetProgram:
    """
    Complete workflow program - collection of facet handles.

    This is the top-level object produced by combinators and
    consumed by the compiler/orchestrator.

    Attributes:
        handles: List of facet handles with triggers/policies
        metadata: Program-level metadata
    """

    handles: List[FacetHandle] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> List[str]:
        """
        Validate program structure.

        Checks:
        - No duplicate facet names
        - Facet-level validation errors

        Note: Does NOT validate that all consumed facts are emitted by other facets,
        as facts may be seeded externally (e.g., TaskRequest).

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []

        # Check for duplicate names
        names = [h.definition.name for h in self.handles]
        duplicates = [n for n in names if names.count(n) > 1]
        if duplicates:
            errors.append(f"Duplicate facet names: {set(duplicates)}")

        # Check each facet definition
        for handle in self.handles:
            facet_errors = handle.definition.validate()
            errors.extend(facet_errors)

        return errors


# ──────────────────────────────────────────────────────────────────────────────
# Combinators
# ──────────────────────────────────────────────────────────────────────────────


def seq(*facets: FacetDefinition) -> FacetProgram:
    """
    Sequential pipeline combinator.

    Chains facets so each subsequent facet triggers on the previous
    facet's emitted facts. Auto-wires dependencies and validates fact contracts.

    The first facet triggers on its required facts (from .needs()).
    Each subsequent facet triggers on the previous facet's emissions that
    match what it needs. Raises an error if there's no overlap.

    Args:
        *facets: Facet definitions in execution order

    Returns:
        FacetProgram with linked triggers

    Example:
        workflow = seq(
            facet("plan").needs(TaskRequest).agent("planner").emit(PlanDoc).build(),
            facet("implement").needs(PlanDoc).agent("coder").emit(CodeArtifact).build(),
            facet("review").needs(CodeArtifact).agent("reviewer").emit(ReviewVerdict).build()
        )

    Raises:
        ValueError: If facets can't be chained (missing emissions or mismatched fact types)
    """
    if len(facets) < 2:
        raise ValueError("seq() requires at least 2 facets")

    handles = []

    # First facet - triggers on its required facts (from .needs())
    first_triggers = []
    for fact_type in facets[0].alias_map.values():
        first_triggers.append(FactPattern(fact_type=fact_type))

    first_handle = FacetHandle(
        definition=facets[0],
        triggers=first_triggers,
        policy=RunPolicy.RUN_ONCE
    )
    handles.append(first_handle)

    # Chain subsequent facets
    for i, facet_def in enumerate(facets[1:], start=1):
        prev_facet = facets[i - 1]

        # Auto-wire: current facet triggers on previous facet's emissions
        # that match what this facet needs
        triggers = []
        for emitted_type in prev_facet.emitted_facts:
            # Check if current facet needs this type
            if emitted_type in facet_def.alias_map.values():
                pattern = FactPattern(fact_type=emitted_type)
                triggers.append(pattern)

        if not triggers:
            # No overlap between emitted and needed facts - invalid chain
            if prev_facet.emitted_facts:
                raise ValueError(
                    f"Cannot chain facet '{facet_def.name}' after '{prev_facet.name}': "
                    f"'{prev_facet.name}' emits {[t.__name__ for t in prev_facet.emitted_facts]} "
                    f"but '{facet_def.name}' needs {[t.__name__ for t in facet_def.alias_map.values()]}"
                )
            else:
                raise ValueError(
                    f"Cannot chain facet '{facet_def.name}' after '{prev_facet.name}': "
                    f"'{prev_facet.name}' emits no facts"
                )

        handle = FacetHandle(
            definition=facet_def,
            triggers=triggers,
            policy=RunPolicy.RUN_ONCE
        )
        handles.append(handle)

    return FacetProgram(handles=handles)


def loop(facet_def: FacetDefinition, until: Callable[[Any], bool]) -> FacetHandle:
    """
    Loop combinator - re-execute facet until predicate satisfied.

    Args:
        facet_def: Facet to loop
        until: Predicate function (returns True to stop)

    Returns:
        FacetHandle with LOOP_UNTIL policy

    Example:
        # Loop test execution until all pass
        loop(
            facet("test").needs(CodeArtifact).agent("tester").emit(TestResult).build(),
            until=lambda result: result.verdict == "all_pass"
        )
    """
    # Extract trigger patterns from facet's needs
    triggers = []
    for fact_type in facet_def.alias_map.values():
        triggers.append(FactPattern(fact_type=fact_type))

    return FacetHandle(
        definition=facet_def,
        triggers=triggers,
        policy=RunPolicy.LOOP_UNTIL,
        guard=until,
        metadata={"loop_predicate": until}
    )


def branch(
    predicate: Callable[[Any], bool],
    on_true: FacetDefinition,
    on_false: FacetDefinition
) -> FacetProgram:
    """
    Conditional branch combinator.

    Executes one of two facets based on predicate.

    Args:
        predicate: Condition to evaluate
        on_true: Facet to execute if predicate true
        on_false: Facet to execute if predicate false

    Returns:
        FacetProgram with conditional guards

    Example:
        branch(
            predicate=lambda verdict: verdict.verdict == "approve",
            on_true=facet("deploy").needs(CodeArtifact).build(),
            on_false=facet("replan").needs(ReviewVerdict).build()
        )

    Note: Implementation simplified - full conditional logic requires
          runtime evaluation in scheduler/compiler.
    """
    handles = [
        FacetHandle(
            definition=on_true,
            triggers=[],
            policy=RunPolicy.RUN_ONCE,
            guard=predicate,
            metadata={"branch": "true"}
        ),
        FacetHandle(
            definition=on_false,
            triggers=[],
            policy=RunPolicy.RUN_ONCE,
            guard=lambda x: not predicate(x),
            metadata={"branch": "false"}
        )
    ]

    return FacetProgram(handles=handles)


def once(facet_def: FacetDefinition, trigger: Optional[FactPattern] = None) -> FacetHandle:
    """
    One-shot facet combinator.

    Execute facet exactly once when trigger satisfied (or immediately if no trigger).

    Args:
        facet_def: Facet to execute
        trigger: Optional trigger pattern (runs immediately if None)

    Returns:
        FacetHandle with RUN_ONCE policy

    Example:
        # Start workflow with seed task
        once(
            facet("seed").emit(TaskRequest, values={"description": "..."}).build()
        )

        # Or trigger on specific fact
        once(
            facet("cleanup").needs(ReviewVerdict).build(),
            trigger=FactPattern(ReviewVerdict, constraints={"verdict": "approve"})
        )
    """
    triggers = []
    if trigger:
        triggers.append(trigger)
    elif facet_def.alias_map:
        # Auto-extract from needs
        for fact_type in facet_def.alias_map.values():
            triggers.append(FactPattern(fact_type=fact_type))

    return FacetHandle(
        definition=facet_def,
        triggers=triggers,
        policy=RunPolicy.RUN_ONCE
    )


def parallel(*facets: FacetDefinition) -> FacetProgram:
    """
    Parallel execution combinator.

    Execute multiple facets concurrently (as fast as scheduler allows).
    All facets start with same triggers (or immediately).

    Args:
        *facets: Facets to execute in parallel

    Returns:
        FacetProgram with parallel handles

    Example:
        parallel(
            facet("analyze_security").needs(CodeArtifact).build(),
            facet("analyze_perf").needs(CodeArtifact).build(),
            facet("analyze_style").needs(CodeArtifact).build()
        )
    """
    handles = []
    for facet_def in facets:
        # Extract triggers from needs
        triggers = []
        for fact_type in facet_def.alias_map.values():
            triggers.append(FactPattern(fact_type=fact_type))

        handles.append(FacetHandle(
            definition=facet_def,
            triggers=triggers,
            policy=RunPolicy.RUN_ONCE
        ))

    return FacetProgram(handles=handles)


# ──────────────────────────────────────────────────────────────────────────────
# Operator Overloading (Optional Enhancement)
# ──────────────────────────────────────────────────────────────────────────────

# Support for f1 >> f2 syntax (Python's __rshift__)
# Can be added to FacetDefinition if desired:
#
# def __rshift__(self, other: FacetDefinition) -> FacetProgram:
#     """Chain facets with >> operator."""
#     return seq(self, other)
