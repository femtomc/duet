"""
Dataspace model for Syndicate-style fact storage (Sprint DSL-5).

Replaces loose string channels with structured fact types and
subscription-based reactive execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type
from collections import defaultdict


# ──────────────────────────────────────────────────────────────────────────────
# Handle (Like Syndicate's OutboundAssertion)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Handle:
    """
    Handle to an asserted fact (like Syndicate's OutboundAssertion).

    Returned by assert_fact(), used to retract the fact later.
    Enables facets to manage their assertions explicitly.
    """

    fact_id: str
    handle_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __repr__(self) -> str:
        return f"Handle({self.fact_id[:8]}...)"


# ──────────────────────────────────────────────────────────────────────────────
# Fact Registry (Optional - for future serialization/validation)
# ──────────────────────────────────────────────────────────────────────────────


class FactRegistry:
    """
    Registry for user-defined fact types.

    Allows dynamic fact type registration for serialization, validation,
    and introspection. Optional - facts work without registration.
    """

    _registry: Dict[str, Type] = {}

    @classmethod
    def register(cls, name: str, fact_type: Type) -> None:
        """Register a fact type by name."""
        cls._registry[name] = fact_type

    @classmethod
    def get(cls, name: str) -> Optional[Type]:
        """Get a registered fact type by name."""
        return cls._registry.get(name)

    @classmethod
    def all_types(cls) -> Dict[str, Type]:
        """Get all registered fact types."""
        return cls._registry.copy()


def fact(cls: Type) -> Type:
    """
    Decorator for registering user-defined fact types.

    Usage:
        @fact
        @dataclass
        class MyCustomFact(Fact):
            fact_id: str
            my_field: str
            my_value: int

    The decorator:
    1. Registers the fact type in FactRegistry (optional, for tooling)
    2. Returns the class unchanged (no modification)

    Args:
        cls: Class to register as a fact type

    Returns:
        The same class (unmodified)
    """
    FactRegistry.register(cls.__name__, cls)
    return cls


# ──────────────────────────────────────────────────────────────────────────────
# Fact Types (Structured Channel Data)
# ──────────────────────────────────────────────────────────────────────────────


class Fact:
    """
    Base class for structured facts in the dataspace.

    Facts are typed, structured data that replace loose string channel values.
    Each fact has an ID and can be asserted/retracted.

    **Creating Custom Facts:**

    User-defined facts should:
    1. Inherit from Fact
    2. Be decorated with @dataclass
    3. Include a fact_id: str field
    4. Optionally use @fact decorator for registration

    Example:
        from dataclasses import dataclass
        from duet.dataspace import Fact, fact

        @fact
        @dataclass
        class TaskRequest(Fact):
            fact_id: str
            task_description: str
            priority: int
            assigned_to: str

    Facts can then be asserted into the dataspace:
        handle = dataspace.assert_fact(
            TaskRequest(
                fact_id="task_123",
                task_description="Implement feature X",
                priority=1,
                assigned_to="planner"
            )
        )

    Not a dataclass itself to avoid field ordering issues with subclasses.
    """

    def matches(self, pattern: FactPattern) -> bool:
        """Check if this fact matches a pattern."""
        return pattern.matches(self)


@fact
@dataclass
class PlanDoc(Fact):
    """Fact representing an implementation plan."""

    fact_id: str
    task_id: str
    content: str
    iteration: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@fact
@dataclass
class CodeArtifact(Fact):
    """Fact representing code changes/implementation."""

    fact_id: str
    plan_id: str
    summary: str
    files_changed: int = 0
    git_commit: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@fact
@dataclass
class ReviewVerdict(Fact):
    """Fact representing a review decision."""

    fact_id: str
    code_id: str
    verdict: str  # "approve", "changes_requested", "blocked"
    feedback: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@fact
@dataclass
class ApprovalRequest(Fact):
    """Fact representing a request for human approval."""

    fact_id: str
    requester: str
    reason: str
    context: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@fact
@dataclass
class ApprovalGrant(Fact):
    """Fact representing granted approval."""

    fact_id: str
    request_id: str
    approver: str
    notes: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)




# ──────────────────────────────────────────────────────────────────────────────
# Fact Patterns (Subscriptions)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class FactPattern:
    """
    Pattern for matching facts in subscriptions.

    Enables facets to subscribe to specific fact types with constraints.
    """

    fact_type: type
    constraints: Dict[str, Any] = field(default_factory=dict)

    def matches(self, fact: Fact) -> bool:
        """Check if fact matches this pattern."""
        if not isinstance(fact, self.fact_type):
            return False

        # Check constraints
        for key, value in self.constraints.items():
            if not hasattr(fact, key):
                return False
            if getattr(fact, key) != value:
                return False

        return True


# ──────────────────────────────────────────────────────────────────────────────
# Dataspace (Fact Storage & Subscriptions)
# ──────────────────────────────────────────────────────────────────────────────


class Dataspace:
    """
    Dataspace for storing and subscribing to facts.

    Implements Syndicate-style operations:
    - assert_fact(fact) - Add fact to dataspace
    - retract_fact(fact_id) - Remove fact from dataspace
    - subscribe(pattern, callback) - React when matching facts appear
    - query(pattern) - Find all matching facts

    Turn Semantics:
    - Subscriptions can be deferred until end of turn (atomic publication)
    - in_turn() context manager batches fact assertions
    """

    def __init__(self):
        self.facts: Dict[str, Fact] = {}  # fact_id -> Fact
        self.facts_by_type: Dict[type, Set[str]] = defaultdict(set)  # type -> {fact_ids}
        self.subscriptions: List[Tuple[FactPattern, Callable]] = []

        # Turn batching
        self._in_turn: bool = False
        self._pending_notifications: List[Tuple[Callable, Fact]] = []

    def assert_fact(self, fact: Fact) -> Handle:
        """
        Assert a fact into the dataspace (like Syndicate's publish).

        Args:
            fact: Fact to assert

        Returns:
            Handle for later retraction

        Triggers callbacks for matching subscriptions (atomic within turn).
        """
        # Store fact
        self.facts[fact.fact_id] = fact
        self.facts_by_type[type(fact)].add(fact.fact_id)

        # Create handle for retraction
        handle = Handle(fact_id=fact.fact_id)

        # Trigger subscriptions (defer if in turn)
        for pattern, callback in self.subscriptions:
            if pattern.matches(fact):
                if self._in_turn:
                    # Defer until turn end (atomic publication)
                    self._pending_notifications.append((callback, fact))
                else:
                    # Immediate delivery
                    callback(fact)

        return handle

    def retract(self, handle: Handle) -> Optional[Fact]:
        """
        Retract a fact using its handle (like Syndicate).

        Args:
            handle: Handle from assert_fact()

        Returns:
            Retracted fact if found, None otherwise
        """
        return self.retract_fact(handle.fact_id)

    def retract_fact(self, fact_id: str) -> Optional[Fact]:
        """
        Retract a fact by ID.

        Args:
            fact_id: ID of fact to retract

        Returns:
            Retracted fact if found, None otherwise

        Triggers callbacks for subscriptions (retraction events).
        """
        fact = self.facts.pop(fact_id, None)
        if fact:
            self.facts_by_type[type(fact)].discard(fact_id)
            # TODO: Notify subscriptions of retraction
        return fact

    def subscribe(self, pattern: FactPattern, callback: Callable[[Fact], None]) -> None:
        """
        Subscribe to facts matching a pattern.

        Args:
            pattern: Pattern to match
            callback: Function called when matching fact is asserted

        Immediately calls callback for existing matching facts.
        """
        self.subscriptions.append((pattern, callback))

        # Trigger for existing facts
        for fact in self.query(pattern):
            callback(fact)

    def query(self, pattern: FactPattern, latest_only: bool = False) -> List[Fact]:
        """
        Query for facts matching a pattern with optional constraints.

        **Typed Fact Queries:**
            # Query all PlanDoc facts
            plans = dataspace.query(FactPattern(fact_type=PlanDoc))

            # Query with constraints
            task_plans = dataspace.query(
                FactPattern(fact_type=PlanDoc, constraints={"task_id": "123"})
            )

            # Get latest by iteration (if fact has iteration field)
            latest_plan = dataspace.query(
                FactPattern(fact_type=PlanDoc, constraints={"task_id": "123"}),
                latest_only=True
            )

        Args:
            pattern: Pattern to match (fact_type + optional constraints)
            latest_only: If True and facts have iteration field, return only latest

        Returns:
            List of matching facts (optionally filtered to latest by iteration)
        """
        # Optimize by type
        candidates = self.facts_by_type.get(pattern.fact_type, set())
        matches = []

        for fact_id in candidates:
            fact = self.facts.get(fact_id)
            if fact and pattern.matches(fact):
                matches.append(fact)

        # Filter to latest by iteration if requested
        if latest_only and matches and hasattr(matches[0], 'iteration'):
            return [max(matches, key=lambda f: f.iteration)]

        return matches

    def get_fact(self, fact_id: str) -> Optional[Fact]:
        """Get a fact by ID."""
        return self.facts.get(fact_id)

    def check_approval(self, request_id: str) -> Optional[ApprovalGrant]:
        """
        Check if an approval request has been granted.

        Convenience method for approval conversation pattern.

        Args:
            request_id: ID of ApprovalRequest fact

        Returns:
            ApprovalGrant fact if found, None otherwise
        """
        pattern = FactPattern(fact_type=ApprovalGrant, constraints={"request_id": request_id})
        grants = self.query(pattern)
        return grants[0] if grants else None

    def in_turn(self):
        """
        Context manager for turn-based execution (Syndicate-style).

        Defers subscription notifications until turn end for atomic publication.

        Usage:
            with dataspace.in_turn():
                handle1 = dataspace.assert_fact(fact1)
                handle2 = dataspace.assert_fact(fact2)
                # Subscriptions not triggered yet
            # All pending notifications delivered atomically
        """
        return TurnContext(self)

    def _begin_turn(self) -> None:
        """Begin a turn (internal - used by TurnContext)."""
        self._in_turn = True
        self._pending_notifications.clear()

    def _end_turn(self) -> None:
        """End a turn and deliver pending notifications (internal)."""
        self._in_turn = False

        # Deliver all pending notifications atomically
        for callback, fact in self._pending_notifications:
            try:
                callback(fact)
            except Exception as exc:
                # Don't let callback errors break turn delivery
                print(f"Subscription callback error: {exc}")

        self._pending_notifications.clear()

    def clear(self) -> None:
        """Clear all facts and subscriptions."""
        self.facts.clear()
        self.facts_by_type.clear()
        self.subscriptions.clear()
        self._pending_notifications.clear()


class TurnContext:
    """Context manager for turn-based execution."""

    def __init__(self, dataspace: Dataspace):
        self.dataspace = dataspace

    def __enter__(self):
        self.dataspace._begin_turn()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.dataspace._end_turn()
        return False  # Don't suppress exceptions
