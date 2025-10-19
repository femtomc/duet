# Sprint 7: Enhanced Streaming UX

Building on Sprint 6's streaming infrastructure, Sprint 7 focuses on enriching the live feedback experience with incremental content, unified display components, and powerful replay/export tooling.

## Goal

Turn streaming into a first-class, informative experience across `duet run`, `duet init`, and future tooling by providing richer real-time visibility into Codex and Claude execution.

---

## 1. Stream Model Enhancements

### 1.1 Event Taxonomy

**Objective**: Create a canonical set of event types for consistent downstream handling.

**Canonical Event Types:**
- `assistant_message` - Main response content
- `reasoning` - Thinking/planning steps
- `tool_use` - Tool invocation (file operations, commands, etc.)
- `turn_complete` - Turn finished with usage metadata
- `parse_error` - JSON parsing failure
- `system_notice` - System-level notifications

**Implementation:**
```python
# Update StreamEvent with optional fields
class StreamEvent(TypedDict):
    event_type: str                        # Canonical type
    payload: Dict[str, Any]                # Original payload
    timestamp: datetime.datetime
    # Optional enriched fields
    text_snippet: NotRequired[str]         # For messages/reasoning
    reasoning_step: NotRequired[int]       # Step number
    usage: NotRequired[Dict[str, int]]     # Token counts
    tool_info: NotRequired[Dict[str, Any]] # Tool invocation details
```

**Adapter Changes:**
- Map Codex `item.completed` → canonical types based on `item.type`
- Map Claude Code events → canonical types
- Normalize payload structure for consistent access

### 1.2 Incremental Content Tracking

**Objective**: Build cumulative assistant output as stream progresses.

**Implementation:**
```python
class StreamAccumulator:
    """Tracks incremental content across streaming events."""

    def __init__(self):
        self.accumulated_text = ""
        self.reasoning_steps = []
        self.tool_invocations = []
        self.latest_delta = ""

    def process_event(self, event: StreamEvent) -> None:
        """Update accumulator with new event."""
        if event["event_type"] == "assistant_message":
            # Append to accumulated text
            delta = event.get("text_snippet", "")
            self.accumulated_text += delta
            self.latest_delta = delta

        elif event["event_type"] == "reasoning":
            self.reasoning_steps.append(event.get("text_snippet", ""))

        elif event["event_type"] == "tool_use":
            self.tool_invocations.append(event.get("tool_info", {}))
```

**For Codex:**
- Track cumulative message from successive `agent_message` items
- Store reasoning text from `reasoning` items
- Capture tool use from `tool_use` items

**For Claude:**
- If only final result available, detect content changes across events
- Build incremental snippets by diffing successive payloads
- Store deltas for progressive display

### 1.3 Reasoning/Tool Metadata

**Objective**: Normalize tool outputs and reasoning for display.

**Payload Structure:**
```python
# Reasoning event
{
    "event_type": "reasoning",
    "text_snippet": "Analyzing module structure...",
    "reasoning_step": 1,
    "payload": {...}  # Original event
}

# Tool use event
{
    "event_type": "tool_use",
    "tool_info": {
        "tool_name": "pytest",
        "status": "running",
        "output_preview": "===== test session starts ====="
    },
    "payload": {...}
}
```

**Display:**
- Show "Claude running pytest..." when tool_use detected
- Display "Codex reasoning about module structure..." for reasoning steps
- Include tool output previews (first 100 chars)

---

## 2. Unified Streaming Display Component

### 2.1 StreamingDisplay Rewrite

**Objective**: Single component supporting multiple sections with rich content.

