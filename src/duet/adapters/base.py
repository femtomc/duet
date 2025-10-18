"""Adapter interfaces and registry utilities."""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Type, TypedDict

from ..models import AssistantRequest, AssistantResponse


class StreamEvent(TypedDict, total=False):
    """
    Event emitted during streaming adapter execution.

    Required fields: event_type, payload, timestamp
    Optional enriched fields: text_snippet, reasoning_step, usage, tool_info
    """

    # Required fields
    event_type: str  # Canonical type (use CanonicalEventType enum values)
    payload: Dict[str, Any]  # Original raw event data
    timestamp: datetime.datetime

    # Optional enriched fields
    text_snippet: str  # Text content for messages/reasoning
    reasoning_step: int  # Step number for reasoning events
    usage: Dict[str, int]  # Token counts {input_tokens, output_tokens, cached_input_tokens}
    tool_info: Dict[str, Any]  # Tool invocation details {tool_name, status, output_preview}


class AssistantAdapter(ABC):
    """Abstract base class for orchestration assistants."""

    name: str
    role: str

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.extra = kwargs

    @abstractmethod
    def stream(
        self,
        request: AssistantRequest,
        on_event: Optional[Callable[[StreamEvent], None]] = None,
    ) -> AssistantResponse:
        """
        Execute the request with streaming support.

        Args:
            request: The assistant request to process
            on_event: Optional callback invoked for each streaming event

        Returns:
            Final AssistantResponse after stream completes
        """

    def generate(self, request: AssistantRequest) -> AssistantResponse:
        """
        Produce a response for the given prompt (non-streaming wrapper).

        This is a convenience method that calls stream() without a callback,
        maintaining backward compatibility with existing code.
        """
        return self.stream(request, on_event=None)


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
