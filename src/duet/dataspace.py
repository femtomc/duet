"""
Dataspace model for Syndicate-style fact storage (Sprint DSL-5).

Replaces loose string channels with structured fact types and
subscription-based reactive execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
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
# Fact Types (Structured Channel Data)
# ──────────────────────────────────────────────────────────────────────────────


class Fact:
    """
    Base class for structured facts in the dataspace.

    Facts are typed, structured data that replace loose string channel values.
    Each fact has an ID and can be asserted/retracted.

    Not a dataclass to avoid field ordering issues with subclasses.
    """

    def matches(self, pattern: FactPattern) -> bool:
        """Check if this fact matches a pattern."""
        return pattern.matches(self)


@dataclass
class PlanDoc(Fact):
    """Fact representing an implementation plan."""

    fact_id: str
    task_id: str
    content: str
    iteration: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeArtifact(Fact):
    """Fact representing code changes/implementation."""

    fact_id: str
    plan_id: str
    summary: str
    files_changed: int = 0
    git_commit: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewVerdict(Fact):
    """Fact representing a review decision."""

    fact_id: str
    code_id: str
    verdict: str  # "approve", "changes_requested", "blocked"
    feedback: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalRequest(Fact):
    """Fact representing a request for human approval."""

    fact_id: str
    requester: str
    reason: str
    context: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalGrant(Fact):
    """Fact representing granted approval."""

    fact_id: str
    request_id: str
    approver: str
    notes: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelFact(Fact):
    """
    Fact representing a channel value (migration from string channels).

    Wraps channel values as structured facts in the dataspace.
    As we move to fully typed facts (PlanDoc, etc.), these will be replaced.

    Attributes:
        fact_id: Unique fact ID
        channel_name: Name of channel this fact belongs to
        value: Channel value (any type - string, dict, etc.)
        iteration: Iteration when value was written
        phase: Phase that wrote this value
    """

    fact_id: str
    channel_name: str
    value: Any
    iteration: int = 0
    phase: Optional[str] = None
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
    """

    def __init__(self):
        self.facts: Dict[str, Fact] = {}  # fact_id -> Fact
        self.facts_by_type: Dict[type, Set[str]] = defaultdict(set)  # type -> {fact_ids}
        self.subscriptions: List[Tuple[FactPattern, Callable]] = []

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

        # Trigger subscriptions (TODO: delay until end of turn)
        for pattern, callback in self.subscriptions:
            if pattern.matches(fact):
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

    def query(self, pattern: FactPattern) -> List[Fact]:
        """
        Query for facts matching a pattern.

        Args:
            pattern: Pattern to match

        Returns:
            List of matching facts
        """
        # Optimize by type
        candidates = self.facts_by_type.get(pattern.fact_type, set())
        matches = []

        for fact_id in candidates:
            fact = self.facts.get(fact_id)
            if fact and pattern.matches(fact):
                matches.append(fact)

        return matches

    def get_fact(self, fact_id: str) -> Optional[Fact]:
        """Get a fact by ID."""
        return self.facts.get(fact_id)

    def clear(self) -> None:
        """Clear all facts and subscriptions."""
        self.facts.clear()
        self.facts_by_type.clear()
        self.subscriptions.clear()
