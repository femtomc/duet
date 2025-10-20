## Facet DSL Migration Plan

Below is the staged roadmap we have been executing for the facet/combinator rewrite.
Each stage lists its primary goals and key deliverables.

### Stage 1 – Facet DSL Surface (complete)
- Implement `FacetBuilder` with `.needs()/.agent()/.tool()/.emit()/.human()`.
- Introduce combinators (`seq`, `loop`, `branch`, `once`, `parallel`).
- Provide registry utilities and unit tests for the builder/combinators.

### Stage 2 – Combinator Compiler & Scheduler Integration (complete)
- Compile `FacetProgram` → `FacetRegistration`.
- Extend `FacetScheduler` with execution policies/guards.
- Add integration tests for registration, triggers, guards.

### Stage 3 – Orchestrator Rebuild
- Replace legacy orchestrator with facet-driven runtime.
- Wire dataspace + scheduler + facet runner loop.
- Persist artifacts/DB entries with facet vocabulary.

### Stage 4 – Workflow Loader & CLI
- Define new workflow module contract (facet programs).
- Implement loader and reconnect `duet run`/`duet lint`.
- Update `duet init` scaffolding and tests.

### Stage 5 – Documentation & Examples
- Rewrite README workflow section.
- Publish facet DSL guide, templates, and sample workflows.
- Refresh manual/smoke tests to use new DSL.

### Stage 6 – Persistence & Logging Updates
- Align DB schema, artifact store, and logger with facets.
- Expose new CLI status/history views based on facets/facts.

### Stage 7 – Cleanup & Observability
- Remove deprecated models/utilities (`TransitionDecision`, channel store, etc.).
- Add telemetry hooks (facet trigger events, fact emissions).
- Finalize unit/integration coverage.

### Stage 8 – Post-Implementation Tasks
- Developer guide for extending the DSL (custom combinators/facts).
- Release notes and version bump once stable.

### Stage 9 – Native Acceleration (new)
- Prototype Rust extensions (via PyO3) for hot paths:
  - Dataspace fact store (assert/query/subscribe).
  - Reactive scheduler queue/trigger evaluation.
  - Optional persistence layer or facet executor hot loops.
- Provide Python bindings and drop-in replacements in orchestration runtime.
- Benchmark end-to-end runs to quantify speedups; document integration steps.

