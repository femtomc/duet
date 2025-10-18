<div align="center">
  <img src="logo-circle.png" alt="Duet Logo" width="200"/>

  # Duet

  **Automate the Codex ↔ Claude Code loop**
</div>

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
   This creates `.duet/` with configuration, workflow definition (DSL), context notes, and run/log directories. Use `--help` for additional options (custom models, skip discovery, alternate location).

3. **Review generated files**
   - `.duet/duet.yaml`: adapter configuration and workflow guardrails
   - `.duet/workflow.py`: workflow definition using Python DSL (Sprint 9)
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

### Core Commands

| Command | Description |
|---------|-------------|
| `duet init` | Scaffold `.duet/` (config, workflow DSL, context, logs, runs, SQLite DB) |
| `duet run [--config PATH] [--run-id ID] [--quiet]` | Execute the full orchestration loop with live streaming output |
| `duet status RUN_ID [--show-states]` | Inspect run status and state history (Sprint 8) |
| `duet summary RUN_ID [--save]` | Display or persist a run summary (filesystem) |
| `duet history [OPTIONS]` | List recent runs with advanced filtering and JSON export |
| `duet inspect RUN_ID [OPTIONS]` | Show per-iteration details with streaming events and JSON export |
| `duet migrate [--force]` | Backfill SQLite database from existing artifacts |
| `duet show-config [--config PATH]` | Pretty-print the resolved configuration |

### Stateful Workflow Commands (Sprint 8)

