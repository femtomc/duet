# DSL‑1 Implementation Guide — Object References

## Goal
Operate entirely on `Phase` and `Channel` objects instead of string identifiers. String-based APIs are removed in this sprint; workflows must be authored with object references.

## Deliverables
1. `Phase` and `Channel` instances expose stable internal IDs (UUID or deterministic counter) alongside human-readable names.
2. Guards (`When.channel_has`, etc.), transitions, and workflow constructors accept object references as inputs.
3. Compiler and runtime translate object references to IDs/names transparently; string inputs continue to work with deprecation warnings.
4. Regression tests cover mixed string/object usage across DSL loading, orchestration, and CLI commands.

## Work Breakdown

### 1. Core Data Model
- Extend `Channel`/`Phase` dataclasses with internal `id` fields initialised on construction.
- Provide helper methods (`Channel.name`, `Channel.id`), and ensure hashing/equality use `id`.
- Update serialization (`model_dump`, JSON artifacts) to reference `name` for human readability while retaining `id` internally.

### 2. API Surface Updates
- Replace string-based constructors with object-only signatures:
  - `Transition(from_phase: Phase, to_phase: Phase, ...)`.
  - Guard builders accept `Channel` objects exclusively.
- Remove legacy keyword arguments that accept names; update runtime to raise immediately if strings are encountered.
- Require `Workflow(initial_phase=phase_obj, task_channel=channel_obj, ...)`.

### 3. Compiler Integration
- Enforce object references: validate every phase/channel provided is an instance, fail fast otherwise.
- Maintain lookup tables (`id → Phase`, `id → Channel`) for runtime, but no longer map string names.
- Update validations (duplicate detection, wiring checks) to operate on IDs.

### 4. Runtime Adjustments
- Orchestrator/executor, persistence, CLI must consume phases/channels via IDs; any string references should be removed.
- Ensure artifacts and DB rows still store human-readable names for display, but logic depends on IDs.
- Remove fallback logic that inferred phase names for adapter selection; everything comes from `Phase` objects.

### 5. Testing
- Unit tests covering `Phase`/`Channel` ID/equality semantics.
- DSL loader tests using object-only workflows; confirm string usage raises `TypeError`.
- Integration test defining and running a workflow without any string references.
- Update existing tests to use object references (sweeping refactor needed).

## Risks & Notes
- **Breaking Change**: all existing workflows must be updated; schedule refactor across codebase immediately.
- **Tooling impact**: IDE/editor support improves; ensure docs/templates updated the same sprint.
- **Persistence**: schema unchanged but confirm CLI renders names correctly after ID shift.
