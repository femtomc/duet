# Integration Plan

This roadmap tracks major milestones for Duet. Items marked as complete are already in the main branch; upcoming milestones are ordered by priority.

## Sprint 1 – Orchestrator Foundation *(Complete)*
- Implement deterministic PLAN → IMPLEMENT → REVIEW loop.
- Persist checkpoints, iterations, and summaries to disk.
- Provide CLI commands (`run`, `status`, `summary`, `show-config`).
- Add acceptance tests using the echo adapter.

## Sprint 2 – Adapter Integrations *(Complete)*
- Implement Codex and Claude Code adapters against the local CLIs.
- Normalize outputs into `AssistantResponse` with rich metadata.
- Provide smoke tests and adapter documentation.

## Sprint 3 – Workflow Policies and Git Guardrails *(Complete)*
- Support structured review verdicts (APPROVE / CHANGES_REQUESTED / BLOCKED).
- Enforce git change detection and feature-branch isolation.
- Introduce guardrail configuration (iteration caps, replan limits, phase runtime limits).
- Add human approval workflow and JSONL logging.

## Sprint 4 – Workspace Bootstrap (`duet init`) *(Complete)*
- Scaffold `.duet/` with configuration, prompt templates, context notes, logs, and run directories.
- Generate editable prompt templates and repository context via Codex discovery.
- Document the bootstrap workflow and add unit tests for initialization.

## Sprint 5 – Persistent History *(Complete)*
- Introduce SQLite-backed persistence (`.duet/duet.db`) alongside existing JSON artifacts.
- Track runs, iterations, artifacts, and decisions for queryable history.
- Expose CLI helpers (`duet history`, `duet inspect`) powered by the database.
- Provide migration utilities (`duet migrate`) for existing JSON-only runs.

## Sprint 6 – Streaming Infrastructure and Observability *(Complete)*
- **Streaming Adapters**: Replace blocking `subprocess.run` with `subprocess.Popen` for real-time JSONL parsing.
- **Timeout Protection**: Background thread + queue polling ensures hung CLIs are terminated after configured timeout.
- **Event Persistence**: New `events` table in SQLite (schema v2) with migration support.
- **Rich Live Display**: Real-time streaming console output during runs (4fps refresh, transient panels).
- **Quiet Mode**: `--quiet` flag and `logging.quiet` config to disable live display (events still persisted).
- **CLI Enhancements**:
  - `duet history`: Advanced filtering (--phase, --verdict, --since, --until, --contains, --format json)
  - `duet inspect`: Event timeline display (--show-events, --output json)
- **Tests**: 86 passing tests including streaming adapter unit tests and integration tests for event persistence.
- **Documentation**: Updated README and Adapter Guide with streaming API, event types, and usage examples.

## Sprint 7 – Hardening and Advanced Resume (Planned)
- Implement resumable runs via the database.
- Add retry/backoff policies, adapter timeouts, and health metrics.
- Strengthen automated coverage for persistence, failure modes, and approval workflows.

Future items (notifications, additional adapters, integration with external ticketing systems) can be scheduled once the persistence and observability milestones are complete.

---

## Technical Reference: SQLite Events Table (Sprint 6)

### Schema

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    iteration INTEGER,
    phase TEXT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,  -- JSON-encoded event payload
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX idx_events_run_phase ON events(run_id, phase);
CREATE INDEX idx_events_run_iteration ON events(run_id, iteration);
```

### Event Structure

Each streaming event from adapters is stored with:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER | Auto-increment primary key |
| `run_id` | TEXT | Run identifier (foreign key to runs table) |
| `iteration` | INTEGER | Iteration number (nullable) |
| `phase` | TEXT | Phase name: plan/implement/review (nullable) |
| `timestamp` | TEXT | ISO-formatted timestamp (YYYY-MM-DDTHH:MM:SS.mmmmmmZ) |
| `event_type` | TEXT | Event category (see below) |
| `payload` | TEXT | JSON-encoded event data |

### Event Types by Adapter

**Codex Adapter** (JSONL streaming from `codex exec --json`):
- `thread.started` - Execution thread initialized, payload includes `thread_id`
- `turn.started` - Turn beginning
- `item.completed` - Item finished, payload includes `item` object with `type` (reasoning/tool_use/agent_message) and `text`
- `turn.completed` - Turn finished, payload includes `usage` (input_tokens, output_tokens, cached_input_tokens)
- `parse_error` - Malformed JSON line, payload includes `error` message and `raw_line` sample

**Claude Code Adapter** (JSON from `claude --print --output-format json`):
- `output` - Generic output event from Claude Code
- `result` - Final result event containing response data
- `parse_error` - JSON parsing failure, payload includes `error` and `raw_output`

**Echo Adapter**:
- `echo` - Echo event, payload includes `role`, `prompt_length`, `context_keys`

### Querying Events

**Python API** (persistence.py):
```python
from duet.persistence import DuetDatabase

db = DuetDatabase(".duet/duet.db")

# List all events for a run
events = db.list_events(run_id="run-abc123")

# Filter by iteration
events = db.list_events(run_id="run-abc123", iteration=2)

# Filter by phase
events = db.list_events(run_id="run-abc123", phase="implement")

# Filter by event type
events = db.list_events(run_id="run-abc123", event_type="item.completed")

# Count events
count = db.count_events(run_id="run-abc123", phase="plan")
```

**CLI**:
```bash
# Display events grouped by iteration/phase
duet inspect run-abc123 --show-events

# Export full event transcript as JSON
duet inspect run-abc123 --output json > transcript.json

# Hide events, show only iteration summary
duet inspect run-abc123 --no-events
```

### Payload Examples

**thread.started**:
```json
{
  "type": "thread.started",
  "thread_id": "thread_abc123xyz"
}
```

**item.completed (agent_message)**:
```json
{
  "type": "item.completed",
  "item": {
    "id": "item_0",
    "type": "agent_message",
    "text": "Here is the implementation plan..."
  }
}
```

**turn.completed (with usage)**:
```json
{
  "type": "turn.completed",
  "usage": {
    "input_tokens": 1024,
    "output_tokens": 512,
    "cached_input_tokens": 256
  }
}
```

**parse_error**:
```json
{
  "error": "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
  "raw_line": "{invalid json..."
}
```

### Schema Initialization

All tables are created on first database access using `CREATE TABLE IF NOT EXISTS`:
- Fresh databases get the full schema immediately
- Existing databases add any missing tables/indexes automatically
- Simple and idempotent (safe to run multiple times)