**New Architecture:**
```python
class EnhancedStreamingDisplay:
    """Unified streaming display with rich content sections."""

    def __init__(
        self,
        console: Console,
        phase: Phase,
        iteration: int,
        mode: str = "detailed",  # compact | detailed | off
    ):
        self.console = console
        self.phase = phase
        self.iteration = iteration
        self.mode = mode

        self.accumulator = StreamAccumulator()
        self.start_time = time.time()
        self.events = []  # Last N events for rolling log

    def render(self) -> Panel:
        """Render multi-section panel."""
        elapsed = int(time.time() - self.start_time)

        if self.mode == "compact":
            return self._render_compact(elapsed)
        elif self.mode == "detailed":
            return self._render_detailed(elapsed)
        else:
            return None  # Off mode (quiet)

    def _render_detailed(self, elapsed: int) -> Panel:
        """Detailed mode with all sections."""
        lines = []

        # Header
        lines.append(f"[bold cyan]{self.phase.upper()}[/] | Iteration {self.iteration} | {elapsed}s elapsed")
        lines.append("")

        # Current status (derived from recent events)
        status = self._derive_status()
        lines.append(f"[bold]Status:[/] {status}")
        lines.append("")

        # Progress metrics
        lines.append(f"[bold]Progress:[/]")
        lines.append(f"  • Events: {len(self.events)}")
        if self.accumulator.reasoning_steps:
            lines.append(f"  • Reasoning steps: {len(self.accumulator.reasoning_steps)}")
        if self.accumulator.tool_invocations:
            lines.append(f"  • Tool uses: {len(self.accumulator.tool_invocations)}")

        # Token usage (if available)
        if self.accumulator.usage:
            usage = self.accumulator.usage
            lines.append(f"  • Tokens: {usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out")

        # Latest message snippet (scrolling window)
        if self.accumulator.accumulated_text:
            lines.append("")
            lines.append(f"[bold]Response Preview:[/]")
            # Show last 300 chars, truncate intelligently
            preview = self.accumulator.accumulated_text[-300:]
            if len(self.accumulator.accumulated_text) > 300:
                preview = "..." + preview
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
                lines.append(f"  [dim]{timestamp}[/] {self._format_event_type(event_type)}")

        content = "\n".join(lines)
        return Panel(content, title="[bold cyan]Streaming Output[/]", border_style="cyan")

    def _derive_status(self) -> str:
        """Derive current status from recent events."""
        if not self.events:
            return "[dim]Initializing...[/]"

        last_event = self.events[-1]
        event_type = last_event["event_type"]

        if event_type == "reasoning":
            snippet = last_event.get("text_snippet", "")[:50]
            return f"[yellow]Reasoning:[/] {snippet}..."
        elif event_type == "tool_use":
            tool_name = last_event.get("tool_info", {}).get("tool_name", "unknown")
            return f"[cyan]Running {tool_name}...[/]"
        elif event_type == "assistant_message":
            return "[green]Generating response...[/]"
        elif event_type == "turn_complete":
            return "[green]Turn complete[/]"
        else:
            return f"[dim]{event_type}[/]"
```

### 2.2 Configurable Verbosity

**Configuration:**
```yaml
logging:
  quiet: false
  stream_mode: "detailed"  # compact | detailed | off
  stream_window_size: 50   # Number of events to retain
```

**CLI Overrides:**
```bash
duet run --stream-mode detailed  # Override config
duet run --stream-mode compact   # Minimal display
duet run --quiet                 # Equivalent to stream_mode: off
```

**Mode Descriptions:**
- **off** (quiet): No live display, events still persisted
- **compact**: Phase + iteration + event count only (current Sprint 6 behavior)
- **detailed**: Full panel with snippets, reasoning, tools, rolling log

### 2.3 Reuse Across Contexts

**Unified Usage:**
```python
# In orchestrator
display = EnhancedStreamingDisplay(console, phase, iteration, mode=config.logging.stream_mode)

# In init.py
display = EnhancedStreamingDisplay(console, "plan", 0, mode="detailed")  # Always detailed for init

# Both use same rendering logic
with Live(display.render(), ...) as live:
    response = adapter.stream(request, on_event=lambda e: display.add_event(e) and live.update())
```

**Throttling for High-Volume:**
- Implementation phase may generate many file operation events
- Throttle updates to max 2fps instead of 4fps
- Batch events: only update display on significant events (message, reasoning, turn_complete)

---

## 3. Transcript Replay & Export

### 3.1 CLI Replay

**New Command:**
```bash
duet inspect RUN_ID --replay [OPTIONS]

Options:
  --speed FLOAT        Playback speed multiplier (default: 1.0, range: 0.5-5.0)
  --step              Step-by-step mode (press Enter to advance)
  --phase PHASE       Replay only specific phase events
  --iteration INT     Replay only specific iteration
```

