"""Streaming utilities for incremental content tracking and display."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel

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


class EnhancedStreamingDisplay:
    """
    Unified streaming display with rich content sections.

    Supports multiple display modes:
    - detailed: Full panel with snippets, reasoning, tools, rolling log
    - compact: Minimal display (phase + iteration + event count)
    - off: No display (equivalent to quiet mode)
    """

    def __init__(
        self,
        console: Console,
        phase: str,
        iteration: int,
        mode: str = "detailed",
        max_events: int = 50,
    ):
        self.console = console
        self.phase = phase
        self.iteration = iteration
        self.mode = mode
        self.max_events = max_events

        self.accumulator = StreamAccumulator()
        self.start_time = time.time()
        self.events: List[StreamEvent] = []

    def add_event(self, event: StreamEvent) -> None:
        """
        Add an event to the display.

        Args:
            event: Streaming event from adapter
        """
        # Update accumulator
        self.accumulator.process_event(event)

        # Store event for rolling log (keep last N)
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events.pop(0)

    def render(self) -> Optional[Panel]:
        """
        Render the display based on current mode.

        Returns:
            Panel for display or None if mode is 'off'
        """
        if self.mode == "off":
            return None
        elif self.mode == "compact":
            return self._render_compact()
        else:  # detailed
            return self._render_detailed()

    def _render_compact(self) -> Panel:
        """Render compact mode (minimal display)."""
        elapsed = int(time.time() - self.start_time)
        metrics = self.accumulator.get_metrics()

        content = (
            f"[bold]{self.phase.upper()}[/] | "
            f"Iteration {self.iteration} | "
            f"Events: {metrics['event_count']} | "
            f"{elapsed}s"
        )

        return Panel(content, border_style="cyan", expand=False)

    def _render_detailed(self) -> Panel:
        """Render detailed mode with all sections."""
        elapsed = int(time.time() - self.start_time)
        lines = []

        # Header
        lines.append(
            f"[bold cyan]{self.phase.upper()}[/] | "
            f"Iteration {self.iteration} | "
            f"[dim]{elapsed}s elapsed[/]"
        )
        lines.append("")

        # Current status (derived from recent events)
        status = self._derive_status()
        lines.append(f"[bold]Status:[/] {status}")
        lines.append("")

        # Progress metrics
        metrics = self.accumulator.get_metrics()
        lines.append(f"[bold]Progress:[/]")
        lines.append(f"  • Events: {metrics['event_count']}")

        if metrics["reasoning_steps"] > 0:
            lines.append(f"  • Reasoning steps: {metrics['reasoning_steps']}")

        if metrics["tool_invocations"] > 0:
            lines.append(f"  • Tool uses: {metrics['tool_invocations']}")

        # Token usage
        if metrics["usage"]:
            usage = metrics["usage"]
            input_tok = usage.get("input_tokens", 0)
            output_tok = usage.get("output_tokens", 0)
            lines.append(f"  • Tokens: {input_tok} in / {output_tok} out")

        # Response preview (last 300 chars of accumulated text)
        preview = self.accumulator.get_preview(max_length=300)
        if preview:
            lines.append("")
            lines.append(f"[bold]Response Preview:[/]")
            # Show up to 3 lines
            preview_lines = preview.split("\n")[:3]
            for line in preview_lines:
                if line.strip():
                    lines.append(f"  [dim]{line[:80]}[/]")

        # Rolling event log (last 3 events)
        if self.events:
            lines.append("")
            lines.append(f"[bold]Recent Events:[/]")
            for event in self.events[-3:]:
                timestamp = event["timestamp"].strftime("%H:%M:%S")
                event_type = event["event_type"]
                formatted = self._format_event_type(event_type)
                lines.append(f"  [dim]{timestamp}[/] {formatted}")

        content = "\n".join(lines)
        return Panel(content, title="[bold cyan]Streaming Output[/]", border_style="cyan")

    def _derive_status(self) -> str:
        """Derive current status from recent events."""
        if not self.events:
            return "[dim]Initializing...[/]"

        # Check last few events for context
        last_event = self.events[-1]
        event_type = last_event["event_type"]

        # Get latest reasoning
        latest_reasoning = self.accumulator.get_latest_reasoning()
        if latest_reasoning and event_type == "reasoning":
            snippet = latest_reasoning[:50]
            return f"[yellow]Reasoning:[/] {snippet}..."

        # Get latest tool
        latest_tool = self.accumulator.get_latest_tool()
        if latest_tool and event_type == "tool_use":
            tool_name = latest_tool.get("tool_name", "unknown")
            return f"[cyan]Running {tool_name}...[/]"

        # Check event type
        if event_type == "assistant_message":
            return "[green]Generating response...[/]"
        elif event_type == "turn_complete":
            return "[green]Turn complete[/]"
        elif event_type == "thread_started":
            return "[cyan]Starting...[/]"
        elif event_type == "parse_error":
            return "[red]Parse error detected[/]"
        else:
            return f"[dim]{event_type}[/]"

    def _format_event_type(self, event_type: str) -> str:
        """Format event type for display with icons."""
        if event_type == "assistant_message":
            return "[green]✓[/] Message"
        elif event_type == "reasoning":
            return "[yellow]…[/] Reasoning"
        elif event_type == "tool_use":
            return "[cyan]▶[/] Tool"
        elif event_type == "turn_complete":
            return "[green]✓[/] Complete"
        elif event_type == "parse_error":
            return "[red]✗[/] Parse error"
        elif event_type == "thread_started":
            return "[cyan]▶[/] Started"
        else:
            return f"[dim]{event_type}[/]"
