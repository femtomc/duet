"""
Reactive facet scheduler (Syndicate-style).

Executes facets based on fact availability, not sequential order.
Facets subscribe to fact patterns and wake when inputs ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from collections import deque

from rich.console import Console

from .dataspace import Dataspace, FactPattern, Handle
from .dsl.workflow import Phase


@dataclass
class FacetSubscription:
    """
    Subscription tracking for a facet.

    Links facet to fact patterns it depends on.
    When matching facts appear, facet becomes ready.
    """

    facet_id: str
    phase: Phase
    input_patterns: List[FactPattern]
    callback: Optional[Callable] = None


class FacetScheduler:
    """
    Event-driven scheduler for reactive facet execution.

    Inspired by Syndicate's turn-based execution model.
    Facets subscribe to fact patterns and execute when inputs available.

    Attributes:
        dataspace: Global fact store
        facets: Registered facets (phase scripts)
        ready_queue: Facets ready to execute (inputs available)
        waiting: Facets waiting for facts
        executing: Currently executing facet (if any)
    """

    def __init__(self, dataspace: Dataspace, console: Optional[Console] = None):
        self.dataspace = dataspace
        self.console = console or Console()

        self.facets: Dict[str, FacetSubscription] = {}  # facet_id -> subscription
        self.ready_queue: deque = deque()  # Facets ready to execute
        self.waiting: Set[str] = set()  # facet_ids waiting for facts
        self.executing: Optional[str] = None

    def register_facet(self, facet_id: str, phase: Phase) -> None:
        """
        Register a facet with the scheduler.

        Subscribes facet to input patterns based on phase.get_reads().

        Args:
            facet_id: Unique facet identifier
            phase: Phase defining facet script
        """
        # Build input patterns from phase reads
        reads = phase.get_reads()
        patterns = [
            FactPattern(fact_type=ChannelFact, constraints={"channel_name": ch.name})
            for ch in reads
        ]

        subscription = FacetSubscription(
            facet_id=facet_id,
            phase=phase,
            input_patterns=patterns,
        )

        self.facets[facet_id] = subscription

        # Check if inputs already available
        if self._check_inputs_ready(subscription):
            self.ready_queue.append(facet_id)
        else:
            self.waiting.add(facet_id)

        # Subscribe to dataspace for future facts
        for pattern in patterns:
            self.dataspace.subscribe(pattern, lambda fact: self._on_fact_asserted(facet_id, fact))

    def _check_inputs_ready(self, subscription: FacetSubscription) -> bool:
        """Check if all input facts available for facet."""
        for pattern in subscription.input_patterns:
            facts = self.dataspace.query(pattern, latest_only=True)
            if not facts:
                return False  # Missing required input
        return True

    def _on_fact_asserted(self, facet_id: str, fact) -> None:
        """
        Callback when fact matching facet's patterns is asserted.

        Moves facet from waiting to ready if all inputs now available.
        """
        if facet_id in self.waiting:
            subscription = self.facets[facet_id]
            if self._check_inputs_ready(subscription):
                self.waiting.remove(facet_id)
                self.ready_queue.append(facet_id)
                self.console.log(f"[dim]Facet {facet_id} ready (inputs available)[/]")

    def has_ready_facets(self) -> bool:
        """Check if any facets are ready to execute."""
        return len(self.ready_queue) > 0

    def next_ready(self) -> Optional[str]:
        """
        Get next ready facet (FIFO).

        Returns:
            facet_id if ready facet available, None otherwise
        """
        if self.ready_queue:
            return self.ready_queue.popleft()
        return None

    def mark_executing(self, facet_id: str) -> None:
        """Mark facet as currently executing."""
        self.executing = facet_id

    def mark_completed(self, facet_id: str) -> None:
        """
        Mark facet execution completed.

        Removes from executing state. Facet can be re-queued if inputs change.
        """
        if self.executing == facet_id:
            self.executing = None

    def mark_waiting(self, facet_id: str) -> None:
        """
        Mark facet as waiting (e.g., after HumanStep pause).

        Facet will wake when approval granted or inputs change.
        """
        self.waiting.add(facet_id)
        if self.executing == facet_id:
            self.executing = None


# Import after class definition to avoid circular import
from .dataspace import ChannelFact