**Implementation:**
```python
def replay_events(
    run_id: str,
    db: DuetDatabase,
    console: Console,
    speed: float = 1.0,
    step_mode: bool = False,
    phase_filter: Optional[str] = None,
    iteration_filter: Optional[int] = None,
) -> None:
    """Replay stored events with live display."""
    events = db.list_events(run_id, phase=phase_filter, iteration=iteration_filter)

    if not events:
        console.print("[yellow]No events to replay[/]")
        return

    display = EnhancedStreamingDisplay(console, "plan", 0, mode="detailed")

    with Live(display.render(), console=console, refresh_per_second=4) as live:
        for i, event in enumerate(events):
            # Add event to display
            display.add_event(event)
            live.update(display.render())

            # Calculate delay based on timestamps and speed
            if i < len(events) - 1:
                next_event = events[i + 1]
                delta = (next_event["timestamp"] - event["timestamp"]).total_seconds()
                delay = max(0.1, delta / speed)  # Minimum 100ms between events

                if step_mode:
                    input("[dim]Press Enter for next event...[/]")
                else:
                    time.sleep(delay)

    console.print("[green]Replay complete[/]")
```

### 3.2 Export Formats

**JSONL Export:**
```bash
duet inspect RUN_ID --output jsonl > events.jsonl
# One event per line for streaming analysis
```

**CSV Export:**
```bash
duet inspect RUN_ID --output csv > events.csv
# Columns: timestamp, event_type, iteration, phase, payload_summary
```

**Implementation:**
```python
if output == "jsonl":
    for event in events:
        # Flatten event to single JSON line
        json_line = json.dumps(event, default=str)
        console.print(json_line)
elif output == "csv":
    import csv
    writer = csv.DictWriter(sys.stdout, fieldnames=[...])
    writer.writeheader()
    for event in events:
        row = {
            "timestamp": event["timestamp"],
            "event_type": event["event_type"],
            "iteration": event.get("iteration"),
            "phase": event.get("phase"),
            "payload_summary": str(event["payload"])[:100],
        }
        writer.writerow(row)
```

**Piping Examples:**
```bash
# Find all parse errors
duet inspect RUN_ID --output jsonl | jq 'select(.event_type == "parse_error")'

# Token usage analysis
duet inspect RUN_ID --output jsonl | jq 'select(.event_type == "turn_complete") | .payload.usage'

# CSV for spreadsheet analysis
duet inspect RUN_ID --output csv | pandas-query "event_type == 'tool_use'"
```

---

## 4. Orchestrator Hook Rewrites

### 4.1 Phase Streaming

**Replace Current Display:**
```python
# Current (Sprint 6)
streaming_display = StreamingDisplay(console, current_phase, iteration)

# New (Sprint 7)
streaming_display = EnhancedStreamingDisplay(
    console,
    current_phase,
    iteration,
    mode=config.logging.stream_mode
)
```

**Enhanced Event Handling:**
```python
def _create_event_handler(self, run_id, iteration, phase, display):
    """Event handler with enriched streaming."""
    accumulator = StreamAccumulator()

    def handle_event(event: StreamEvent) -> None:
        # Persist to SQLite
        if self.db:
            self.db.insert_event(...)

        # Update accumulator
        accumulator.process_event(event)

        # Enrich event with normalized fields
        enriched_event = self._enrich_event(event, accumulator)

        # Update display
        if display:
            display.add_event(enriched_event)

    return handle_event
```

**Capture Mid-Stream Updates:**
- Don't wait for final response to show content
- Display reasoning steps as they arrive
- Show tool invocations immediately
- Update token counts incrementally

### 4.2 Additional Metrics

**Phase-Level Tracking:**
```python
# Add to RunSnapshot metadata
metadata["phase_timings"] = {
    "plan": {"start": "...", "end": "...", "duration": 45.2},
    "implement": {"start": "...", "end": "...", "duration": 180.5},
    "review": {"start": "...", "end": "...", "duration": 30.1},
}

metadata["streaming_metrics"] = {
    "total_events": 127,
    "reasoning_steps": 8,
    "tool_invocations": 12,
    "parse_errors": 0,
}
```

**SQLite Updates:**
```python
# Ensure iterations table captures accurate metrics
# (stream_events, reasoning_steps columns already exist)
db.insert_iteration(
    ...
    stream_metadata={
        "stream_events": accumulator.event_count,
        "reasoning_steps": len(accumulator.reasoning_steps),
        "tool_invocations": len(accumulator.tool_invocations),
    }
)
```

---

## 5. Adapter & Persistence Adjustments

### 5.1 Adapter Callbacks

