# Duet

Prototype automation tool that coordinates Codex (for design/review) and Claude Code (for implementation).

## Features (Planned)
- Deterministic state machine that loops through plan → implement → review stages.
- Pluggable adapters for Codex and Claude providers.
- CLI to start new orchestration runs, resume from checkpoints, and inspect history.
- Persistent storage of prompts, responses, and commits for auditability.

## Project Layout
```
docs/                    # Design notes and architecture references
src/duet/                # Python package for the orchestrator
pyproject.toml           # Project metadata and dependencies
```

## Getting Started
1. Install [uv](https://docs.astral.sh/uv/) and ensure Python 3.10+ is available (uv can manage interpreters automatically).
2. Sync dependencies (include the `dev` dependency group for tests and tooling):
   ```bash
   uv sync --group dev
   ```
3. Invoke the CLI via uv:
   ```bash
   uv run duet --help
   ```
4. Run the test suite:
   ```bash
   uv run pytest
   ```

## Development Status
This repository currently contains scaffolding and high-level design notes. Integrations with Codex and Claude Code are not yet implemented. Refer to `docs/orchestration_overview.md` for the architectural blueprint and open questions.

> Authentication Note: The orchestrator assumes the Codex and Claude CLIs are already authenticated on the host machine; direct API keys are optional and can be supplied via `api_key_env` only if needed.
