# Duet Kernel Language Migration TODO

We are replacing the v0 workflow DSL with a Scheme-like kernel interpreter that
integrates directly with the syndicated actor runtime. This checklist captures
the implementation work needed to ship the new stack while preserving time-travel
semantics, capability discipline, and multi-client ergonomics.

## 0. Foundations and Planning
- [ ] Ratify the kernel surface (`docs/LANGUAGE_DESIGN.md`) with project stakeholders.
- [ ] Align with control-plane roadmap (CLI bridge, service discovery) so the new
      interpreter slots into upcoming client work.

## 1. Parser, AST, and Module Loader
- [ ] Extend `src/interpreter/parser.rs` to parse kernel forms (modules, macros,
      hygienic identifiers) while retaining source spans.
- [ ] Define kernel AST nodes (modules, definitions, expressions) separate from the
      legacy `ProgramIr`.
- [ ] Implement a module loader with cache invalidation keyed by source hash +
      capability manifest; wire it into interpreter entity startup.

## 2. Evaluator and Continuations
- [ ] Design kernel bytecode or continuation-passing interpreter that supports
      proper tail recursion, closures, and `call/cc`/`prompt`.
- [ ] Ensure captured continuations are serialisable; add serde support so the
      interpreter snapshot can round-trip through the journal.
- [ ] Implement primitive library (arithmetic, data structure ops, records) in Rust
      with host hooks for Syndicate operations.

## 3. Syndicate Primitives
- [ ] Create host-facing primitives for `assert!`, `retract!`, `signal!`,
      `send!`, `watch`, `spawn-facet!`, `spawn-entity!`, `attach-entity!`,
      `invoke-tool!`, `generate-request-id!`, etc., mapping to existing
      `Action`/`WaitCondition` variants.
- [ ] Add wait registration helpers that produce deterministic handles and
      respect existing capability gates.
- [ ] Unit-test each primitive against replay/rewind scenarios to confirm the
      runtime journal captures the right turn outputs.

## 4. Scheduler and Fiber Runtime
- [ ] Replace the single-command interpreter loop with a fiber scheduler that
      can park continuations on waits and resume them deterministically.
- [ ] Update `InterpreterEntity` to persist fiber tables, wait descriptors, and
      prompt state when serialising snapshots/replaying turns.
- [ ] Support multi-wait constructs (`select`, `with-timeout`) by installing and
      retracting wait registrations atomically.

## 5. Session and Capability Management
- [ ] Define dataspace schema for interpreter discovery, session creation, and
      per-client capability attenuation.
- [ ] Implement session lifecycle: spawn per-client facet, mint attenuated
      capabilities, tear down entities on disconnect or rewind.
- [ ] Add request/response stream handlers so clients interact through
      `(session/request …)` and receive `(session/event …)` assertions.

## 6. Standard Library and Compatibility Layer
- [ ] Port existing workflow helpers into kernel modules/macros (planner/worker,
      workspace tooling, reaction management).
- [ ] Provide a transpiler or wrapper that lets `.duet` v0 programs execute via
      the new kernel during transition.
- [ ] Seed new example programs under `.duet/programs` and mirror them in tests.

## 7. Tooling, Tests, and Docs
- [ ] Update CLI commands to compile/load kernel modules, start sessions, and
      stream events through the new dataspace protocol.
- [ ] Expand integration tests to cover branch rewind, snapshot hydrate, and
      multi-client orchestration.
- [ ] Document kernel semantics, primitives, and module system in
      `docs/LANGUAGE_DESIGN.md`; refresh README and workflow docs accordingly.

## 8. Decommission Legacy Workflow IR
- [ ] Remove unused `ProgramIr` builders/parser once kernel rollout completes.
- [ ] Strip legacy workflow examples and docs; point users to the kernel library.
- [ ] Clean up code paths gated by the old interpreter entity.

Progress should be tracked turn-by-turn to ensure reversible execution remains
auditable throughout the migration.
