# Sprint Planning & Roadmap

This document captures the working roadmap for Duet. It is intentionally living and should be updated whenever priorities shift.

## Completed Milestones

- **Sprint 1 – Orchestrator Foundation**: deterministic PLAN→IMPLEMENT→REVIEW loop, JSON artifacts, base CLI.
- **Sprint 2 – Adapter Integrations**: Codex & Claude adapters, normalised responses, smoke tests.
- **Sprint 3 – Workflow Policies & Git Guardrails**: structured verdicts, git change detection, guardrail configuration, human approval, JSONL logging.
- **Sprint 4 – Workspace Bootstrap**: `duet init`, `.duet/` scaffolding, discovery.
- **Sprint 5 – Persistent History**: SQLite (`runs`, `iterations`, `events`), `duet history`, `duet inspect`, migration tooling.
- **Sprint 6 – Streaming & Observability**: streaming adapters, Rich live UI, event persistence, filtering.
- **Sprint 7 – Enhanced Streaming UX**: richer displays, verbosity settings, transcript replay.
- **Sprint 8 – Stateful CLI Workflow**: checkpoints, `duet next/cont/back`, git baselines, time travel.
- **Sprint 9 – Workflow DSL**: programmable workflows, channel definitions, guard system, loader & template.
- **Sprint 10 – Runtime Integration**: prompt builders, channel store, guard evaluator, workflow executor, channel snapshots.
- **Sprint 11 – Message Persistence** *(in progress)*: messages table, orchestration hooks, CLI visibility, replay support.

## Upcoming Focus

- **Sprint 11 – Message Persistence & Replay**
  - Persist channel updates to SQLite (`messages` table).
  - Expose channel history via CLI (`duet status`, `duet inspect`).
  - Provide replay helpers and document the syndicated workspace.
  - (Optional) CLI export for message history.

- **Sprint 12 – Tooling & Workflow UX**
  - DSL linting and validation (`duet lint-ide`).
  - Hot-reload hints, improved error reporting.
  - Sample workflows, prompt builder documentation.
  - Polished streaming/logging output grouped by channel.

- **Sprint 13 – Migration & Hardening**
  - Legacy-to-DSL migration tooling.
  - Long-run soak tests, profiling, regression coverage.
  - Release notes, upgrade guides, polish for GA.

- **Sprint 14 – Performance & Observability Push**
  - Profile orchestrator + persistence path.
  - Optimise channel serialization/guard evaluation hotspots.
  - Metrics dashboards, logging improvements.
  - Define interfaces for potential native (Rust) services.

- **Sprint 15 – Syndicate Concurrency Prototype**
  - Prototype actor-style concurrent execution.
  - Design channel broker / relay (potentially Rust-backed).
  - Validate with sample workflows and persistence integration.

- **Sprint 16 – Production Concurrency & Scaling**
  - Harden the concurrency model.
  - Tooling and documentation for opt-in high-performance runtime.
  - Ensure compatibility with existing CLI/DSL experience.

## Backlog Ideas

- Notification & chat integrations (Slack, email, ticketing).
- Static analysis / linting agents plugged into the workflow.
- Rich terminal or web dashboard for live runs.
- Advanced analytics on run outcomes and feedback loops.
- Optional cloud persistence backend.

Keep this document concise—capture intent, not exhaustive specs. For detailed design work, spin out dedicated docs.
