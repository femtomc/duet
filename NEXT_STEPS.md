# Next Steps – Interpreter & Runtime Evolution

This roadmap captures the work we want to tackle over the coming sprints to
turn Duet’s interpreter into an “Emacs for the runtime”: programmable, live,
and expressive enough to orchestrate fleets of agents.

---

## Guiding Principles

- **Determinism first** – every new capability must record the same journal
  artefacts so time-travel and replay continue to work without special cases.
- **Language features > hard-coded plumbing** – wherever possible we enrich the
  DSL so orchestration logic lives in user programs, not Rust.
- **Ship in vertical slices** – each sprint should leave the interpreter in a
  usable state (e.g., chat-only workflows before tool invocation, functions
  without recursion before we extend the runtime stack).

---

## Sprint Roadmap

### Sprint A – Structured Values & DSL Preparation
- Introduce a `Value` representation (symbols, strings, numbers, lists,
  records) and update `Action::Assert/Log/Send` to consume it.
- Extend the parser/builder to accept literal values instead of raw strings.
- Add minimal helper forms for building preserves records (e.g. `(record :label …)`).
- Tests: IR parsing round-trips, runtime asserts structured values, snapshot
  persistence of value trees.

### Sprint B – Function Declarations (No Await/Recursion Yet)
- Add `(defn name (params…) body…)` and `(call name args…)` syntax.
- Compile functions into inline-expanded bodies (no runtime call stack yet) so
  we get reuse without changing execution semantics.
- Provide a small standard library file (e.g. `lang/agent.duet`) that defines
  `send-agent`, `await-response`, etc., in pure DSL.
- CLI/service: allow `workflow start` to load bundled helper definitions.

### Sprint C – Runtime Call Frames & Real Calls
- Extend the IR with `Call`/`Return` instructions and teach
  `InterpreterRuntime` to maintain a function stack.
- Move from inline expansion to true calls (parameters, locals, return).
- Restrict awaits inside functions initially (fail validation) while we gain
  confidence in the stack.
- Snapshot/resume: serialize call frames so waiting programs hydrate cleanly.

### Sprint D – Richer Control Flow & Awaitable Functions
- Allow functions to perform waits (suspend/resume with stack frames intact).
- Support recursion/tail-calls once suspension logic is battle-tested.
- Add optional `(let …)` / pattern forms to make functions pleasant to write.
- Build the first multi-agent workflow in the DSL using the new helpers; use it
  as the canonical smoke test.

### Sprint E – Tooling & UX Polishing
- Ship a REPL-like `workflow eval` command for iterative development.
- Expose role-binding helpers (`workflow start --planner claude …`) that
  materialise actor/facet IDs and inject them into programs.
- Improve observability: interpreter instance logs, hook registration, Rich
  rendering of call stacks in the CLI.

---

## Cross-Cutting Tasks

- **Documentation** – keep the Implementation Guide and language reference in
  sync with each sprint’s deliverables.
- **Testing** – add interpreter-level integration tests for function calls,
  resumption, and multi-agent chat scenarios.
- **Migration** – refactor existing workflows/tests to use the new DSL helpers
  as they land, retiring Rust-side plumbing (`send-prompt`, etc.).
- **Interpreter polish**
  - Tool invocation path now flows through a dedicated helper in `interpreter/entity.rs`; follow-up: peel observer plumbing into its own helper module once event wiring is complete.
  - Observer registrations now assert `interpreter-observer` records and hydrate cleanly; follow-up: move notification routing into a helper module and surface observer listings via the service/CLI.
  - Spawn capability minted automatically for the interpreter; runtime now enforces `entity/spawn` capability checks (see `entity_spawn_requires_capability`).
  - Document the `tool-error` payload schema and add tests that verify a `tool-result` wake-up end-to-end.
  - Decide whether `interpreter-tool-request` should persist after completion or be retracted automatically.
  - Silence or satisfy remaining `missing_docs` warnings in interpreter IR/types so the lint budget stays clean.
- **Runtime polish**
  - Document the `interpreter-tool-result` assertion format and ensure journal consumers know how to interpret it.
  - Improve capability error reporting (status flag vs raw record) so workflows can branch on success vs failure.
  - Guard against posting tool results to dead facets (log and drop rather than enqueueing useless asserts).

---

## Interpreter Opcode Inventory (2025-02)

| Opcode / Enum                                    | Current Role                                        | Notes & Alignment w/ Syndicate Primitives |
|--------------------------------------------------|------------------------------------------------------|-------------------------------------------|
| `Instruction::Action` (`src/interpreter/ir.rs`)  | Wrapper to run an `Action`                           | Good fit for “turn-assert!/message!” style effects. |
| `Instruction::Await`                             | Suspends on `WaitCondition::Signal/RecordFieldEq`    | Maps to dataspace observe; no support yet for richer patterns. |
| `Instruction::Branch` (`Condition::Signal`)      | Conditional based on a signal                        | Overlaps with `await` + manual state; consider library helper instead. |
| `Instruction::Loop`                              | Local loop                                            | Pure control-flow; keep, but ensure library forms prefer higher-level combinators. |
| `Instruction::Transition`                        | State machine jump                                    | Core primitive analogous to Syndicate state transitions. |
| `Instruction::Call`                              | Function invocation                                   | Runtime supports stack frames; aligns with Scheme-style functions. |
| `Action::Assert` (`Value`)                       | Dataspace assertion                                   | Direct match for `turn-assert!`. |
| `Action::Retract`                                | Dataspace retraction                                  | Implemented by tracking assertion handles; mirrors `turn-retract!`. |
| `Action::Log`                                    | Dataspace log assertion                               | OK; library helper can expose `log`. |
| `Action::InvokeTool { role, capability, tag }`   | Posts `interpreter-tool-request`; awaits `tool-result` | External broker must consume request and call `Control::invoke_capability`. |
| `Action::Send { actor, facet, payload }`         | Enqueue a `TurnOutput::Message`                        | New primitive; mirrors `turn-message!` and underpins future `send!` helper. |
| Wait conditions (`Signal`, `RecordFieldEq`)      | Minimal observe semantics                             | Need richer pattern DSL inspired by Syndicate’s captures. |

**Missing runtime hooks compared to the Syndicate core**

- Message send / sync equivalents (`turn-message!`, `turn-sync!`).
- Facet lifecycle primitives (spawn/stop/link).
- Timer scheduling (`turn-after-seconds!`, `turn-every-seconds!`).
- Capability invocation plumbing (ties into `Action::InvokeTool`).

These gaps define the next thin opcodes we should add before building higher-level helpers (`during`, `send!`, timers, etc.).

---

## Status Tracking

| Sprint | Focus                                | Owner | Status |
|--------|--------------------------------------|-------|--------|
| A      | Structured values & DSL prep         |       | ❌     |
| B      | Function declarations (no awaits)    |       | ❌     |
| C      | Runtime call frames                  |       | ❌     |
| D      | Awaitable functions & recursion      |       | ❌     |
| E      | Tooling, REPL, role-binding helpers  |       | ❌     |

(*Fill in owner/status as we schedule each sprint.*)