**Codex Adapter Enhancements:**
```python
def stream(self, request, on_event):
    ...
    # When processing item.completed
    if event_type == "item.completed":
        item = event_data.get("item", {})
        item_type = item.get("type")

        # Emit enriched event
        enriched_event: StreamEvent = {
            "event_type": self._normalize_type(item_type),  # reasoning → reasoning
            "payload": event_data,
            "timestamp": datetime.now(timezone.utc),
        }

        # Add optional fields based on type
        if item_type == "agent_message":
            enriched_event["text_snippet"] = item.get("text", "")
        elif item_type == "reasoning":
            enriched_event["text_snippet"] = item.get("text", "")
            enriched_event["reasoning_step"] = self._reasoning_counter
            self._reasoning_counter += 1
        elif item_type == "tool_use":
            enriched_event["tool_info"] = {
                "tool_name": item.get("name"),
                "input": item.get("input"),
            }

        if on_event:
            on_event(enriched_event)
```

**Claude Code Adapter:**
```python
# Similar enrichment for Claude events
# Detect tool use from payload patterns
# Extract incremental content if available
```

### 5.2 Events Table

**Decision: Use JSON Payload**
- Store enriched fields in `payload` JSON column (no schema changes needed)
- Existing `events` table structure supports this
- Query with JSON extraction: `SELECT json_extract(payload, '$.text_snippet') FROM events`

**Alternative (if needed later):**
```sql
-- Add columns for common fields
ALTER TABLE events ADD COLUMN text_snippet TEXT;
ALTER TABLE events ADD COLUMN reasoning_step INTEGER;
ALTER TABLE events ADD COLUMN tool_name TEXT;
```

**Recommendation**: Stick with JSON payload for flexibility. Only add columns if query performance becomes an issue.

---

## 6. duet init Enhancements

### 6.1 Discovery Streaming

**Already Implemented**: Sprint 6 added Rich Live display with enriched content

**Additional Improvements:**
```python
def render_progress():
    """Enhanced discovery progress."""
    lines = []

    # Header with spinner
    spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    frame = spinner[(event_count // 4) % len(spinner)]
    lines.append(f"{frame} [bold cyan]Context Discovery[/] [dim]({elapsed}s)[/]")
    lines.append("")

    # Status with context
    if latest_reasoning:
        lines.append(f"[yellow]Analyzing:[/] {latest_reasoning[:60]}...")
    elif agent_message_snippet:
        lines.append(f"[green]Generating:[/] Context analysis")
    else:
        lines.append("[dim]Connecting to Codex...[/]")

    # Metrics
    lines.append("")
    lines.append(f"[bold]Progress:[/]")
    lines.append(f"  • Events: {event_count}")
    if reasoning_count > 0:
        lines.append(f"  • Analysis steps: {reasoning_count}")
    if token_count > 0:
        lines.append(f"  • Generated: {token_count} tokens")

    # Preview (latest reasoning or message)
    if latest_reasoning:
        lines.append("")
        lines.append(f"[bold]Current Step:[/]")
        lines.append(f"  [dim]{latest_reasoning[:150]}...[/]")

    return Panel("\n".join(lines), border_style="cyan")
```

### 6.2 Non-Interactive Mode

**Quiet Mode Behavior:**
```python
if config.logging.quiet:
    # No live display
    response = adapter.stream(request, on_event=None)
    # Print summary after completion
    console.print(f"[green]✓ Context discovery complete ({event_count} events, {token_count} tokens)[/]")
else:
    # Full live display
    with Live(...) as live:
        ...
```

**Stream Mode Support:**
```bash
duet init --stream-mode compact   # Minimal progress
duet init --stream-mode detailed  # Rich preview (default)
duet init --quiet                 # No display, summary only
```

---

## 7. Testing

### 7.1 Unit Tests

**Streaming Component Tests:**
```python
def test_stream_accumulator_builds_text():
    """Test StreamAccumulator tracks incremental content."""
    acc = StreamAccumulator()

    event1 = {"event_type": "assistant_message", "text_snippet": "Hello "}
    event2 = {"event_type": "assistant_message", "text_snippet": "world!"}

    acc.process_event(event1)
    acc.process_event(event2)

    assert acc.accumulated_text == "Hello world!"
    assert acc.latest_delta == "world!"

def test_enhanced_display_renders_detailed_mode():
    """Test EnhancedStreamingDisplay renders all sections."""
    display = EnhancedStreamingDisplay(Console(), "plan", 1, mode="detailed")

    event = {
        "event_type": "reasoning",
        "text_snippet": "Analyzing codebase structure",
        "timestamp": datetime.now(),
        "payload": {}
    }
    display.add_event(event)

    panel = display.render()
    assert "Analyzing codebase" in str(panel)
    assert "Reasoning steps: 1" in str(panel)
```

