# Duet

Workflow orchestrator that automates the collaboration between Codex (planning/review) and Claude Code (implementation).

## Features

✅ **Implemented**:
- Deterministic state machine (PLAN → IMPLEMENT → REVIEW → DONE/BLOCKED)
- Pluggable adapter system (Echo, Codex, Claude Code)
- CLI commands: `run`, `status`, `summary`, `show-config`
- Structured artifact persistence with iteration tracking
- Checkpoint-based resumability
- Comprehensive logging (rich console + optional JSONL)
- Extensive test coverage (unit + integration tests)
- Cross-platform compatibility (Windows, macOS, Linux)

🚧 **In Progress**:
- Real Codex and Claude Code CLI integrations
- Git operations and commit tracking
- Advanced workflow policies and approval routing

## Quick Start

### Installation

1. Install [uv](https://docs.astral.sh/uv/) and ensure Python 3.10+ is available
2. Sync dependencies:
   ```bash
   uv sync --group dev
   ```

### Configuration

Create `duet.yaml` from the example:
```bash
cp config/duet.example.yaml duet.yaml
```

Edit `duet.yaml` to configure your adapters:
```yaml
# Development mode (using echo adapters)
codex:
  provider: "echo"
  model: "gpt-4"

claude:
  provider: "echo"
  model: "claude-sonnet-4"

# Production mode (using real CLIs)
# codex:
#   provider: "codex"
#   model: "gpt-4"
#   temperature: 0.2
#
# claude:
#   provider: "claude-code"
#   model: "claude-sonnet-4"
#   temperature: 0.1
```

### Usage

**Run orchestration**:
```bash
uv run duet run [--config duet.yaml] [--run-id my-run]
```

**Check run status**:
```bash
uv run duet status <run-id>
```

**View run summary**:
```bash
uv run duet summary <run-id> [--save]
```

**Show configuration**:
```bash
uv run duet show-config
```

### Testing

**Automated tests** (mocked, no real CLI required):
```bash
uv run pytest
```

**Manual acceptance test** (echo adapter):
```bash
uv run python tests/manual_test.py
```

**Smoke tests** (requires real CLIs - run before production):
```bash
# Test both Codex and Claude Code adapters
python tests/smoke_tests.py --both

# Test individual adapters
python tests/smoke_tests.py --codex
python tests/smoke_tests.py --claude
```

See [Smoke Testing Guide](docs/smoke_testing.md) for details.

## Architecture

```
src/duet/
├── adapters/          # Pluggable assistant adapters
│   ├── base.py        # Abstract adapter interface & registry
│   ├── echo.py        # Echo adapter (testing/development)
│   ├── codex.py       # Codex adapter (planning/review)
│   └── claude_code.py # Claude Code adapter (implementation)
├── artifacts.py       # Artifact storage & persistence
├── cli.py             # CLI commands (Typer)
├── config.py          # Configuration models (Pydantic)
├── logging.py         # Structured logging (rich + JSONL)
├── models.py          # Domain models (Phase, Request, Response, etc.)
└── orchestrator.py    # Core orchestration loop & state machine
```

## Documentation

- **[Adapter Guide](docs/adapter_guide.md)**: Configure and implement adapters
- **[Orchestration Overview](docs/orchestration_overview.md)**: Architecture and design
- **[Integration Plan](docs/integration_plan.md)**: Development roadmap

## Adapters

### Echo Adapter
For testing and development without API calls. Mirrors prompts back.

### Codex Adapter
Invokes Codex CLI for planning and review tasks. Requires local authentication.

### Claude Code Adapter
Invokes Claude CLI for implementation tasks. Requires local authentication.

See [Adapter Guide](docs/adapter_guide.md) for details.

## Authentication

The orchestrator uses **local CLI authentication**:
- **Codex**: Authenticate via `codex auth login`
- **Claude Code**: Authenticate via `claude auth login`
- **Echo**: No authentication required (testing only)

No API keys are stored in configuration files.

## Project Status

**Current Milestone**: Milestone 2 - Adapter API Integration
**Next Milestone**: Milestone 3 - Workflow Policies & Approval Routing

See [Integration Plan](docs/integration_plan.md) for the complete roadmap.
