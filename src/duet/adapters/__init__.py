"""Assistant adapter implementations."""

from .base import REGISTRY, AdapterRegistry, AssistantAdapter, register_adapter
from .echo import EchoAdapter

__all__ = [
    "AssistantAdapter",
    "AdapterRegistry",
    "REGISTRY",
    "register_adapter",
    "EchoAdapter",
]