**Adapter Normalization Tests:**
```python
def test_codex_adapter_enriches_events():
    """Test Codex adapter adds normalized fields to events."""
    adapter = CodexAdapter(model="gpt-4")

    events_received = []
    def on_event(event):
        events_received.append(event)

    # Mock stream with reasoning and message items
    ...

    # Verify enriched fields
    reasoning_events = [e for e in events_received if e["event_type"] == "reasoning"]
    assert reasoning_events[0].get("text_snippet") is not None
    assert reasoning_events[0].get("reasoning_step") is not None
```

### 7.2 Integration Tests

**Event Payload Validation:**
```python
def test_orchestrator_stores_enriched_events():
    """Test that enriched event fields are persisted."""
    # Run orchestrator with mock adapter
    ...

    # Query events from database
    events = db.list_events(run_id)

    # Verify payload contains enriched fields
    message_event = next(e for e in events if e["event_type"] == "assistant_message")
    payload = message_event["payload"]
    assert "text_snippet" in payload or "text" in payload
```

### 7.3 Smoke Tests

**Manual Testing Checklist:**
```markdown
## Sprint 7 Smoke Tests

### duet init
- [ ] Run `duet init` on medium-sized repo (1000+ files)
- [ ] Verify detailed progress panel shows:
  - [ ] Elapsed time counter
  - [ ] Reasoning step count
  - [ ] Token count
  - [ ] Message preview (first 200 chars)
- [ ] Verify display updates smoothly (no flicker)
- [ ] Test compact mode: `duet init --stream-mode compact`
- [ ] Test quiet mode: `duet init --quiet`

### duet run
- [ ] Run full orchestration with `duet run`
- [ ] Verify enhanced display during each phase (PLAN/IMPLEMENT/REVIEW)
- [ ] Check reasoning steps are visible during planning
- [ ] Check tool uses are displayed during implementation
- [ ] Verify token counts update incrementally
- [ ] Test mode switching: `duet run --stream-mode compact`

### duet inspect
- [ ] Verify event timeline shows enriched events
- [ ] Test replay: `duet inspect RUN_ID --replay`
- [ ] Test replay speed: `duet inspect RUN_ID --replay --speed 2.0`
- [ ] Test JSONL export: `duet inspect RUN_ID --output jsonl | jq`
- [ ] Test CSV export: `duet inspect RUN_ID --output csv`

### duet history
- [ ] Verify filters work with enriched events
- [ ] Test JSON export includes event counts
```

---

## 8. Documentation

### 8.1 README & Guides

**README Updates:**
```markdown
## Streaming Observability

### Live Display Modes

Duet provides three streaming modes:

**Detailed Mode (default):**
- Shows phase, iteration, and elapsed time
- Displays progress metrics (events, reasoning steps, tools, tokens)
- Real-time response preview (last 300 characters)
- Rolling event log (last 3 events)

**Compact Mode:**
- Shows phase, iteration, event count only
- Minimal screen space, suitable for small terminals

**Quiet Mode:**
- No live display
- Events still persisted to SQLite
- Summary printed after completion

Configuration:
```yaml
logging:
  stream_mode: "detailed"  # compact | detailed | off
```

CLI override:
```bash
duet run --stream-mode compact
duet init --stream-mode detailed
duet run --quiet  # Equivalent to stream_mode: off
```

### Transcript Replay

Replay stored events to review execution:
```bash
# Normal speed replay
duet inspect RUN_ID --replay

# 2x speed
duet inspect RUN_ID --replay --speed 2.0

# Step-by-step
duet inspect RUN_ID --replay --step

# Replay specific phase
duet inspect RUN_ID --replay --phase implement
```

### Export Formats

Export events for analysis:
```bash
# JSONL for streaming analysis
duet inspect RUN_ID --output jsonl | jq 'select(.event_type == "tool_use")'

# CSV for spreadsheet analysis
duet inspect RUN_ID --output csv > events.csv

# Full transcript as JSON
duet inspect RUN_ID --output json > transcript.json
```
```

### 8.2 Adapter Guide

