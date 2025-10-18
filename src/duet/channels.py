"""
Channel store for workflow message passing.

Manages channel payloads during workflow execution, providing get/set/snapshot
operations with optional schema validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .dsl.workflow import Channel


@dataclass
class ChannelStore:
    """
    Stores channel payloads for workflow execution.

    Manages the syndicated workspace where phases communicate through
    structured channels rather than direct message passing.

    Attributes:
        channels: Channel definitions from workflow
        payloads: Current channel values (channel_name -> value)
        history: Historical snapshots for replay/debugging
    """

    channels: Dict[str, Channel]
    payloads: Dict[str, Any] = field(default_factory=dict)
    history: list[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        """Initialize payloads with channel initial values."""
        for name, channel in self.channels.items():
            if channel.initial_value is not None:
                self.payloads[name] = channel.initial_value

    def get(self, channel_name: str, default: Any = None) -> Any:
        """
        Get value from a channel.

        Args:
            channel_name: Name of channel
            default: Default value if channel not set

        Returns:
            Channel value or default
        """
        return self.payloads.get(channel_name, default)

    def set(self, channel_name: str, value: Any) -> None:
        """
        Set value in a channel.

        Args:
            channel_name: Name of channel
            value: Value to store

        Raises:
            ValueError: If channel not declared in workflow
        """
        if channel_name not in self.channels:
            raise ValueError(
                f"Cannot set undeclared channel: '{channel_name}'\n"
                f"Declared channels: {', '.join(sorted(self.channels.keys()))}"
            )

        self.payloads[channel_name] = value

    def update(self, updates: Dict[str, Any]) -> None:
        """
        Bulk update multiple channels.

        Args:
            updates: Dictionary of channel_name -> value

        Raises:
            ValueError: If any channel not declared
        """
        for channel_name, value in updates.items():
            self.set(channel_name, value)

    def snapshot(self) -> Dict[str, Any]:
        """
        Create a snapshot of current channel state.

        Returns:
            Dictionary of all channel values
        """
        return dict(self.payloads)

    def restore(self, snapshot: Dict[str, Any]) -> None:
        """
        Restore channel state from a snapshot.

        Args:
            snapshot: Previously captured channel state
        """
        self.payloads = dict(snapshot)

    def save_snapshot(self) -> None:
        """Save current state to history."""
        self.history.append(self.snapshot())

    def get_schema(self, channel_name: str) -> Optional[str]:
        """
        Get schema hint for a channel.

        Args:
            channel_name: Name of channel

        Returns:
            Schema string or None if not defined
        """
        channel = self.channels.get(channel_name)
        return channel.schema if channel else None

    def validate_value(self, channel_name: str, value: Any) -> bool:
        """
        Validate a value against channel schema (basic validation).

        Future: Implement comprehensive schema validation.

        Args:
            channel_name: Name of channel
            value: Value to validate

        Returns:
            True if valid (always True for now, future: schema checks)
        """
        schema = self.get_schema(channel_name)

        # Basic type checks (extensible in future)
        if schema == "text":
            return isinstance(value, str)
        elif schema == "json":
            return isinstance(value, (dict, list))
        elif schema == "verdict":
            return isinstance(value, str) and value in ("approve", "changes_requested", "blocked")
        elif schema == "git_diff":
            return isinstance(value, (str, dict))  # String diff or parsed dict

        # No schema or unrecognized schema: accept any value
        return True

    def get_all(self) -> Dict[str, Any]:
        """
        Get all channel payloads.

        Returns:
            Dictionary of all channel values
        """
        return dict(self.payloads)

    def clear(self) -> None:
        """Clear all channel payloads (preserves channel definitions)."""
        self.payloads.clear()
        # Re-initialize with initial values
        self.__post_init__()
