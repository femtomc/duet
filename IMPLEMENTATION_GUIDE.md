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
│   └── codebase/       # Built-in entities (workspace, agents, transcripts)
├── python/duet/        # CLI & NDJSON control client
├── tests/              # Integration & feature tests
└── docs/               # Design notes and guides
```

### 2.1 Runtime Core (`src/runtime`)
- **`actor.rs`** – actors, facets, activation contexts; enforces flow control,
  patterns, reactions.
- **`state.rs`** – CRDT sets/maps/counters plus `StateDelta`.
- **`scheduler.rs`** – deterministic ready queue with per-actor credit limits.
- **`journal.rs` / `snapshot.rs`** – persistent storage; recovery verified by
  tests.
- **`branch.rs`** – branch DAG, rewind, merge scaffolding (CRDT join-based).
- **`service`** (crate module) – NDJSON control-plane dispatcher used by the CLI.

### 2.2 Codebase Entities (`src/codebase`)
- **Workspace** – publishes filesystem view + capabilities for read/write.
- **Transcripts** – records `agent-request`/`agent-response` pairs; CLI renders
  them with branch/turn metadata.
- **Agents** – Claude Code and Codex stubs that convert requests into external process
  invocations, plus a generic OpenAI-compatible harness that POSTs to chat completion
  APIs (OpenAI, OpenRouter, LM Studio) while preserving deterministic responses.

### 2.3 CLI (`python/duet`)
- NDJSON client with Rich output for status, transcript display, workspace
  inspection, reaction management, and now `workflow` commands that interact
  with interpreter definitions/instances.
---

## 3. Module Responsibilities & Status

### 4.1 Runtime Core
- **Scheduler** – ready queue & flow control (✅ complete; tuning ongoing).
- **Journal & snapshots** – crash recovery and replay (✅ complete; monitor size
  growth once workflows intensify).
- **Patterns & reactions** – run-time subscriptions & templated actions (✅
  usable; future work includes match-field helpers and metrics).
- **Service** – control facade exposed over NDJSON (✅); ongoing work: richer
  error codes and incremental streaming helpers.
### 3.2 Future Language Work
- The legacy workflow interpreter has been removed. The upcoming Scheme-like
  kernel language is specified in `docs/LANGUAGE_DESIGN.md`; implementation work
  will introduce new modules alongside the runtime once ready.
- CLI workflow commands currently operate in compatibility mode; they will be
  revisited when the kernel interpreter lands.

---

## 4. Testing & Tooling

- `cargo test` covers runtime CRDTs, scheduler, service commands, and reactions.
- Python CLI linting: `python3 -m compileall python` included in the workflow;
  consider adding unit tests for CLI formatters as functionality grows.
- Python CLI linting: `python3 -m compileall python` included in the workflow;
  consider adding unit tests for CLI formatters as functionality grows.
-- Add golden transcripts for agent/tool interactions to ensure CLI/service
  outputs remain stable.

---

## 5. Open Design Questions

1. **Value semantics** – Do we support arithmetic/comparison in the DSL or rely
   on host-provided helpers?
2. **Hook system** – Expose runtime events (turn commit, branch switch,
   assertion change) as interpreter hooks? Needs determinism review.
3. **Capability plumbing** – How should interpreter code acquire capabilities
   safely? Define a standard library for capability discovery.
4. **Security / sandboxing** – Once workflows can spawn external tools we must
   revisit capability attenuation and CLI permissions.

---

## 6. References

- `NEXT_STEPS.md` – sprint-by-sprint roadmap.
- `docs/LANGUAGE_DESIGN.md` – kernel language specification.

Keep this guide current whenever we land meaningful architecture changes or
finish a roadmap phase.
