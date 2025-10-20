"""
Reactive facet scheduler (Syndicate-style).

Executes facets based on fact availability, not sequential order.
Facets subscribe to fact patterns and wake when inputs ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from collections import deque

from rich.console import Console

from .dataspace import Dataspace, FactPattern, Handle
from .dsl.workflow import Phase


class ExecutionPolicy(Enum):
    """Execution policy for facet scheduling."""

    RUN_ONCE = "run_once"  # Execute once when triggers satisfied, then complete
    LOOP_UNTIL = "loop_until"  # Re-execute until guard predicate true
    ON_FACT = "on_fact"  # Execute whenever new matching fact appears
    WAIT_APPROVAL = "wait_approval"  # Wait for human approval (already supported)


@dataclass
class FacetRegistration:
    """
    Registration for a facet with the scheduler.

    Combines facet definition, trigger patterns, and execution policy.
    Produced by the compiler from FacetProgram.

    Attributes:
        facet_id: Unique facet identifier
        phase: Executable phase (from FacetDefinition.to_phase())
        trigger_patterns: Fact patterns that activate this facet
        policy: Execution policy (RUN_ONCE, LOOP_UNTIL, etc.)
        guard: Optional predicate for conditional execution/looping
        metadata: Additional registration metadata
        completed: Whether facet has completed (for RUN_ONCE)
    """

    facet_id: str
    phase: Phase
    trigger_patterns: List[FactPattern]
    policy: ExecutionPolicy = ExecutionPolicy.RUN_ONCE
    guard: Optional[Callable[[Any], bool]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    completed: bool = False

    def should_execute(self, facts: Optional[List[Any]] = None) -> bool:
        """
        Check if facet should execute based on policy and guard.

        Args:
            facts: Optional facts to evaluate guard against

        Returns:
            True if facet should execute
        """
        # If already completed and RUN_ONCE, don't execute
        if self.policy == ExecutionPolicy.RUN_ONCE and self.completed:
            return False

        # If guard exists, evaluate it
        if self.guard and facts:
            # For LOOP_UNTIL, execute if guard is False (loop until True)
            if self.policy == ExecutionPolicy.LOOP_UNTIL:
                # Guard should return True to stop looping
                # So execute if guard returns False
                return not all(self.guard(f) for f in facts)

            # For other policies, execute if guard is True
            return any(self.guard(f) for f in facts)

        return True


@dataclass
class FacetSubscription:
    """
    Subscription tracking for a facet (legacy, for backward compatibility).

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

        self.facets: Dict[str, FacetSubscription] = {}  # facet_id -> subscription (legacy)
        self.registrations: Dict[str, FacetRegistration] = {}  # facet_id -> registration (new)
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

    def register(self, registration: FacetRegistration) -> None:
        """
        Register a facet using FacetRegistration (new API).

        Supports execution policies and guards for advanced scheduling.

        Args:
            registration: FacetRegistration with policy and triggers
        """
        self.registrations[registration.facet_id] = registration

        # Check if triggers already satisfied
        if self._check_triggers_ready(registration):
            # Query trigger facts to pass to guard
            trigger_facts = []
            for pattern in registration.trigger_patterns:
                trigger_facts.extend(self.dataspace.query(pattern, latest_only=True))

            # Additional check: should_execute based on policy/guard
            if registration.should_execute(trigger_facts if trigger_facts else None):
                self.ready_queue.append(registration.facet_id)
            else:
                self.waiting.add(registration.facet_id)
        else:
            self.waiting.add(registration.facet_id)

        # Subscribe to dataspace for future facts
        for pattern in registration.trigger_patterns:
            # Capture registration in closure
            reg = registration
            self.dataspace.subscribe(
                pattern,
                lambda fact, r=reg: self._on_fact_asserted_new(r.facet_id, fact)
            )

    def _check_triggers_ready(self, registration: FacetRegistration) -> bool:
        """Check if all trigger patterns are satisfied."""
        if not registration.trigger_patterns:
            # No triggers - always ready
            return True

        for pattern in registration.trigger_patterns:
            facts = self.dataspace.query(pattern, latest_only=True)
            if not facts:
                return False  # Missing required fact
        return True

    def _on_fact_asserted_new(self, facet_id: str, fact) -> None:
        """
        Callback for new registration system.

        Checks policy and guard before queuing.
        """
        if facet_id not in self.registrations:
            return

        registration = self.registrations[facet_id]

        # Skip if already completed (RUN_ONCE)
        if registration.completed:
            return

        if facet_id in self.waiting:
            if self._check_triggers_ready(registration):
                # Check guard/policy
                trigger_facts = []
                for pattern in registration.trigger_patterns:
                    trigger_facts.extend(self.dataspace.query(pattern, latest_only=True))

                if registration.should_execute(trigger_facts):
                    self.waiting.remove(facet_id)
                    self.ready_queue.append(facet_id)
                    self.console.log(f"[dim]Facet {facet_id} ready (triggers satisfied)[/]")

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

        Removes from executing state. For RUN_ONCE policy, marks as completed
        to prevent re-execution.
        """
        if self.executing == facet_id:
            self.executing = None

        # Mark registration as completed if RUN_ONCE
        if facet_id in self.registrations:
            registration = self.registrations[facet_id]
            if registration.policy == ExecutionPolicy.RUN_ONCE:
                registration.completed = True

    def get_phase(self, facet_id: str) -> Optional[Phase]:
        """
        Get Phase for a facet (works with both old and new registration).

        Args:
            facet_id: Facet identifier

        Returns:
            Phase if found, None otherwise
        """
        # Try new registration first
        if facet_id in self.registrations:
            return self.registrations[facet_id].phase

        # Fall back to old subscription
        if facet_id in self.facets:
            return self.facets[facet_id].phase

        return None

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


