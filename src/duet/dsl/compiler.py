"""
Compiler for converting facet programs into scheduler registrations.

Takes FacetProgram (combinator output) and produces List[FacetRegistration]
for scheduler consumption.
"""

from __future__ import annotations

from typing import List

from ..scheduler import ExecutionPolicy, FacetRegistration
from .combinators import FacetHandle, FacetProgram, RunPolicy


def compile_program(program: FacetProgram) -> List[FacetRegistration]:
    """
    Compile FacetProgram into scheduler registrations.

    Converts high-level combinator structures (FacetHandle) into
    low-level scheduler registrations (FacetRegistration).

    Args:
        program: FacetProgram from combinators (seq, loop, etc.)

    Returns:
        List of FacetRegistration objects for scheduler

    Raises:
        ValueError: If program validation fails

    Example:
        program = seq(
            facet("plan").needs(TaskRequest).emit(PlanDoc).build(),
            facet("implement").needs(PlanDoc).emit(CodeArtifact).build()
        )
        registrations = compile_program(program)
        for reg in registrations:
            scheduler.register(reg)
    """
    # Validate program before compiling
    errors = program.validate()
    if errors:
        error_msg = "Program validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    registrations = []

    for handle in program.handles:
        # Convert FacetHandle to FacetRegistration
        registration = compile_handle(handle)
        registrations.append(registration)

    return registrations


def compile_handle(handle: FacetHandle) -> FacetRegistration:
    """
    Compile a single FacetHandle into FacetRegistration.

    Converts combinator policy (RunPolicy) to scheduler policy (ExecutionPolicy).

    Args:
        handle: FacetHandle from combinator

    Returns:
        FacetRegistration for scheduler
    """
    # Convert RunPolicy to ExecutionPolicy
    policy_map = {
        RunPolicy.RUN_ONCE: ExecutionPolicy.RUN_ONCE,
        RunPolicy.LOOP_UNTIL: ExecutionPolicy.LOOP_UNTIL,
        RunPolicy.ON_FACT: ExecutionPolicy.ON_FACT,
    }

    scheduler_policy = policy_map.get(handle.policy, ExecutionPolicy.RUN_ONCE)

    # Convert FacetDefinition to Phase
    phase = handle.definition.to_phase()

    # Create registration
    registration = FacetRegistration(
        facet_id=handle.definition.name,
        phase=phase,
        trigger_patterns=handle.triggers,
        policy=scheduler_policy,
        guard=handle.guard,
        metadata=handle.metadata.copy()
    )

    return registration


def validate_and_compile(program: FacetProgram) -> List[FacetRegistration]:
    """
    Validate and compile program with diagnostic output.

    Convenience function that provides detailed error messages.

    Args:
        program: FacetProgram to compile

    Returns:
        List of FacetRegistrations

    Raises:
        ValueError: With detailed diagnostics if validation fails
    """
    # Run validation
    errors = program.validate()

    if errors:
        # Build detailed error report
        report = ["Program validation failed:"]
        for i, error in enumerate(errors, 1):
            report.append(f"  {i}. {error}")

        # Add facet summary
        report.append("\nFacet summary:")
        for handle in program.handles:
            facet_def = handle.definition
            report.append(f"  - {facet_def.name}:")
            report.append(f"      needs: {[t.__name__ for t in facet_def.alias_map.values()]}")
            report.append(f"      emits: {[t.__name__ for t in facet_def.emitted_facts]}")
            report.append(f"      triggers: {len(handle.triggers)} patterns")
            report.append(f"      policy: {handle.policy.value}")

        raise ValueError("\n".join(report))

    # Compile
    return compile_program(program)
