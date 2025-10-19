# Sprint DSL‑1 – Object References Implementation Plan

## Objective
Eliminate string-based workflow wiring. All DSL constructs (phases, channels, guards, transitions, workflow init) must operate exclusively on object references with stable IDs.

## Step-by-Step Plan

### 1. Data Model & Utilities
1. Introduce `BaseElement` mixin providing:
   - `id: UUID`
   - `name: str`
   - `__hash__` / `__eq__` based on `id`
2. Update `Channel` and `Phase` to inherit from `BaseElement`; audit constructors.
3. Add helper functions for UI display (`display_name`), if needed.

### 2. DSL API Refactor
1. Change signatures:
   - `Transition(from_phase: Phase, to_phase: Phase, when: Guard | None = None, ...)`
   - `Workflow(..., initial_phase: Phase, task_channel: Channel | None = None)`
   - Guard builders (`When.channel_has(channel: Channel, value)`).
2. Remove overloads accepting strings; delete related tests/helpers.
3. Update DSL modules (`duet/dsl/__init__.py`, guard implementations) to match the new signatures.

### 3. Compiler Rewrite
1. Entry validation: ensure all phases/channels/agents are proper instances; raise `TypeError` otherwise.
2. Build maps:
   - `phase_by_id`, `phase_by_name`
   - `channel_by_id`, `channel_by_name`
3. Convert transitions and guards using IDs; store mapping for runtime.
4. Adjust error messages to reference phase/channel names via lookup tables.

### 4. Runtime Integration
1. Orchestrator/executor:
   - Replace string lookups with `WorkflowGraph` ID-based methods.
   - Adapter selection uses `Phase.agent` and metadata only.
   - Channel seeding and updates done by object references (through IDs in channel store).
2. Persistence & CLI:
   - Ensure database writes still contain human-readable names, but retrieval relies on graph lookups.
   - Update CLI renders (`duet status`, etc.) to pull names from graph map.

### 5. Channel Store & Guards
1. Channel store keyed by channel IDs; expose convenience accessors that accept channel objects.
2. Guard evaluator expects guard definitions referencing channel IDs.
3. Update tool interfaces (if any) to use channel objects/IDs consistently.

### 6. Test Suite Update
1. Sweep DSL tests: replace string references with objects.
2. Update workflow fixtures to use object-based API.
3. Integration tests: run canonical workflows to ensure orchestrator works end-to-end.
4. Remove/adjust tests expecting string compatibility errors.

### 7. Documentation & Templates (same sprint)
1. Rewrite `docs/workflow_dsl.md` to demonstrate the new object-based API.
2. Update `duet init` template workflow to use object references.
3. Add migration note in README/changelog highlighting the breaking change.

### 8. Cleanup & QA
1. Search for remaining string comparisons in codebase (`rg '"plan"'`, etc.).
2. Run full test suite + smoke tests (`uv run pytest`, `duet run` on sample workflows).
3. Prepare release notes summarizing breaking API change.

## TODO Tracker

### Data Model
- [ ] Implement `BaseElement` mixin with UUID ID, equality, hashing.
- [ ] Refactor `Channel` to inherit from `BaseElement`.
- [ ] Refactor `Phase` to inherit from `BaseElement`.
- [ ] Ensure serialization keeps human-readable `name` fields.

### API Surface
- [ ] Update `Transition` constructor to require `Phase` instances.
- [ ] Update guard builders (`When.*`) to require `Channel` instances.
- [ ] Change `Workflow` constructor to require object refs (`initial_phase`, `task_channel`, etc.).
- [ ] Remove any remaining string-based helper overloads.

### Compiler
- [ ] Validate inputs are `Phase`/`Channel` instances; raise `TypeError` otherwise.
- [ ] Build ID-based lookup tables for phases/channels.
- [ ] Update transition/guard compilation to operate on IDs.
- [ ] Update validation errors to use names via lookup tables.

### Runtime
- [ ] Update orchestrator/executor to fetch phase/channel info via IDs.
- [ ] Remove legacy adapter fallbacks that relied on phase names.
- [ ] Ensure channel store uses channel IDs internally.
- [ ] Adjust persistence/CLI to render names while using IDs for logic.

### Tests
- [ ] Sweep DSL tests to replace string references with object refs.
- [ ] Update workflow fixtures in `tests/fixtures/` to object-based API.
- [ ] Update integration tests (CLI/orchestrator) to new API.
- [ ] Remove/adjust tests that assumed string compatibility.

### Documentation & Templates
- [ ] Rewrite `docs/workflow_dsl.md` examples to object-based syntax.
- [ ] Update `duet init` workflow template.
- [ ] Add migration note / changelog entry describing breaking change.

### QA
- [ ] Run full pytest suite (`uv run pytest`).
- [ ] Execute smoke tests with sample workflows via `duet run`.
- [ ] Draft release notes summarising DSL‑1 changes.
