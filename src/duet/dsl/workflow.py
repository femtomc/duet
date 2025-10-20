"""
Workflow primitives for facet-based execution.

Core types for the new DSL - no legacy compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .steps import PhaseStep, ReadStep


@dataclass
class Phase:
    """
    Phase definition - an executable facet script.

    A phase is an ordered sequence of steps (read → tool → agent → write).
    Built using FacetBuilder, executed by FacetRunner.

    Attributes:
        name: Unique phase identifier
        steps: Ordered execution steps
        description: Human-readable description
        metadata: Additional metadata
    """

    name: str
    steps: List[PhaseStep] = field(default_factory=list)
    description: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_fact_reads(self) -> List:
        """
        Extract fact patterns from ReadSteps for scheduler dependency tracking.

        Returns:
            List of FactPattern objects
        """
        from ..dataspace import FactPattern

        patterns = []
        for step in self.steps:
            if isinstance(step, ReadStep):
                pattern = FactPattern(
                    fact_type=step.fact_type,
                    constraints=step.constraints or {}
                )
                patterns.append(pattern)
        return patterns
