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

✅ **Recent Additions**:
- Codex and Claude Code CLI integrations (smoke tested)
- Git operations and commit tracking
- Workflow policies and approval routing
- Review verdict parsing (APPROVE/CHANGES_REQUESTED/BLOCKED)
- Guardrail enforcement (iterations, replans, runtime)
- Feature branch isolation
- JSONL streaming support (Codex)

## Quick Start

### Installation

1. Install [uv](https://docs.astral.sh/uv/) and ensure Python 3.10+ is available
2. Clone the repository:
   ```bash
   git clone https://github.com/femtomc/duet.git
   cd duet
   ```
3. Sync dependencies:
   ```bash
   uv sync --group dev
   ```

### Initialize Your Project Workspace

**Recommended: One-Command Setup**

Navigate to your project and initialize Duet:
```bash
cd /path/to/your/project
uv run /path/to/duet/duet init
```

This automatically creates:
- `.duet/` directory structure
- `.duet/duet.yaml` with production-ready configuration
- `.duet/prompts/` with editable templates (plan, implement, review)
- `.duet/context/context.md` with Codex repository analysis

**Customization**:
```bash
# Review generated config
cat .duet/duet.yaml

# Edit configuration (optional)
vim .duet/duet.yaml

# Customize prompts (optional)
vim .duet/prompts/review.md

# Review repository context
cat .duet/context/context.md
```

**Init Options**:
```bash
uv run duet init --force                # Overwrite existing .duet/
uv run duet init --skip-discovery       # Skip Codex analysis (offline mode)
uv run duet init --model-codex gpt-4    # Customize Codex model
uv run duet init --model-claude opus    # Customize Claude model
```

<details>
<summary><strong>Alternative: Manual Configuration</strong></summary>

If you prefer not to use `duet init`:

```bash
# Copy example config
cp config/duet.example.yaml duet.yaml

# Edit manually
vim duet.yaml
```

Note: Manual setup does not create prompt templates or run context discovery.

</details>

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
uv run python tests/smoke_tests.py --both

# Test individual adapters
uv run python tests/smoke_tests.py --codex
uv run python tests/smoke_tests.py --claude
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
