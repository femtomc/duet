"""
Facet registry for managing facet IDs and fact types.

Provides global tracking of facets, emitted facts, and alias resolution
for validation and introspection.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Type

from ..dataspace import Fact
from .facet import FacetDefinition


class FacetRegistry:
    """
    Global registry for facet definitions and fact types.

    Tracks:
    - Facet definitions by ID
    - Fact types emitted by each facet
    - Fact types consumed by each facet
    - Alias mappings

    Used for validation, introspection, and dependency analysis.
    """

    def __init__(self):
        self._facets: Dict[str, FacetDefinition] = {}
        self._emitted_facts: Dict[str, List[Type]] = {}  # facet_id -> [fact_types]
        self._consumed_facts: Dict[str, List[Type]] = {}  # facet_id -> [fact_types]
        self._aliases: Dict[str, Dict[str, Type]] = {}  # facet_id -> {alias: fact_type}

    def register(self, facet: FacetDefinition) -> None:
        """
        Register a facet definition.

        Args:
            facet: Facet to register

        Raises:
            ValueError: If facet ID already registered
        """
        if facet.name in self._facets:
            raise ValueError(f"Facet '{facet.name}' already registered")

        self._facets[facet.name] = facet
        self._emitted_facts[facet.name] = facet.emitted_facts.copy()
        self._consumed_facts[facet.name] = list(facet.alias_map.values())
        self._aliases[facet.name] = facet.alias_map.copy()

    def get(self, facet_id: str) -> Optional[FacetDefinition]:
        """Get facet definition by ID."""
        return self._facets.get(facet_id)

    def get_emitted_facts(self, facet_id: str) -> List[Type]:
        """Get fact types emitted by facet."""
        return self._emitted_facts.get(facet_id, [])

    def get_consumed_facts(self, facet_id: str) -> List[Type]:
        """Get fact types consumed by facet."""
        return self._consumed_facts.get(facet_id, [])

    def get_aliases(self, facet_id: str) -> Dict[str, Type]:
        """Get alias mappings for facet."""
        return self._aliases.get(facet_id, {})

    def find_producers(self, fact_type: Type) -> List[str]:
        """
        Find facets that emit a given fact type.

        Args:
            fact_type: Fact type to search for

        Returns:
            List of facet IDs that emit this type
        """
        producers = []
        for facet_id, emitted in self._emitted_facts.items():
            if fact_type in emitted:
                producers.append(facet_id)
        return producers

    def find_consumers(self, fact_type: Type) -> List[str]:
        """
        Find facets that consume a given fact type.

        Args:
            fact_type: Fact type to search for

        Returns:
            List of facet IDs that consume this type
        """
        consumers = []
        for facet_id, consumed in self._consumed_facts.items():
            if fact_type in consumed:
                consumers.append(facet_id)
        return consumers

    def get_dependency_graph(self) -> Dict[str, List[str]]:
        """
        Build dependency graph (facet -> [dependent_facets]).

        A facet depends on another if it consumes facts the other emits.

        Returns:
            Dict mapping facet_id to list of facets it depends on
        """
        graph = {}

        for facet_id, consumed_types in self._consumed_facts.items():
            dependencies = []
            for fact_type in consumed_types:
                producers = self.find_producers(fact_type)
                dependencies.extend(producers)

            graph[facet_id] = list(set(dependencies))  # Remove duplicates

        return graph

    def validate_dependencies(self) -> List[str]:
        """
        Validate all facet dependencies are satisfied.

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []

        # Check each facet's consumed facts
        for facet_id, consumed_types in self._consumed_facts.items():
            for fact_type in consumed_types:
                producers = self.find_producers(fact_type)
                if not producers:
                    errors.append(
                        f"Facet '{facet_id}' consumes {fact_type.__name__} "
                        f"but no facet emits it"
                    )

        return errors

    def detect_cycles(self) -> List[List[str]]:
        """
        Detect circular dependencies in facet graph.

        Returns:
            List of cycles (each cycle is a list of facet IDs)
        """
        graph = self.get_dependency_graph()
        cycles = []

        def dfs(node: str, path: List[str], visited: Set[str]) -> None:
            """DFS to find cycles."""
            if node in path:
                # Found cycle
                cycle_start = path.index(node)
                cycle = path[cycle_start:] + [node]
                cycles.append(cycle)
                return

            if node in visited:
                return

            visited.add(node)
            path.append(node)

            # Visit dependencies
            for dep in graph.get(node, []):
                dfs(dep, path.copy(), visited)

        visited_global = set()
        for facet_id in self._facets.keys():
            if facet_id not in visited_global:
                dfs(facet_id, [], visited_global)

        return cycles

    def clear(self) -> None:
        """Clear all registered facets."""
        self._facets.clear()
        self._emitted_facts.clear()
        self._consumed_facts.clear()
        self._aliases.clear()

    def list_facets(self) -> List[str]:
        """Get list of all registered facet IDs."""
        return list(self._facets.keys())

    def __len__(self) -> int:
        """Number of registered facets."""
        return len(self._facets)

    def __contains__(self, facet_id: str) -> bool:
        """Check if facet is registered."""
        return facet_id in self._facets


# Global registry instance
_global_registry = FacetRegistry()


def register_facet(facet: FacetDefinition) -> None:
    """Register facet in global registry."""
    _global_registry.register(facet)


def get_facet(facet_id: str) -> Optional[FacetDefinition]:
    """Get facet from global registry."""
    return _global_registry.get(facet_id)


def get_registry() -> FacetRegistry:
    """Get global registry instance."""
    return _global_registry


def clear_registry() -> None:
    """Clear global registry (useful for testing)."""
    _global_registry.clear()
