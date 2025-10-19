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

- **Sprint 12 – Hot Reload, Error UX & Hardening**
  - Deliver `duet lint` for workflow validation without running the orchestrator.
  - Improve hot-reload hints and surface clean errors when workflow loading fails.
  - Provide sample workflows/prompt builder docs to ease customization.
  - Run acceptance & soak tests; profile long runs, tighten git baseline warnings, and improve adapter failure messaging.

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

### DSL Modernisation Roadmap

| Sprint | Focus | Outcomes |
|--------|-------|----------|
| **DSL‑1 – Object References** | Introduce stable IDs for phases/channels and allow guards/transitions to consume object references while keeping string support for compatibility. | Compiler/runtime accept references; regression tests cover mixed string/object usage. |
| **DSL‑2 – Fluent Phase & Tool API** | Add fluent builders on `Phase` (`with_agent`, `with_tool`, `with_human`, …) and formalise deterministic `Tool` interface. | Phases configured via chaining; compiler persists attached tools/policies; examples updated. |
| **DSL‑3 – Workflow Combinators** | Provide composable dataflow helpers (`and_then`, `if_else`, `branch`, `loop`) to generate transitions automatically. | High-level workflows authored without manual transition lists; integration tests validate graph conversion. |
| **DSL‑4 – Policy Registry Integration** | Centralise approvals/git/replan rules in a policy registry; orchestrator executes tools and enforces policies from registry data. | Legacy metadata translated with warnings; runtime no longer relies on hard-coded phase names. |
| **DSL‑5 – Documentation & Migration** | Refresh docs/templates, supply migration guidance, expand smoke tests, and schedule removal of deprecated APIs. | New DSL showcased; backward compatibility plan communicated; deprecation path documented. |

## Backlog Ideas

- Notification & chat integrations (Slack, email, ticketing).
- Static analysis / linting agents plugged into the workflow.
- Rich terminal or web dashboard for live runs.
- Advanced analytics on run outcomes and feedback loops.
- Optional cloud persistence backend.

Keep this document concise—capture intent, not exhaustive specs. For detailed design work, spin out dedicated docs.
