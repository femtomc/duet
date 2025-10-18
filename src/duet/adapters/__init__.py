"""Assistant adapter implementations."""

from .base import REGISTRY, AdapterRegistry, AssistantAdapter, register_adapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .echo import EchoAdapter

__all__ = [
    "AssistantAdapter",
    "AdapterRegistry",
    "REGISTRY",
    "register_adapter",
    "EchoAdapter",
    "CodexAdapter",
    "ClaudeCodeAdapter",
]
