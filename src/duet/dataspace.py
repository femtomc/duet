"""
Dataspace model for Syndicate-style fact storage (Sprint DSL-5).

Replaces loose string channels with structured fact types and
subscription-based reactive execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, Iterable, Iterator, List, Optional, Set, Tuple, Type, TypeVar, Union, Literal
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
    facet_id: str
    handle_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    relay_handles: List[Tuple["Dataspace", "Handle"]] = field(default_factory=list)

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


# ──────────────────────────────────────────────────────────────────────────────
# Message Types (Ephemeral Actions)
# ──────────────────────────────────────────────────────────────────────────────


class Message:
    """Base class for messages sent through the dataspace."""

    def matches(self, pattern: "MessagePattern") -> bool:
        return pattern.matches(self)


@fact
@dataclass
class TaskRequest(Fact):
    """Fact representing a task request to start a workflow."""

    fact_id: str
    description: str
    priority: int = 0
    assigned_to: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


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

    def key(self) -> Tuple[type, Tuple[Tuple[str, Any], ...]]:
        """Return canonical key representation for trie indexing."""
        return self.fact_type, tuple(sorted(self.constraints.items()))

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
# Fact Events (Subscription Notifications)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FactEvent:
    """
    Event delivered to subscription callbacks.

    Attributes:
        fact: Fact involved in the event
        facet_id: Facet that asserted/retracted the fact
        action: Literal describing the change ("asserted" or "retracted")
    """

    fact: Fact
    facet_id: str
    action: Literal["asserted", "retracted"]


# ──────────────────────────────────────────────────────────────────────────────
# Message Patterns & Events
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class MessagePattern:
    """Pattern for matching messages in subscriptions."""

    message_type: type
    constraints: Dict[str, Any] = field(default_factory=dict)

    def matches(self, message: Message) -> bool:
        if not isinstance(message, self.message_type):
            return False

        for key, value in self.constraints.items():
            if not hasattr(message, key):
                return False
            if getattr(message, key) != value:
                return False

        return True


@dataclass(frozen=True)
class MessageEvent:
    """Event delivered to message subscription callbacks."""

    message: Message
    facet_id: str


@fact
@dataclass
class FactInterest(Fact):
    """Interest assertion indicating a facet's desire for facts matching a pattern."""

    fact_id: str
    facet_id: str
    fact_type: str
    constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InterestRegistration:
    """Runtime record linking an interest to its callback and handle."""

    facet_id: str
    pattern: FactPattern
    callback: Callable[[FactEvent], None]
    handle: Handle
    key: Tuple[Any, ...]


# ──────────────────────────────────────────────────────────────────────────────
# Dataspace (Fact Storage & Subscriptions)
# ──────────────────────────────────────────────────────────────────────────────


T = TypeVar("T")


class PatternTrieNode(Generic[T]):
    __slots__ = ("value", "children")

    def __init__(self) -> None:
        self.value: List[T] = []
        self.children: Dict[Any, PatternTrieNode[T]] = {}


