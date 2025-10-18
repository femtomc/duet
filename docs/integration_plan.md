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
- Provide migration utilities (`duet migrate`) for backfilling JSON-only runs.

## Sprint 6 – Streaming Infrastructure & Observability *(Complete)*
- Replace blocking adapters with streaming JSONL parsing (Codex, Claude Code).
- Add timeout protection to prevent hung CLIs from blocking runs.
- Persist streaming events to SQLite (`events` table).
- Introduce live console output with Rich Live panels and quiet mode.
- Extend CLI commands (`history`, `inspect`) with filtering, event display, and JSON export.
- Update docs and tests to cover streaming behaviour.

## Sprint 7 – Enhanced Streaming UX *(Planned)*
- Enrich live streaming panels with incremental assistant text, reasoning snippets, and detailed token counters.
- Standardize streaming displays across plan/implement/review phases and `duet init`.
- Add configuration for streaming verbosity (compact vs detailed modes).
- Provide transcript replay tooling (e.g., `duet inspect --replay`, JSONL export options).
- Normalize event type names across adapters for consistent downstream handling.
- Expand tests and documentation to showcase the enriched streaming experience.

## Sprint 8 – Hardening and Advanced Resume (Planned)
- Implement resumable runs via the database.
- Add retry/backoff policies, adapter timeouts, and health metrics.
- Strengthen automated coverage for persistence, failure modes, and approval workflows.

Future enhancements (notifications, additional adapters, integration with external ticketing systems) can be scheduled after the Sprint 7/8 milestones.
