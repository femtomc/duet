# Duet

Duet orchestrates iterative software delivery by coordinating Codex for planning and review with Claude Code for implementation. It automates the plan → implement → review loop, enforces guardrails, and preserves artifacts for observability and audit.

## Requirements

- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management
- Codex CLI (authenticated via `codex auth login`)
- Claude Code CLI (authenticated via `claude auth login`)
- Git repository for the target workspace

## Quick Start

1. **Install dependencies**
   ```bash
   uv sync --group dev
   ```

2. **Bootstrap the workspace**
   ```bash
   uv run duet init
   ```
   This creates `.duet/` with configuration, prompt templates, context notes, and run/log directories. Use `--help` for additional options (custom models, skip discovery, alternate location).

3. **Review generated files**
   - `.duet/duet.yaml`: adapter configuration and workflow guardrails
   - `.duet/prompts/*.md`: editable prompt templates for each phase
   - `.duet/context/context.md`: repository overview generated during init

4. **Run smoke tests (recommended before production)**
   ```bash
   uv run python tests/smoke_tests.py --both
   ```

5. **Start an orchestration run**
   ```bash
   uv run duet run --run-id feature-x
   ```

## CLI Reference

| Command | Description |
|---------|-------------|
| `duet init` | Scaffold `.duet/` (config, prompts, context, logs, runs, SQLite DB) |
| `duet run [--config PATH] [--run-id ID] [--quiet]` | Execute the orchestration loop with live streaming output |
| `duet status RUN_ID` | Inspect the latest checkpoint for a run (filesystem) |
| `duet summary RUN_ID [--save]` | Display or persist a run summary (filesystem) |
| `duet history [OPTIONS]` | List recent runs with advanced filtering and JSON export |
| `duet inspect RUN_ID [OPTIONS]` | Show per-iteration details with streaming events and JSON export |
| `duet migrate [--force]` | Backfill SQLite database from existing artifacts |
| `duet show-config [--config PATH]` | Pretty-print the resolved configuration |

### Advanced Filtering (Sprint 6)

**duet history** supports:
- `--phase PHASE` - Filter by phase (plan/implement/review/done/blocked)
- `--verdict VERDICT` - Filter by review verdict (approve/changes_requested/blocked)
- `--since YYYY-MM-DD` - Runs created after date
- `--until YYYY-MM-DD` - Runs created before date
- `--contains TEXT` - Search in run notes
- `--format json` - Export as JSON
- `--limit N` - Max results (default: 20)

**duet inspect** supports:
- `--show-events` / `--no-events` - Display streaming event timeline (default: show)
- `--output json` - Export run + iterations + events as JSON

**duet run** supports:
- `--quiet` / `-q` - Disable live streaming console output (events still persisted)

All commands accept `--config PATH` to point to an alternate `duet.yaml`. If omitted, the loader searches `.duet/duet.yaml`, `duet.yaml`, and `config/duet.yaml` in precedence order.

## .duet Directory Layout

```
.duet/
├── duet.yaml           # Adapter configuration and workflow guardrails
├── prompts/            # Prompt templates for PLAN / IMPLEMENT / REVIEW
├── context/            # Repository discovery notes
├── runs/               # Run artifacts (checkpoints, iterations, summaries)
├── logs/               # JSONL event stream (optional)
└── duet.db             # SQLite persistence (runs, iterations, events, stats)
```

Artifacts inside `runs/<run-id>/` include:
- `checkpoint.json`: latest `RunSnapshot`
- `iterations/iter-*.json`: structured record for each phase iteration
- `interactions/*.json`: raw prompt/response exchanges
- `summary.json`: aggregated run-level summary

SQLite tables (Sprint 5 & 6):
- `runs`: Run metadata (phase, iteration, git refs, timestamps)
- `iterations`: Per-iteration details (prompts, responses, verdicts, tokens, git changes)
- `events`: Streaming events from adapters (event_type, payload, timestamps)

## Running and Monitoring