class PatternTrie(Generic[T]):
    def __init__(self) -> None:
        self.root: PatternTrieNode[T] = PatternTrieNode()

    def insert(self, key: Iterable[Any], item: T) -> None:
        node = self.root
        for component in key:
            node = node.children.setdefault(component, PatternTrieNode())
        if item not in node.value:
            node.value.append(item)

    def remove(self, key: Iterable[Any], item: T) -> None:
        path: List[Tuple[Any, PatternTrieNode[T]]] = []
        node = self.root
        for component in key:
            child = node.children.get(component)
            if child is None:
                return
            path.append((component, node))
            node = child
        if item in node.value:
            node.value.remove(item)
        if node.value or node.children:
            return
        for component, parent in reversed(path):
            del parent.children[component]
            if parent.value or parent.children:
                break

    def lookup(self, key: Iterable[Any]) -> List[T]:
        node = self.root
        for component in key:
            child = node.children.get(component)
            if child is None:
                return []
            node = child
        result: List[T] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.value:
                result.extend(current.value)
            if current.children:
                stack.extend(current.children.values())
        return result


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

    def __init__(self, *, name: str = "root", parent: Optional["Dataspace"] = None):
        self.facts: Dict[str, Fact] = {}  # fact_id -> Fact
        self.facts_by_type: Dict[type, Set[str]] = defaultdict(set)  # type -> {fact_ids}
        self.subscriptions: List[Tuple[FactPattern, Callable[[FactEvent], None]]] = []
        self.message_subscriptions: List[Tuple[MessagePattern, Callable[[MessageEvent], None]]] = []
        self._facet_assertions: Dict[str, Dict[str, Fact]] = defaultdict(dict)  # facet_id -> {fact_id: Fact}
        self._fact_index: Dict[str, str] = {}  # fact_id -> facet_id
        self.name = name
        self.parent = parent
        self.children: Dict[str, "Dataspace"] = {}
        self._facet_retractions: Dict[str, List[Handle]] = defaultdict(list)
        self._subscription_trie: PatternTrie[int] = PatternTrie()
        self._interest_trie: PatternTrie["InterestRegistration"] = PatternTrie()
        self._interest_registry: Dict[str, List["InterestRegistration"]] = defaultdict(list)
        self._interest_handle_map: Dict[str, "InterestRegistration"] = {}

        # Turn batching
        self._in_turn: bool = False
        self._pending_notifications: List[Tuple[Callable[[FactEvent], None], FactEvent]] = []

    def spawn_child(self, name: str) -> "Dataspace":
        """Create a child dataspace nested within this dataspace."""
        if name in self.children:
            raise ValueError(f"Child dataspace '{name}' already exists")

        child = Dataspace(name=name, parent=self)
        self.children[name] = child
        return child

    def _build_pattern_path(self, pattern: FactPattern) -> Tuple[Any, ...]:
        components: List[Any] = [pattern.fact_type]
        for key, value in sorted(pattern.constraints.items()):
            components.append((key, value))
        return tuple(components)

    def get_child(self, name: str) -> Optional["Dataspace"]:
        """Retrieve a child dataspace by name."""
        return self.children.get(name)

    def ensure_child(self, name: str) -> "Dataspace":
        """Retrieve child dataspace, creating it if necessary."""
        child = self.get_child(name)
        if child is None:
            child = self.spawn_child(name)
        return child

    def remove_child(self, name: str, *, retract: bool = True) -> None:
        """Remove a child dataspace, optionally retracting its assertions."""
        child = self.children.pop(name, None)
        if child and retract:
            child.clear(retract=True)

    def assert_fact(self, fact: Fact, facet_id: Optional[str] = None, relay: bool = False) -> Handle:
        """
        Assert a fact into the dataspace (like Syndicate's publish).

        Args:
            fact: Fact to assert
            facet_id: Facet asserting the fact (default: "__anon__")

            relay: Relay assertion to parent dataspace if True

        Returns:
            Handle for later retraction

        Triggers callbacks for matching subscriptions (atomic within turn).
        """
        facet = facet_id or "__anon__"

        # Store fact
        self.facts[fact.fact_id] = fact
        self.facts_by_type[type(fact)].add(fact.fact_id)
        self._facet_assertions[facet][fact.fact_id] = fact
        self._fact_index[fact.fact_id] = facet

        # Create handle for retraction
        handle = Handle(fact_id=fact.fact_id, facet_id=facet)
        self._facet_retractions[facet].append(handle)

        event = FactEvent(fact=fact, facet_id=facet, action="asserted")

        # Trigger subscriptions (defer if in turn)
        subscription_indices = self._subscription_trie.lookup([type(fact)])
        if subscription_indices:
            for index in set(subscription_indices):
                if index >= len(self.subscriptions):
                    continue
                patt, callback = self.subscriptions[index]
                if patt.matches(fact):
                    if self._in_turn:
                        self._pending_notifications.append((callback, event))
                    else:
                        callback(event)

        # Trigger interest registrations (scheduler)
        interest_records = self._interest_trie.lookup([type(fact)])
        if interest_records:
            seen_interest_handles: Set[str] = set()
            for record in interest_records:
                handle_id = record.handle.handle_id
                if handle_id in seen_interest_handles:
                    continue
                if record.pattern.matches(fact):
                    if self._in_turn:
                        self._pending_notifications.append((record.callback, event))
                    else:
                        record.callback(event)
                seen_interest_handles.add(handle_id)

        # Relay to parent dataspace if requested
        if relay and self.parent:
            parent_facet = f"{self.name}.{facet}"
            relay_handle = self.parent.assert_fact(fact, facet_id=parent_facet, relay=False)
            handle.relay_handles.append((self.parent, relay_handle))

        return handle

    def retract(self, handle: Handle) -> Optional[Fact]:
        """
        Retract a fact using its handle (like Syndicate).

        Args:
            handle: Handle from assert_fact()

        Returns:
            Retracted fact if found, None otherwise
        """
        fact = self.retract_fact(handle.fact_id)

        # Retract any relayed assertions upstream
        for dataspace, relay_handle in list(handle.relay_handles):
            dataspace.retract(relay_handle)
        handle.relay_handles.clear()

        return fact

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
            facet = self._fact_index.pop(fact_id, "__anon__")
            facet_facts = self._facet_assertions.get(facet)
            if facet_facts and fact_id in facet_facts:
                del facet_facts[fact_id]
                if not facet_facts:
                    del self._facet_assertions[facet]

            event = FactEvent(fact=fact, facet_id=facet, action="retracted")

            for pattern, callback in self.subscriptions:
                if pattern.matches(fact):
                    if self._in_turn:
                        self._pending_notifications.append((callback, event))
                    else:
                        callback(event)
        return fact

    def subscribe(self, pattern: FactPattern, callback: Callable[[FactEvent], None]) -> None:
        """
        Subscribe to facts matching a pattern.

        Args:
            pattern: Pattern to match
            callback: Function called when matching fact is asserted

        Immediately calls callback for existing matching facts.
        """
        self.subscriptions.append((pattern, callback))
        index = len(self.subscriptions) - 1
        self._subscription_trie.insert(self._build_pattern_path(pattern), index)

        # Trigger for existing facts
        for fact in self.query(pattern):
            facet_id = self._fact_index.get(fact.fact_id, "__anon__")
            callback(FactEvent(fact=fact, facet_id=facet_id, action="asserted"))

    # ────────────────────────────────────────────────────────────────────────
    # Message actions
    # ────────────────────────────────────────────────────────────────────────

    def subscribe_message(self, pattern: MessagePattern, callback: Callable[[MessageEvent], None]) -> None:
        """Subscribe to messages matching a pattern."""
        self.message_subscriptions.append((pattern, callback))

    def send_message(self, message: Message, facet_id: Optional[str] = None, relay: bool = False) -> None:
        """Send a message through the dataspace."""
        facet = facet_id or "__anon__"
        event = MessageEvent(message=message, facet_id=facet)

        for pattern, callback in self.message_subscriptions:
            if pattern.matches(message):
                callback(event)

        if relay and self.parent:
            parent_facet = f"{self.name}.{facet}"
            self.parent.send_message(message, facet_id=parent_facet, relay=False)

    def register_interest(
        self,
        pattern: FactPattern,
        facet_id: str,
        callback: Callable[[FactEvent], None],
        *,
        relay: bool = True,
    ) -> Handle:
        """Register an interest in facts matching pattern on behalf of a facet."""

        interest_fact = FactInterest(
            fact_id=f"interest:{facet_id}:{uuid.uuid4().hex[:8]}",
            facet_id=facet_id,
            fact_type=f"{pattern.fact_type.__module__}.{pattern.fact_type.__qualname__}",
            constraints=dict(pattern.constraints),
        )

        handle = self.assert_fact(interest_fact, facet_id=facet_id, relay=relay)
        key = self._build_pattern_path(pattern)
        record = InterestRegistration(
            facet_id=facet_id,
            pattern=pattern,
            callback=callback,
            handle=handle,
            key=key,
        )
        self._interest_trie.insert(key, record)
        self._interest_registry[facet_id].append(record)
        self._interest_handle_map[handle.handle_id] = record

        # Deliver existing facts to new interest
        for fact in self.query(pattern):
            facet_origin = self._fact_index.get(fact.fact_id, "__anon__")
            event = FactEvent(fact=fact, facet_id=facet_origin, action="asserted")
            callback(event)

        return handle

    def unregister_interest(self, handle: Handle) -> None:
        """Remove a previously registered interest by handle."""

        record = self._interest_handle_map.pop(handle.handle_id, None)
        if not record:
            return

        self._interest_trie.remove(record.key, record)
        registry = self._interest_registry.get(record.facet_id)
        if registry and record in registry:
            registry.remove(record)
            if not registry:
                del self._interest_registry[record.facet_id]

        self.retract(handle)

    def unregister_interests_for_facet(self, facet_id: str) -> None:
        """Remove all interests registered by a facet."""

        records = list(self._interest_registry.get(facet_id, []))
        for record in records:
            self.unregister_interest(record.handle)

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
        for callback, event in self._pending_notifications:
            try:
                callback(event)
            except Exception as exc:
                # Don't let callback errors break turn delivery
                print(f"Subscription callback error: {exc}")

        self._pending_notifications.clear()

    def clear(self, *, retract: bool = True) -> None:
        """Clear all facts and subscriptions."""
        for name, child in list(self.children.items()):
            child.clear(retract=retract)
            self.children.pop(name, None)

        if retract:
            for handles in list(self._facet_retractions.values()):
                for handle in list(handles):
                    self.retract(handle)

        for records in list(self._interest_registry.values()):
            for record in list(records):
                self.unregister_interest(record.handle)
        self._interest_registry.clear()
        self._interest_handle_map.clear()
        self._subscription_trie = PatternTrie()
        self._interest_trie = PatternTrie()

        self.facts.clear()
        self.facts_by_type.clear()
        self._facet_retractions.clear()
        self.subscriptions.clear()
        self.message_subscriptions.clear()
        self._pending_notifications.clear()
        self._facet_assertions.clear()
        self._fact_index.clear()


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
