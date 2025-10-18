# Integration Plan

The current codebase ships with an `EchoAdapter` that mirrors prompts. Follow the milestones below to connect the orchestration runtime to real assistants.

## Milestone 1 – Local Validation
- Configure `config/duet.yaml` to point both assistants to the `echo` adapter.
- Run `duet run --config config/duet.yaml` and inspect artifacts under `runs/<run-id>/`.
- Extend prompts in `Orchestrator._compose_request` to include repository-specific context (recent commits, TODO list, etc.).

## Milestone 2 – Adapter API Integration
- Implement `CodexAdapter` and `ClaudeAdapter` classes under `src/duet/adapters/`.
- Reuse the locally authenticated CLI sessions for each assistant; optionally support environment-variable credentials when available.
- Normalize API responses into `AssistantResponse`, populating `metadata` with raw outputs for debugging.
- Ensure adapters surface error conditions explicitly (e.g., raise `AdapterError` on HTTP 4xx/5xx).

## Milestone 3 – Workflow Policies
- Replace `_decide_next_phase` heuristics with policy checks that:
  - Confirm Claude produced commits (via git diff or structured response).
  - Parse Codex review verdicts (`approve`, `changes_requested`, `blocked`).
  - Optionally route to a human approver using notifications (Slack/email) when `requires_human_approval` is true.
- Provide configuration knobs in `duet.yaml` for iteration limits, review thresholds, and merge behaviour.

## Milestone 4 – Git Operations
- Integrate a git library (e.g., `GitPython`) to manage branches per run.
- Ensure Claude operates on a scratch branch and pushes commits for later review.
- Record commit SHAs in the run artifacts and attach diffs to review prompts.

## Milestone 5 – Observability and UX
- Emit structured logs (JSONL) for ingestion into external dashboards.
- Add a `status` command to list historical runs and their terminal states.
- Consider building a lightweight TUI (Textual) or web dashboard for real-time monitoring.

## Milestone 6 – Production Hardening
- Introduce resilience features: retry/backoff, timeouts, heartbeats, metrics.
- Support pausing/resuming runs and migrating state to a persistent database (SQLite/Postgres).
- Add automated tests covering state transitions, adapter failures, and config validation.
