# Duet Implementation Guide (Spring 2024 Refresh)

This guide captures the current design of the Duet runtime and the plan for
evolving it into a programmable, Emacs-style environment for multi-agent
automation. It replaces the early bring-up notes while keeping the same
deterministic Syndicated Actor foundation.

---

## 1. Vision & Guiding Principles

- **Determinism everywhere** – every observable effect (assertion, message,
  external request) is recorded as a turn input/output so replay, time-travel,
  and branching remain exact.
- **Dataspace-first state** – entities should publish durable assertions rather
  than keeping private mutable state; hydration exists for the rare cases that
  need it.
- **Programmable orchestration** – the interpreter should let users express
  workflows in the DSL instead of wiring behaviour directly in Rust. Our north
  star is an Emacs-like experience for the runtime.
- **Vertical slices** – deliver features in end-to-end increments (e.g.,
  structured values + tests in one sprint) rather than scattering partially
  finished subsystems.

---

## 2. System Overview

```
duet/
├── src/
│   ├── runtime/        # Syndicated Actor core
│   ├── codebase/       # Built-in entities (workspace, agents, transcripts)
│   └── interpreter/    # DSL parser, IR, runtime, protocol
├── python/duet/        # CLI & NDJSON control client
├── tests/              # Integration & feature tests
└── docs/               # Design notes and guides
```

### 2.1 Runtime Core (`src/runtime`)
- **`actor.rs`** – actors, facets, activation contexts, interpreter wait
  handling; enforces flow control, patterns, reactions.
- **`state.rs`** – CRDT sets/maps/counters plus `StateDelta`.
- **`scheduler.rs`** – deterministic ready queue with per-actor credit limits.
- **`journal.rs` / `snapshot.rs`** – persistent storage; recovery verified by
  tests.
- **`branch.rs`** – branch DAG, rewind, merge scaffolding (CRDT join-based).
- **`service.rs`** – NDJSON control-plane dispatcher used by the CLI; now
  includes interpreter awareness.

### 2.2 Codebase Entities (`src/codebase`)
- **Workspace** – publishes filesystem view + capabilities for read/write.
- **Transcripts** – records `agent-request`/`agent-response` pairs; CLI renders
  them with branch/turn metadata.
- **Agents** – Claude Code stub that converts requests into external process
  invocations and posts deterministic responses.

### 2.3 Interpreter (`src/interpreter`)
- **Parser/IR** – S-expression parser feeding a typed state-machine IR
  (`ProgramIr`, `State`, `Instruction`, `Action`).
- **Runtime** – executes IR in deterministic ticks, now capable of pausing on
  waits and hydrating snapshots (including wait handles).
- **Protocol** – dataspace schemas (`interpreter-definition`, `interpreter-instance`,
  `interpreter-wait`) used by the service/CLI.
- **Entity** – registers interpreter instances as hydratable entities; records
  definitions, runs programs, and resumes waiting snapshots.

### 2.4 CLI (`python/duet`)
- NDJSON client with Rich output for status, transcript display, workspace
  inspection, reaction management, and now `workflow` commands that interact
  with interpreter definitions/instances.

---

## 3. Interpreter Language Roadmap

The interpreter currently offers a minimal DSL: `(workflow …)`, `(roles …)`,
`(state …)`, primitive actions (`send-prompt`, `emit`, `assert`, `invoke-tool`
stub), waits (`record`, `signal`), branches, loops, and goto. To
reach the Emacs-style goal we will expand the language over the next sprints.

| Phase | Focus | Key Deliverables | Status |
|-------|-------|------------------|--------|
| A | Structured values | `Value` enum, literal syntax, record helpers, tests | Planned |
| B | Function declarations | `(defn …)`, `(call …)` compiled via inline expansion, bundled helper library | Planned |
| C | Runtime call frames | IR `Call`/`Return`, interpreter stack, no-await functions | Planned |
| D | Awaitable functions | Stack-aware suspension, recursion, let/pattern helpers, canonical multi-agent workflow | Planned |
| E | Tooling/UX | REPL-style eval, role-binding helpers, richer CLI visualisations | Planned |

Each phase corresponds to a sprint in `NEXT_STEPS.md`. We only advance to the
next phase when the current one is fully tested (unit + integration) and
documented.

---

## 4. Module Responsibilities & Status

### 4.1 Runtime Core
- **Scheduler** – ready queue & flow control (✅ complete; tuning ongoing).
- **Journal & snapshots** – crash recovery and replay (✅ complete; monitor size
  growth once workflows intensify).
- **Patterns & reactions** – run-time subscriptions & templated actions (✅
  usable; future work includes match-field helpers and metrics).
- **Service** – control facade, interpreter list/start commands (✅); remaining
  work: resume/cancel commands, richer error codes.

### 4.2 Interpreter Entity
- Definitions persisted in dataspace (✅).
- Wait suspension/resume with hydrations (✅).
- Tool invocation (`Action::InvokeTool`) – 🚧 unimplemented; required when we
  integrate codebase edits.
- Prompt semantics – currently emits `interpreter-prompt`; forthcoming language
  work will move agent plumbing into DSL helper functions.

### 4.3 CLI
- Workflow list/start wired to interpreter protocol (✅).
- Needs follow-ups: workspace-aware role binding, workflow instance controls,
  and optional REPL command once functions land.

---

## 5. Testing & Tooling

- `cargo test` covers runtime CRDTs, scheduler, interpreter hydration, service
  commands, and reactions. Continue adding interpreter-focused integration
  tests as new language features arrive (structured values, function calls,
  multi-agent workflows).
- Python CLI linting: `python3 -m compileall python` included in the workflow;
  consider adding unit tests for CLI formatters as functionality grows.
- Add golden transcripts for interpreter programs to ensure CLI/service outputs
  remain stable.

---

## 6. Open Design Questions

1. **Value semantics** – Do we support arithmetic/comparison in the DSL or rely
   on host-provided helpers?
2. **Await inside functions** – When we introduce resumable call stacks, do we
   allow arbitrary awaits or restrict them to tail positions?
3. **Hook system** – Expose runtime events (turn commit, branch switch,
   assertion change) as interpreter hooks? Needs determinism review.
4. **Capability plumbing** – How should interpreter code acquire capabilities
   safely? Define a standard library for capability discovery.
5. **Security / sandboxing** – Once workflows can spawn external tools we must
   revisit capability attenuation and CLI permissions.

---

## 7. References

- `NEXT_STEPS.md` – sprint-by-sprint roadmap.
- `docs/workflows.md` – language sketches; update alongside DSL changes.
- `tests/interpreter_runtime.rs` – current smoke test demonstrating wait/resume
  flow (chat-only scenario).

Keep this guide current whenever we land meaningful architecture changes or
finish a roadmap phase.
