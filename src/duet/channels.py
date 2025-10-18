"""
Channel store for workflow message passing.

Manages channel payloads during workflow execution, providing get/set/snapshot
operations with optional schema validation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .dsl.workflow import Channel


def serialize_channel_message(
    channel_name: str,
    value: Any,
    schema: Optional[str] = None,
    source_phase: Optional[str] = None,
    truncate_at: int = 5000,
) -> Dict[str, Any]:
    """
    Serialize a channel message for database storage.

    Converts channel payload to JSON-safe format with metadata.

    Args:
        channel_name: Name of the channel
        value: Channel payload (any type)
        schema: Channel schema hint
        source_phase: Phase that published this message
        truncate_at: Maximum payload length before truncation

    Returns:
        Dictionary with payload and metadata ready for JSON encoding
    """
    # Convert value to string representation
    if isinstance(value, str):
        payload_str = value
    elif isinstance(value, (dict, list)):
        import json
        payload_str = json.dumps(value, indent=2)
    else:
        payload_str = str(value)

    # Truncate large payloads
    truncated = False
    if len(payload_str) > truncate_at:
        payload_str = payload_str[:truncate_at]
        truncated = True

    # Build metadata
    metadata = {
        "schema": schema,
        "source_phase": source_phase,
        "truncated": truncated,
        "original_length": len(str(value)),
        "serialized_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    return {
        "channel": channel_name,
        "payload": payload_str,
        "metadata": metadata,
    }


def deserialize_channel_message(message: Dict[str, Any]) -> Any:
    """
    Deserialize a channel message from database.

    Reconstructs original payload from stored JSON.

    Args:
        message: Message dictionary from database

    Returns:
        Reconstructed payload value
    """
    payload = message.get("payload")
    metadata = message.get("metadata", {})
    schema = metadata.get("schema")

    # Try to reconstruct based on schema
    if schema == "json":
        import json
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return payload

    # Default: return as-is (string)
    return payload


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