1. **Launch a run** with `duet run`. The orchestrator:
   - Creates or checks out a feature branch (`duet/<run-id>` by default)
   - Alternates Codex (plan/review) and Claude Code (implement) phases
   - Enforces guardrails (iteration limits, replan limits, git change detection)
   - **Displays live streaming output** (Sprint 6) showing real-time events, tokens, and progress
   - Writes artifacts to `.duet/runs/<run-id>/` and logs events via Rich/JSONL

2. **Inspect progress** at any time:
   ```bash
   # View current checkpoint
   uv run duet status feature-x

   # Query run history with filters
   uv run duet history --phase blocked --since 2025-10-01
   uv run duet history --verdict approve --format json

   # Inspect detailed iteration timeline with streaming events
   uv run duet inspect feature-x --show-events
   uv run duet inspect feature-x --output json > transcript.json

   # Generate summary
   uv run duet summary feature-x --save
   ```

3. **Quiet mode** for non-interactive environments:
   ```bash
   duet run --quiet  # Disable live display, events still persisted
   ```
   Or configure in `.duet/duet.yaml`:
   ```yaml
   logging:
     quiet: true
   ```

4. If a run requires human intervention (e.g., guardrail breach or BLOCKED verdict), the orchestrator creates `.duet/runs/<run-id>/PENDING_APPROVAL`. Review artifacts, clear the flag, and resume with `duet run --run-id <run-id>`.

## Streaming Observability (Sprint 6)

Duet now provides **real-time visibility** into adapter execution:

### Live Console Output

During `duet run`, a Rich Live panel displays:
- Current phase (PLAN/IMPLEMENT/REVIEW) and iteration number
- Total streaming events received
- Rolling log of last 5 events with timestamps
- Event-specific formatting: ✓ completed, … reasoning, ✗ errors
- Token counts from turn completion events

The display refreshes 4 times per second and automatically clears when the phase completes.

### Event Persistence

All streaming events are persisted to the SQLite `events` table with:
- `run_id`, `iteration`, `phase` - Execution context
- `event_type` - Event category (thread.started, item.completed, turn.completed, parse_error, etc.)
- `payload` - JSON-encoded event data (usage, item details, error info)
- `timestamp` - ISO-formatted event timestamp

Query events using `duet inspect`:
```bash
# Show events grouped by iteration/phase
duet inspect run-abc123 --show-events

# Export full transcript
duet inspect run-abc123 --output json > transcript.json

# Hide events, show only iteration summary
duet inspect run-abc123 --no-events
```

### Filtering and Export

Advanced querying with `duet history`:
```bash
# Find all blocked runs from October
duet history --phase blocked --since 2025-10-01

# Export approved runs as JSON
duet history --verdict approve --format json

# Search for timeout issues
duet history --contains "timeout" --limit 50

# Combine filters
duet history --phase review --verdict changes_requested --since 2025-10-15
```

## Testing and Validation

- **Unit and integration tests**
  ```bash
  uv run pytest
  ```
- **Manual echo loop (offline acceptance)**
  ```bash
  uv run python tests/manual_test.py
  ```
- **Smoke tests (real CLIs)**
  ```bash
  uv run python tests/smoke_tests.py --codex
  uv run python tests/smoke_tests.py --claude
  ```
  The smoke suite auto-selects supported Codex models (honoring `CODEX_SMOKE_MODEL` if set).
  Run these before production orchestration to confirm local CLI compatibility.

## Documentation

- `docs/orchestration_overview.md`: architecture and workflow
- `docs/adapter_guide.md`: adapter behavior and configuration
- `docs/integration_plan.md`: milestone roadmap
- `docs/smoke_testing.md`: manual validation procedures

## Project Status

- **Sprint 1–4**: Core orchestrator, adapters, guardrails, and `duet init` workspace bootstrapping.
- **Sprint 5**: SQLite persistence layer, CLI history/inspect commands, artifact migration.
- **Sprint 6** (Complete): Streaming infrastructure with real-time console output, event persistence, advanced CLI filtering, and JSON export.
- **Upcoming**: Advanced resume/hardening (Sprint 7), TUI dashboard. See `docs/integration_plan.md` for roadmap.