| Command | Description |
|---------|-------------|
| `duet next [FEEDBACK] [--run-id ID]` | Execute next phase (auto-resumes most recent run, optional feedback) |
| `duet cont RUN_ID [--max-phases N]` | Continue executing phases until done or blocked |
| `duet back STATE_ID [--force]` | Restore git workspace and database to a previous state |

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
├── workflow.py         # Workflow definition using Python DSL (Sprint 9)
├── context/            # Repository discovery notes
├── runs/               # Run artifacts (checkpoints, iterations, summaries)
├── logs/               # JSONL event stream (optional)
└── duet.db             # SQLite persistence (runs, iterations, events, states)
```

Artifacts inside `runs/<run-id>/` include:
- `checkpoint.json`: latest `RunSnapshot`
- `iterations/iter-*.json`: structured record for each phase iteration
- `interactions/*.json`: raw prompt/response exchanges
- `summary.json`: aggregated run-level summary

SQLite tables (Sprint 5, 6 & 8):
- `runs`: Run metadata (phase, iteration, git refs, timestamps, active_state_id)
- `iterations`: Per-iteration details (prompts, responses, verdicts, tokens, git changes)
- `events`: Streaming events from adapters (event_type, payload, timestamps)
- `run_states`: State checkpoints (state_id, phase_status, baseline_commit, parent_state_id, feedback)

## Stateful Workflow (Sprint 8)

Sprint 8 introduces a **stateful CLI workflow** that allows you to execute orchestration runs one phase at a time, rewind to previous states, and provide feedback between phases.

### State-Based Execution

Each run maintains a series of **checkpoints** (states) that track:
- Phase status (e.g., `plan-ready`, `plan-complete`, `implement-ready`, `done`, `blocked`)
- Git baseline commit for each state
- User feedback provided at each step
- Parent-child relationships for state history

### Workflow Commands

1. **Start or continue a run phase-by-phase**:
   ```bash
   # Create new run and execute first phase
   duet next

   # Auto-resume most recent run
   duet next

   # Provide feedback (auto-resumes)
   duet next "Try variant B"

   # Target specific run with feedback
   duet next "Fix error handling" --run-id run-20251018-142030
   ```

2. **Auto-continue until completion or blocking**:
   ```bash
   # Execute all phases until done or blocked
   duet cont run-20251018-142030

   # Limit to 5 phases max
   duet cont run-20251018-142030 --max-phases 5
   ```

3. **Inspect run status and state history**:
   ```bash
   # View current state and history
   duet status run-20251018-142030

   # Hide state history
   duet status run-20251018-142030 --no-states
   ```

4. **Rewind to a previous state**:
   ```bash
   # Restore git workspace and database to a checkpoint
   duet back run-20251018-142030-plan-complete

   # Force restore even with uncommitted changes
   duet back run-20251018-142030-plan-complete --force
   ```

### State ID Pattern

States follow the pattern: `<run-id>-<phase-status>`

Examples:
- `run-20251018-142030-plan-ready`
- `run-20251018-142030-plan-complete`
- `run-20251018-142030-implement-ready`
- `run-20251018-142030-done`
- `run-20251018-142030-blocked`

### Use Cases

- **Incremental workflow**: Execute one phase at a time, review results, provide feedback
- **Experimentation**: Rewind to a previous state and try different approaches
- **Debugging**: Restore to a checkpoint to investigate what went wrong
- **Manual approval**: Pause between phases to review before proceeding
- **Feedback loops**: Provide specific guidance after plan or review phases

## Workflow DSL (Sprint 9)

Sprint 9 introduces a **Python DSL** for defining workflows programmatically, replacing legacy prompt templates with a type-safe, channel-based messaging system.

### Channel-Based Messaging

Workflows now use **channels** for structured communication between phases:

```python
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    agents=[
        Agent(name="planner", provider="codex", model="gpt-5-codex"),
        Agent(name="implementer", provider="claude", model="sonnet"),
    ],
    channels=[
        Channel(name="task", schema="text"),
        Channel(name="plan", schema="text"),
        Channel(name="code", schema="git_diff"),
    ],
    phases=[
        Phase(name="plan", agent="planner",
              consumes=["task"], publishes=["plan"]),
        Phase(name="implement", agent="implementer",
              consumes=["plan"], publishes=["code"]),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
    ],
)
```

### Key Concepts

**Agents** - AI models that execute phases (Codex, Claude Code, custom)

**Channels** - Communication pathways carrying typed data (text, JSON, git diffs, verdicts)

**Phases** - Workflow steps that consume from channels and publish results
- `consumes: List[str]` - Input channels
- `publishes: List[str]` - Output channels
- `description: str` - Human-readable purpose

**Transitions** - Conditional state changes with guard predicates
- `when: Guard` - Condition that must evaluate to True
- `priority: int` - For conflict resolution (higher = preferred)

**Guards** - Predicates for transition conditions
- `When.always()` - Always fires
- `When.channel_has("verdict", "approve")` - Check channel value
- `When.git_changes(required=True)` - Verify git changes
- `When.all(guard1, guard2)` - Boolean AND
- `When.any(guard1, guard2)` - Boolean OR
- `When.not_(guard)` - Boolean NOT

### Customization

Edit `.duet/workflow.py` to customize your workflow:
- Add/remove agents for different models
- Define custom channels for domain-specific data
- Configure phase dependencies (consumes/publishes)
- Set conditional transitions with guards
- Adjust priorities for deterministic routing

See `docs/workflow_dsl.md` for comprehensive DSL reference.

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
- **Sprint 6**: Streaming infrastructure with real-time console output, event persistence, advanced CLI filtering, and JSON export.
- **Sprint 7**: Enhanced streaming displays with configurable modes (detailed/compact/off).
- **Sprint 8**: Stateful CLI workflow with phase-by-phase execution, state checkpoints, git baseline management, and time-travel commands (`duet next`, `duet cont`, `duet back`, `duet status`).
- **Sprint 9** (Complete): Python DSL for workflow definitions with channel-based messaging, guard system for conditional transitions, semantic validation compiler, and dynamic workflow loader. Replaces legacy prompt templates with `.duet/ide.py` declarative workflows.
- **Upcoming**: Runtime integration (channel payload routing), orchestrator DSL wiring, TUI dashboard, `jj` backend support. See `docs/integration_plan.md` for roadmap.
