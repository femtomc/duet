"""
Unit tests for streaming utilities.

Tests StreamAccumulator and event enrichment logic.
"""

from __future__ import annotations

import datetime

import pytest

from duet.models import CanonicalEventType
from duet.streaming import StreamAccumulator


# ──────────────────────────────────────────────────────────────────────────────
# StreamAccumulator Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_stream_accumulator_initialization():
    """Test StreamAccumulator initializes with empty state."""
    acc = StreamAccumulator()

    assert acc.accumulated_text == ""
    assert acc.reasoning_steps == []
    assert acc.tool_invocations == []
    assert acc.latest_delta == ""
    assert acc.usage is None
    assert acc.event_count == 0
    assert acc.parse_error_count == 0


def test_stream_accumulator_tracks_assistant_messages():
    """Test accumulator builds cumulative text from assistant messages."""
    acc = StreamAccumulator()

    event1 = {
        "event_type": "assistant_message",
        "text_snippet": "Hello ",
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }
    event2 = {
        "event_type": "assistant_message",
        "text_snippet": "world!",
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }

    acc.process_event(event1)
    acc.process_event(event2)

    assert acc.accumulated_text == "Hello world!"
    assert acc.latest_delta == "world!"
    assert acc.event_count == 2


def test_stream_accumulator_tracks_reasoning_steps():
    """Test accumulator stores reasoning steps."""
    acc = StreamAccumulator()

    event1 = {
        "event_type": "reasoning",
        "text_snippet": "Analyzing module structure",
        "reasoning_step": 0,
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }
    event2 = {
        "event_type": "reasoning",
        "text_snippet": "Identifying dependencies",
        "reasoning_step": 1,
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }

    acc.process_event(event1)
    acc.process_event(event2)

    assert len(acc.reasoning_steps) == 2
    assert acc.reasoning_steps[0] == "Analyzing module structure"
    assert acc.reasoning_steps[1] == "Identifying dependencies"
    assert acc.get_latest_reasoning() == "Identifying dependencies"


def test_stream_accumulator_tracks_tool_invocations():
    """Test accumulator stores tool invocations."""
    acc = StreamAccumulator()

    event = {
        "event_type": "tool_use",
        "tool_info": {
            "tool_name": "pytest",
            "status": "running",
            "output_preview": "===== test session starts =====",
        },
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }

    acc.process_event(event)

    assert len(acc.tool_invocations) == 1
    assert acc.tool_invocations[0]["tool_name"] == "pytest"
    assert acc.get_latest_tool()["tool_name"] == "pytest"


def test_stream_accumulator_tracks_token_usage():
    """Test accumulator captures usage from turn_complete events."""
    acc = StreamAccumulator()

    event = {
        "event_type": "turn_complete",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_input_tokens": 25,
        },
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }

    acc.process_event(event)

    assert acc.usage is not None
    assert acc.usage["input_tokens"] == 100
    assert acc.usage["output_tokens"] == 50
    assert acc.usage["cached_input_tokens"] == 25


def test_stream_accumulator_tracks_parse_errors():
    """Test accumulator counts parse errors."""
    acc = StreamAccumulator()

    event1 = {
        "event_type": "parse_error",
        "payload": {"error": "Invalid JSON"},
        "timestamp": datetime.datetime.now(),
    }
    event2 = {
        "event_type": "parse_error",
        "payload": {"error": "Unexpected token"},
        "timestamp": datetime.datetime.now(),
    }

    acc.process_event(event1)
    acc.process_event(event2)

    assert acc.parse_error_count == 2
    assert acc.event_count == 2


def test_stream_accumulator_get_preview():
    """Test get_preview returns truncated text."""
    acc = StreamAccumulator()

    # Add long text
    long_text = "A" * 500
    event = {
        "event_type": "assistant_message",
        "text_snippet": long_text,
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }

    acc.process_event(event)

    # Default preview (300 chars)
    preview = acc.get_preview()
    assert len(preview) <= 303  # "..." + 300 chars
    assert preview.startswith("...")
    assert preview.endswith("A")

    # Custom length
    preview_short = acc.get_preview(max_length=100)
    assert len(preview_short) <= 103


