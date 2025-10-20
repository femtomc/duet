"""Assistant adapter implementations."""

from .base import REGISTRY, AdapterRegistry, AssistantAdapter, register_adapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .echo import EchoAdapter


def get_adapter(config):
    """
    Get adapter instance from configuration.

    Args:
        config: AssistantConfig with provider and model

    Returns:
        AssistantAdapter instance

    Raises:
        KeyError: If provider not found in registry

    Example:
        adapter = get_adapter(duet_config.codex)
    """
    provider = config.provider
    return REGISTRY.resolve(provider)


__all__ = [
    "AssistantAdapter",
    "AdapterRegistry",
    "REGISTRY",
    "register_adapter",
    "get_adapter",
    "EchoAdapter",
    "CodexAdapter",
    "ClaudeCodeAdapter",
]
