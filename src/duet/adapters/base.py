"""Adapter interfaces and registry utilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Type

from ..models import AssistantRequest, AssistantResponse


class AssistantAdapter(ABC):
    """Abstract base class for orchestration assistants."""

    name: str
    role: str

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.extra = kwargs

    @abstractmethod
    def generate(self, request: AssistantRequest) -> AssistantResponse:
        """Produce a response for the given prompt."""


@dataclass
class AdapterRegistry:
    """Registry for resolving adapters by name."""

    adapters: Dict[str, Type[AssistantAdapter]]

    def __init__(self) -> None:
        self.adapters = {}

    def register(self, name: str, adapter_cls: Type[AssistantAdapter]) -> None:
        if name in self.adapters:
            raise ValueError(f"Adapter {name!r} already registered")
        self.adapters[name] = adapter_cls

    def resolve(self, name: str, **kwargs) -> AssistantAdapter:
        if name not in self.adapters:
            raise KeyError(f"Adapter {name!r} not registered")
        return self.adapters[name](**kwargs)


REGISTRY = AdapterRegistry()


def register_adapter(name: str):
    """Decorator to register an adapter class."""

    def decorator(cls: Type[AssistantAdapter]) -> Type[AssistantAdapter]:
        REGISTRY.register(name, cls)
        return cls

    return decorator