**Event Taxonomy Section:**
```markdown
## Canonical Event Types (Sprint 7)

All adapters emit standardized event types:

| Type | Description | Optional Fields |
|------|-------------|-----------------|
| `assistant_message` | Main response content | `text_snippet` |
| `reasoning` | Thinking/planning step | `text_snippet`, `reasoning_step` |
| `tool_use` | Tool invocation | `tool_info` (name, input, output) |
| `turn_complete` | Turn finished | `usage` (tokens) |
| `parse_error` | JSON parsing failure | `error`, `raw_line` |
| `system_notice` | System notification | `message` |

### Enriched Payload Structure

Events include both raw `payload` and normalized optional fields:

**assistant_message:**
```json
{
  "event_type": "assistant_message",
  "text_snippet": "Here is the implementation...",
  "payload": { "type": "item.completed", "item": {...} },
  "timestamp": "2025-10-18T..."
}
```

**reasoning:**
```json
{
  "event_type": "reasoning",
  "text_snippet": "Analyzing module dependencies...",
  "reasoning_step": 3,
  "payload": {...},
  "timestamp": "..."
}
```

**tool_use:**
```json
{
  "event_type": "tool_use",
  "tool_info": {
    "tool_name": "pytest",
    "status": "running",
    "output_preview": "===== test session starts ====="
  },
  "payload": {...},
  "timestamp": "..."
}
```
```

### 8.3 Integration Plan

**Mark Sprint 7 Complete:**
```markdown
## Sprint 7 – Enhanced Streaming UX *(Complete)*
- **Event Taxonomy**: Canonical event types across all adapters
- **Incremental Content**: Stream accumulator tracks cumulative output
- **Unified Display**: EnhancedStreamingDisplay with detailed/compact/off modes
- **Configurable Verbosity**: stream_mode config + CLI overrides
- **Transcript Replay**: duet inspect --replay with speed control
- **Export Formats**: JSONL and CSV output for analysis
- **Enriched Panels**: Reasoning steps, tool uses, token counts, message previews
- **Tests**: Unit tests for accumulator, display, adapter enrichment
- **Documentation**: Updated guides with streaming modes and replay examples
```

---

## Implementation Tasks

### Phase 1: Event Model (Weeks 1-2)
- [ ] Define canonical event types enum
- [ ] Add optional fields to StreamEvent TypedDict
- [ ] Implement StreamAccumulator class
- [ ] Update Codex adapter to emit enriched events
- [ ] Update Claude adapter to emit enriched events
- [ ] Update Echo adapter for testing
- [ ] Unit tests for event normalization

### Phase 2: Display Component (Weeks 2-3)
- [ ] Implement EnhancedStreamingDisplay class
- [ ] Add detailed mode rendering (all sections)
- [ ] Add compact mode rendering (minimal)
- [ ] Add status derivation logic
- [ ] Add event type formatting
- [ ] Unit tests for rendering modes

### Phase 3: Orchestrator Integration (Week 3)
- [ ] Update _create_event_handler with accumulator
- [ ] Replace StreamingDisplay with EnhancedStreamingDisplay
- [ ] Add stream_mode config support
- [ ] Update CLI --stream-mode flag
- [ ] Integration tests for enriched events

### Phase 4: Replay & Export (Week 4)
- [ ] Implement replay_events function
- [ ] Add --replay flag to duet inspect
- [ ] Add --speed and --step options
- [ ] Implement JSONL export
- [ ] Implement CSV export
- [ ] CLI tests for replay and export

### Phase 5: Testing & Documentation (Week 5)
- [ ] Comprehensive unit tests (accumulator, display, replay)
- [ ] Integration tests (orchestrator, persistence)
- [ ] Smoke tests with real Codex/Claude
- [ ] Update README with modes and replay
- [ ] Update Adapter Guide with taxonomy
- [ ] Update Integration Plan

---

## Success Criteria

Sprint 7 is complete when:
- ✅ Canonical event types defined and used by all adapters
- ✅ StreamAccumulator tracks incremental content
- ✅ EnhancedStreamingDisplay supports detailed/compact/off modes
- ✅ Transcript replay works with speed control
- ✅ JSONL and CSV export formats available
- ✅ All tests passing (target: 110+ tests)
- ✅ Documentation covers new streaming features

---

## Benefits

**For Users:**
- Richer real-time feedback during orchestration
- See reasoning steps and tool invocations as they happen
- Replay past runs to understand decisions
- Export transcripts for analysis and debugging

**For Development:**
- Consistent event model across adapters
- Reusable display component
- Foundation for TUI dashboard (Sprint 8)
- Better observability for troubleshooting

**For Future:**
- Sprint 8 TUI can consume enriched events directly
- Web dashboard gets structured data
- Analytics tools have clean export formats

---

Sprint 7 transforms streaming from "better than nothing" to a core feature that provides genuine insight into the orchestration process.
