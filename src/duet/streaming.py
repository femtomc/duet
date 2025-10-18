"""Streaming utilities for incremental content tracking and display (Sprint 7)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .adapters.base import StreamEvent
from .models import CanonicalEventType


class StreamAccumulator:
    """
    Tracks incremental content across streaming events.

    Builds cumulative assistant output, reasoning steps, and tool invocations
    as events arrive from adapters.
    """

    def __init__(self):
        self.accumulated_text = ""
        self.reasoning_steps: List[str] = []
        self.tool_invocations: List[Dict[str, Any]] = []
        self.latest_delta = ""
        self.usage: Optional[Dict[str, int]] = None
        self.event_count = 0
        self.parse_error_count = 0

    def process_event(self, event: StreamEvent) -> None:
        """
        Update accumulator with new event.

        Args:
            event: Streaming event from adapter
        """
        self.event_count += 1
        event_type = event["event_type"]

        if event_type == CanonicalEventType.ASSISTANT_MESSAGE or event_type == "assistant_message":
            # Append to accumulated text
            delta = event.get("text_snippet", "")
            if delta:
                self.accumulated_text += delta
                self.latest_delta = delta

        elif event_type == CanonicalEventType.REASONING or event_type == "reasoning":
            # Store reasoning step
            text = event.get("text_snippet", "")
            if text:
                self.reasoning_steps.append(text)

        elif event_type == CanonicalEventType.TOOL_USE or event_type == "tool_use":
            # Store tool invocation
            tool_info = event.get("tool_info", {})
            if tool_info:
                self.tool_invocations.append(tool_info)

        elif event_type == CanonicalEventType.TURN_COMPLETE or event_type == "turn_complete":
            # Update usage metadata
            usage = event.get("usage")
            if usage:
                self.usage = usage

        elif event_type == CanonicalEventType.PARSE_ERROR or event_type == "parse_error":
            self.parse_error_count += 1

    def get_preview(self, max_length: int = 300) -> str:
        """
        Get a preview of accumulated text.

        Args:
            max_length: Maximum preview length

        Returns:
            Text preview (last max_length characters)
        """
        if not self.accumulated_text:
            return ""

        preview = self.accumulated_text[-max_length:]
        if len(self.accumulated_text) > max_length:
            preview = "..." + preview
        return preview

    def get_latest_reasoning(self) -> Optional[str]:
        """Get the most recent reasoning step."""
        return self.reasoning_steps[-1] if self.reasoning_steps else None

    def get_latest_tool(self) -> Optional[Dict[str, Any]]:
        """Get the most recent tool invocation."""
        return self.tool_invocations[-1] if self.tool_invocations else None

    def get_metrics(self) -> Dict[str, Any]:
        """
        Get summary metrics.

        Returns:
            Dictionary with event counts, token usage, etc.
        """
        return {
            "event_count": self.event_count,
            "reasoning_steps": len(self.reasoning_steps),
            "tool_invocations": len(self.tool_invocations),
            "parse_errors": self.parse_error_count,
            "accumulated_text_length": len(self.accumulated_text),
            "usage": self.usage,
        }