def test_stream_accumulator_get_metrics():
    """Test get_metrics returns comprehensive summary."""
    acc = StreamAccumulator()

    # Add various events
    acc.process_event({
        "event_type": "assistant_message",
        "text_snippet": "Response text",
        "payload": {},
        "timestamp": datetime.datetime.now(),
    })
    acc.process_event({
        "event_type": "reasoning",
        "text_snippet": "Thinking...",
        "payload": {},
        "timestamp": datetime.datetime.now(),
    })
    acc.process_event({
        "event_type": "tool_use",
        "tool_info": {"tool_name": "pytest"},
        "payload": {},
        "timestamp": datetime.datetime.now(),
    })
    acc.process_event({
        "event_type": "turn_complete",
        "usage": {"input_tokens": 50, "output_tokens": 25},
        "payload": {},
        "timestamp": datetime.datetime.now(),
    })

    metrics = acc.get_metrics()

    assert metrics["event_count"] == 4
    assert metrics["reasoning_steps"] == 1
    assert metrics["tool_invocations"] == 1
    assert metrics["parse_errors"] == 0
    assert metrics["accumulated_text_length"] == 13  # "Response text"
    assert metrics["usage"]["input_tokens"] == 50


def test_stream_accumulator_handles_events_without_optional_fields():
    """Test accumulator gracefully handles events missing optional fields."""
    acc = StreamAccumulator()

    # Event without text_snippet
    event = {
        "event_type": "assistant_message",
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }

    acc.process_event(event)

    # Should not crash, just not accumulate text
    assert acc.accumulated_text == ""
    assert acc.event_count == 1


def test_stream_accumulator_uses_canonical_enum_values():
    """Test accumulator works with CanonicalEventType enum values."""
    acc = StreamAccumulator()

    event = {
        "event_type": CanonicalEventType.ASSISTANT_MESSAGE.value,
        "text_snippet": "Using enum",
        "payload": {},
        "timestamp": datetime.datetime.now(),
    }

    acc.process_event(event)

    assert acc.accumulated_text == "Using enum"


def test_stream_accumulator_mixed_event_sequence():
    """Test accumulator with realistic event sequence."""
    acc = StreamAccumulator()

    # Simula realistic Codex stream
    events = [
        {
            "event_type": CanonicalEventType.THREAD_STARTED.value,
            "payload": {"thread_id": "abc"},
            "timestamp": datetime.datetime.now(),
        },
        {
            "event_type": CanonicalEventType.REASONING.value,
            "text_snippet": "Analyzing requirements",
            "reasoning_step": 0,
            "payload": {},
            "timestamp": datetime.datetime.now(),
        },
        {
            "event_type": CanonicalEventType.REASONING.value,
            "text_snippet": "Planning implementation",
            "reasoning_step": 1,
            "payload": {},
            "timestamp": datetime.datetime.now(),
        },
        {
            "event_type": CanonicalEventType.ASSISTANT_MESSAGE.value,
            "text_snippet": "Here is the plan:\n",
            "payload": {},
            "timestamp": datetime.datetime.now(),
        },
        {
            "event_type": CanonicalEventType.ASSISTANT_MESSAGE.value,
            "text_snippet": "1. Implement feature X\n2. Add tests",
            "payload": {},
            "timestamp": datetime.datetime.now(),
        },
        {
            "event_type": CanonicalEventType.TURN_COMPLETE.value,
            "usage": {"input_tokens": 200, "output_tokens": 100},
            "payload": {},
            "timestamp": datetime.datetime.now(),
        },
    ]

    for event in events:
        acc.process_event(event)

    # Verify accumulated state
    assert acc.event_count == 6
    assert len(acc.reasoning_steps) == 2
    assert "Here is the plan:" in acc.accumulated_text
    assert "Add tests" in acc.accumulated_text
    assert acc.usage["output_tokens"] == 100

    # Verify preview
    preview = acc.get_preview(max_length=100)
    assert len(preview) <= 103  # "..." + 100
    assert "Add tests" in preview  # Should show end of text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
