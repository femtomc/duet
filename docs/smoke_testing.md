# Smoke Testing Guide

This guide describes how to validate Duet against the real Codex and Claude Code CLIs before running production orchestration loops. All automated tests in the repository use mocked subprocess calls, so smoke tests are the definitive check that the local CLIs, authentication, and adapters agree on I/O formats.

## Prerequisites

1. Install and authenticate the CLIs:
   ```bash
   codex auth login
   claude auth login
   ```
2. Verify the binaries are on `PATH`:
   ```bash
   codex --version
   claude --version
   ```
3. Ensure the repository has been bootstrapped with `uv run duet init` so that `.duet/` exists.

## Running the Smoke Suite

```bash
uv run python tests/smoke_tests.py --both       # Codex and Claude
uv run python tests/smoke_tests.py --codex      # Codex only
uv run python tests/smoke_tests.py --claude     # Claude Code only
```

The Codex tests try models in priority order:
1. `CODEX_SMOKE_MODEL` (if set)
2. `model` from `~/.codex/config.toml`
3. Known fallbacks (`gpt-5-codex`, `o3-mini`)

If a model is rejected with “Unsupported model”, the runner automatically advances to the next candidate.

## Test Coverage

### Codex
- **CLI discovery** – confirms `codex` is available via `PATH`.
- **Simple request** – executes `codex exec --json --model <candidate> "<prompt>"` and verifies that the adapter extracts the final `agent_message`.
- **JSONL parsing** – checks that the adapter tolerates mixed event streams and records metadata (event types, token usage).
- **Error handling** – uses an intentionally invalid model to ensure `CodexError` is raised.

### Claude Code
- **CLI discovery** – confirms `claude` is available via `PATH`.
- **Simple request** – calls `claude --print --output-format json --model <model> "<prompt>"` and verifies the response.
- **Workspace context** – ensures the adapter’s `cwd` propagation exposes temporary files to Claude.
- **JSON parsing** – validates that metadata such as `files_modified` is captured.
- **Error handling** – checks that invalid models raise `ClaudeCodeError`.

## Interpreting Results

- **All tests pass** – Both adapters can reach their CLIs, parse responses, and handle errors. You can proceed to production orchestration runs.
- **Specific failures** – Use the table below to diagnose:

| Symptom | Likely Cause | Remedy |
|---------|--------------|--------|
| `Codex CLI not found` | CLI not installed or not on `PATH` | Install or adjust environment |
| `Authentication failed` | CLI session expired | Re-run `codex auth login` / `claude auth login` |
| `Unsupported model` | Requested model unavailable | Set `CODEX_SMOKE_MODEL` or update config |
| `Codex CLI timeout` | Network issues or long-running prompt | Increase adapter timeout or retry |
| JSON decode error | CLI output format changed | Verify CLI flags and update adapter if necessary |

Failures leave detailed output in the console so you can inspect `stderr` or the prompt that was issued.

## Rerunning Discovery

If you want to regenerate the `.duet/context/context.md` repository overview after the smoke suite, run:

```bash
uv run duet init --force
```

This recreates the `.duet/` scaffolding (including prompts and config), so copy any custom edits first.
