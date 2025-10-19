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
        self.approval_requests: Dict[str, str] = {}  # facet_id -> request_id mapping

    def register_facet(self, facet_id: str, phase: Phase, input_patterns: Optional[List[FactPattern]] = None) -> None:
        """
        Register a facet with the scheduler.

        Extracts fact dependencies from phase's ReadSteps if not provided.

        Args:
            facet_id: Unique facet identifier
            phase: Phase defining facet script
            input_patterns: Optional list of fact patterns (auto-extracted from ReadSteps if None)
        """
        # Auto-extract fact dependencies from ReadSteps if not provided
        if input_patterns is None:
            input_patterns = phase.get_fact_reads()

        subscription = FacetSubscription(
            facet_id=facet_id,
            phase=phase,
            input_patterns=input_patterns,
        )

        self.facets[facet_id] = subscription

        # Check if inputs already available
        if self._check_inputs_ready(subscription):
            self.ready_queue.append(facet_id)
        else:
            self.waiting.add(facet_id)

        # Subscribe to dataspace for future facts
        for pattern in input_patterns:
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

    def mark_waiting_for_approval(self, facet_id: str, request_id: str) -> None:
        """
        Mark facet as waiting for approval.

        Subscribes to ApprovalGrant facts matching the request_id.
        When grant appears, facet is moved to ready queue.

        Args:
            facet_id: Facet waiting for approval
            request_id: ID of ApprovalRequest fact
        """
        from .dataspace import ApprovalGrant, FactPattern

        self.waiting.add(facet_id)
        if self.executing == facet_id:
            self.executing = None

        # Track approval request mapping
        self.approval_requests[facet_id] = request_id

        # Subscribe to ApprovalGrant for this request
        pattern = FactPattern(
            fact_type=ApprovalGrant, constraints={"request_id": request_id}
        )

        def on_approval_granted(fact):
            """Callback when approval is granted."""
            if facet_id in self.waiting:
                self.waiting.remove(facet_id)
                self.ready_queue.append(facet_id)
                # Remove from approval tracking
                if facet_id in self.approval_requests:
                    del self.approval_requests[facet_id]
                self.console.log(
                    f"[green]Facet {facet_id} approved! Moving to ready queue[/]"
                )

        self.dataspace.subscribe(pattern, on_approval_granted)

    def check_approvals(self) -> int:
        """
        Check for pending approvals and resume waiting facets.

        Queries dataspace for ApprovalGrant facts and moves corresponding
        waiting facets to the ready queue. This is a force-check for grants
        that may have been asserted before subscriptions were set up.

        Returns:
            Number of facets resumed
        """
        from .dataspace import ApprovalGrant, FactPattern

        resumed = 0

        # Query all approval grants
        grant_pattern = FactPattern(fact_type=ApprovalGrant)
        grants = self.dataspace.query(grant_pattern)

        # Build map of request_id -> grant
        granted_requests = {grant.request_id: grant for grant in grants}

        # Check each waiting facet for matching grants
        for facet_id in list(self.waiting):
            # Check if this facet is waiting for an approval
            request_id = self.approval_requests.get(facet_id)
            if request_id and request_id in granted_requests:
                # Grant found - move to ready queue
                self.waiting.remove(facet_id)
                self.ready_queue.append(facet_id)
                del self.approval_requests[facet_id]
                resumed += 1
                self.console.log(
                    f"[green]Facet {facet_id} approved (request {request_id})! Moving to ready queue[/]"
                )

        return resumed


