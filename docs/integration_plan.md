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

## Sprint 5 – Persistent History (Planned)
- Introduce SQLite-backed persistence (`.duet/duet.db`) alongside existing JSON artifacts.
- Track runs, iterations, artifacts, and decisions for queryable history.
- Expose CLI helpers (`duet history`, `duet inspect`) powered by the database.
- Provide migration utilities for existing JSON-only runs.

## Sprint 6 – Observability and UX Enhancements (Planned)
- Expand CLI ergonomics (filtered run listings, richer summaries).
- Optional TUI or web status dashboard consuming the SQLite data.
- Structured log export for external monitoring systems.

## Sprint 7 – Hardening and Advanced Resume (Planned)
- Implement resumable runs via the database.
- Add retry/backoff policies, adapter timeouts, and health metrics.
- Strengthen automated coverage for persistence, failure modes, and approval workflows.

Future items (notifications, additional adapters, integration with external ticketing systems) can be scheduled once the persistence and observability milestones are complete.
