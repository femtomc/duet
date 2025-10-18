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
| `duet init` | Scaffold `.duet/` (config, prompts, context, logs, runs) |
| `duet run [--config PATH] [--run-id ID]` | Execute the orchestration loop |
| `duet status RUN_ID` | Inspect the latest checkpoint for a run |
| `duet summary RUN_ID [--save]` | Display or persist a run summary |
| `duet show-config [--config PATH]` | Pretty-print the resolved configuration |

All commands accept `--config PATH` to point to an alternate `duet.yaml`. If omitted, the loader searches `.duet/duet.yaml`, `duet.yaml`, and `config/duet.yaml` in precedence order.

## .duet Directory Layout

```
.duet/
├── duet.yaml           # Adapter configuration and workflow guardrails
├── prompts/            # Prompt templates for PLAN / IMPLEMENT / REVIEW
├── context/            # Repository discovery notes
├── runs/               # Run artifacts (checkpoints, iterations, summaries)
├── logs/               # JSONL event stream (optional)
└── duet.db             # Reserved for SQLite persistence (Sprint 5)
```

Artifacts inside `runs/<run-id>/` include:
- `checkpoint.json`: latest `RunSnapshot`
- `iterations/iter-*.json`: structured record for each phase iteration
- `interactions/*.json`: raw prompt/response exchanges
- `summary.json`: aggregated run-level summary

## Running and Monitoring

1. Launch a run with `duet run`. The orchestrator:
   - Creates or checks out a feature branch (`duet/<run-id>` by default)
   - Alternates Codex (plan/review) and Claude Code (implement) phases
   - Enforces guardrails (iteration limits, replan limits, git change detection)
   - Writes artifacts to `.duet/runs/<run-id>/` and logs events via Rich/JSONL

2. Inspect progress at any time:
   ```bash
   uv run duet status feature-x
   uv run duet summary feature-x --save
   ```

3. If a run requires human intervention (e.g., guardrail breach or BLOCKED verdict), the orchestrator creates `.duet/runs/<run-id>/PENDING_APPROVAL`. Review artifacts, clear the flag, and resume with `duet run --run-id <run-id>`.

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

## Documentation

- `docs/orchestration_overview.md`: architecture and workflow
- `docs/adapter_guide.md`: adapter behavior and configuration
- `docs/integration_plan.md`: milestone roadmap
- `docs/smoke_testing.md`: manual validation procedures

## Project Status

- Core orchestration loop, guardrails, git integration, and adapter support are production-ready.
- `duet init` automates workspace scaffolding (Sprint 4).
- Upcoming work (Sprint 5) introduces SQLite-backed persistence and richer historical queries. See `docs/integration_plan.md` for details.
