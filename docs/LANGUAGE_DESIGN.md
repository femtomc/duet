# Duet Kernel Language Design

## 1. Motivation and Context

Duet’s backend is a reversible, capability-secure implementation of the syndicated
actor model. Actors own facets, dataspace assertions, capabilities, and entity
lifecycles; every turn logs deterministic inputs/outputs so the runtime can rewind,
branch, and replay without ambiguity. The first interpreter (“v0”) provided a
fixed workflow DSL that compiled to `Action` / `WaitCondition` enums, but it lacked
expressive power (no higher-order functions, limited control flow) and duplicated
logic for every orchestration pattern.

We are introducing a Scheme-like kernel language so per-client interpreters can
program syndicated actors directly. The kernel must satisfy four project goals:

1. **Persistence & Time Travel** – every observable effect must still flow through
   turn outputs so the journal stays replayable, snapshots serialize interpreter
   state, and rewinds roll back spawned entities/capabilities.
2. **Capability Discipline** – the kernel should surface Syndicate operations only
   through capability-checked primitives, preserving the provenance and attenuation
   guarantees that Duet enforces at runtime.
3. **Multi-Client Collaboration** – multiple clients connect to a shared runtime,
   each hosting an interpreter session that can spawn facets, agents, and helper
   entities while sharing the dataspace.
4. **Programmable Orchestration** – orchestration logic (agent workflows, control
   plane utilities) lives in user-space libraries rather than hardcoding state
   machines in Rust, making Duet adaptable and auditable.

The language design below is grounded in the underlying syndicated actor model and
targets these requirements explicitly.

## 2. Core Evaluation Model

The kernel is an applicative-order Scheme with:

- Proper tail recursion and lexical scoping.
- First-class procedures and closures.
- Hygienic macros (`syntax-rules` initially, extensible to `syntax-case`).
- Continuations: `call/cc` plus delimited `prompt`/`control` to suspend fibers
  around waits without leaking host state.
- Standard data types: numbers, booleans, characters, symbols, keywords, pairs,
  lists, vectors, bytevectors, and records (mapped to syndicated records).

### Persistence Alignment

Continuations capture only Scheme-level environments and the descriptors needed
to resume the current wait. The interpreter serializes these continuations into
turn snapshots; on replay, closures restore the same lexical environment and wait
handles, so deterministic re-execution is preserved.

### Motivation

Providing a Scheme core lets us express orchestration patterns, macros, and DSLs
as libraries. Proper tail recursion and continuations allow lightweight fibers
without depending on host threads, which simplifies snapshotting.

## 3. Data Model and Serialization

Dataspace assertions in the syndicated model are structured values (`preserves::IOValue`).
We mirror that structure:

- **Atoms**: numbers, strings, booleans, symbols, keywords map directly to IOValue.
- **Pairs/Lists**: serialized as lists.
- **Vectors/Bytevectors**: serialized as arrays / blobs.
- **Records**: `(record <label> field …)` maps to IOValue records with label symbols.

Every kernel value that leaves the interpreter (assertions, messages, capability
payloads) must be convertible to `IOValue`. Non-serializable values (procedures,
continuations) are rejected when they reach a boundary; this keeps the journal
pure and replayable.

### Motivation

Matching IOValue means existing runtime tooling (pattern engine, observers,
time-travel logs) can understand kernel assertions without translation layers.

## 4. Control Primitives

### Conditionals and Binding

- `if`, `cond`, `and`, `or`, `case` behave as in Scheme.
- `let`, `let*`, `letrec`, `define`, `set!` provide structured bindings.
- `begin` sequences expressions within a facet turn.

These constructs are pure Scheme forms and do not interact with the runtime.

### Continuations and Fibers

- `call/cc` captures the current continuation.
- `prompt`/`control` support delimited continuations, useful for implementing
  async abstractions (e.g., `async/await`, structured concurrency).
- `spawn` (language-level) creates a lightweight fiber bound to the current facet.
- Fibers yield explicitly via `await`, `yield`, or `spawn` constructs; the host
  scheduler records their continuations in the interpreter snapshot.

Motivation: orchestrating multiple agents requires concurrent workflows; delimited
continuations give us precise control over suspension points, essential for
coordinating waits that tie directly to runtime turn boundaries.

## 5. Syndicate-Specific Primitives

The interpreter exposes host primitives that align with the syndicated actor model.
Each primitive checks capabilities and emits corresponding `Action` or
`WaitCondition` variants so the runtime journal captures them.

| Primitive            | Description | Runtime Mapping | Motivation |
|----------------------|-------------|-----------------|------------|
| `(assert! value)`    | Assert a value into dataspace; returns handle | `Action::Assert` | Core Syndicate operation; assertions are the shared state. |
| `(retract! handle)`  | Retract a previously asserted handle | `Action::Retract` | Supports reversible state updates. |
| `(signal! label payload …)` | Convenience for emitting structured signal records | `Action::Assert` preset | Signals orchestrate state transitions. |
| `(send! actor facet payload)` | Deliver a message to another actor/facet | `Action::Send` | Message passing across actors. |
| `(watch pattern handler)` | Register observer for dataspace events | `Action::Observe` | Mirrors Syndicate facets observing assertions. |
| `(unwatch token)`    | Remove observer | `Action::UnregisterPattern` | Deterministic teardown. |
| `(await condition)`  | Suspend until condition satisfied | `WaitCondition::*` | Aligns with wait semantics used by runtime. |
| `(spawn-facet! [parent])` | Create child facet | `Action::Spawn` | Structural concurrency within actor. |
| `(spawn-entity! role …)` | Spawn new actor/entity through capability | `Action::SpawnEntity` | Allows per-role agent instantiation; respects capability gates. |
| `(attach-entity! role …)` | Attach helper entity to current actor | `Action::AttachEntity` | Keeps lightweight utilities in-scope. |
| `(detach-entity! role)` | Tear down attached entity | `Action::DetachEntity` | Clean teardown for reversibility. |
| `(invoke-tool! role capability payload [:tag])` | Use a capability | `Action::InvokeTool` | Bridges to codebase tools, workspace operations, etc. |
| `(generate-request-id! role property)` | Deterministic request tags | `Action::GenerateRequestId` | Keeps request correlation consistent across rewinds. |
| `(log! text)` | Emit interpreter log record | `Action::Log` | User-visible progress and debugging. |

### Motivation

Each primitive is a thin veneer over existing runtime actions, ensuring we do not
duplicate side-effect paths and that time-travel semantics remain intact. They
provide a natural Scheme API without hiding the underlying Syndicate concepts.

## 6. Waiting and Asynchrony

`await` accepts structured wait descriptors:

- `(signal label [:scope value])`
- `(record label :field n :equals value)`
- `(tool-result :tag string)`
- `(user-input :prompt value [:tag string])`
- `(predicate proc)` (host polls the predicate against incoming assertions/messages).

When invoked, the interpreter registers the wait with the activation context and
parks the current fiber. Wait handles include deterministic UUIDs derived from the
actor/facet/request tuple so replay rebuilds identical registrations. When the
runtime sees the waited-for assertion/tool result, it resumes the parked fiber with
the matched value (converted back to Scheme data).

### Motivation

This design mirrors the v0 interpreter’s `WaitCondition` semantics (`src/interpreter/ir.rs`)
while freeing us from fixed state machines. By baking waits into the kernel, we can
express loops, branching, and concurrent waits using pure Scheme patterns.

## 7. Sessions and Capability Attenuation

Multiple clients connect to the runtime. Each session gets:

- A per-session facet (spawned via `spawn-facet!` from the control interpreter),
  isolating interpreter fibers and capability tables.
- An attenuated capability set recorded in the dataspace (e.g., `(session/capability …)`).
- Request/response records:
  - `(session/request :session <id> :payload <sexp> :tag <uuid>)`
  - `(session/event :session <id> :kind <symbol> :payload <value> :tag <uuid>)`

The interpreter entity watches request records, compiles them into Scheme modules
or expressions, executes them within the session’s environment, and emits events
with results or prompts (e.g., user input, tool invocation status).

Capability enforcement happens at two levels:

1. The runtime still checks capability IDs when executing primitives.
2. The interpreter maintains a capability manifest in the session environment,
   so scripts cannot reference operations they were not granted.

### Motivation

This setup mirrors the syndicated actor model’s emphasis on explicit capability
granting while enabling multiple clients (humans, agents) to coexist in the same
runtime without interfering.

## 8. Module System

Modules are declared with:

```scheme
(define-module (duet planner)
  (provide run spawn-team)
  (require (duet base)
           (duet agents)))
```

Features:

- Namespaces follow `(hierarchical names)`.
- Each module lists required capabilities; the interpreter checks them against
  the session manifest before loading.
- Module compilation caches bytecode/AST keyed by source hash and capability set.
- Modules can export macros and values; macros expand at load time.
- A small standard library ships with the runtime (base procedures, dataspace
  helpers, agent orchestration utilities).

### Motivation

Modules let us package orchestration patterns (planner/worker loops, review
ratchets, control-plane helpers) while controlling capability exposure. They
also give the CLI/TUI a way to preload libraries for user programs.

## 9. Persistence and Snapshotting

- Interpreter snapshots include:
  - Module cache descriptors (module name, source hash, capability manifest).
  - Session tables (facets, capability IDs, environment frames).
  - Fiber registry (continuations + pending waits).
  - Outstanding waits (converted to `WaitStatus` records for rehydration).
  - Prompt/request state (captured by the interpreter host for resumability).
- Snapshots are serialized using the same IOValue-based protocol already used in
  the runtime snapshot machinery (`runtime_snapshot_to_value`).
- On replay, the interpreter rebuilds module caches, reattaches waits, and resumes
  fibers from their saved continuations.

### Motivation

Reusing the snapshot pipeline ensures compatibility with existing journal and
branch machinery. Because continuations might carry large environments, we mandate
that top-level code avoid storing non-serializable host resources in closures.

## 10. Compatibility and Migration

- Provide a transitional compiler that takes v0 workflow definitions and emits
  kernel code using the new primitives. This allows `.duet` files to coexist with
  kernel modules while the ecosystem migrates.
- Maintain the old parser/IR only during migration; once clients switch, remove
  the redundant code to reduce maintenance.
- Update documentation (`README.md`, `docs/workflows.md`) to reference kernel
  modules and macros instead of the old `(state …)` DSL.

### Motivation

We avoid breaking existing workspaces immediately, giving users time to adopt the
new language without losing functionality.

## 11. Testing Strategy

- **Unit Tests**: validate parser, macro expander, evaluator, and primitive FFI
  bindings; ensure serialization round-trips for continuations.
- **Integration Tests**: run kernel programs that:
  - Spawn facets and agents, assert dataspace facts, rewind, and confirm state.
  - Perform capability invocations and check that tool results resume fibers.
  - Exercise multi-session scenarios to catch capability leakage.
- **Cross-Reference Tests**: execute analogous programs on `syndicate-rs` (reference
  implementation) to check behavioural parity for assertions and waits.

### Motivation

Testing against time-travel operations ensures the core runtime guarantees remain
intact; cross-reference with `syndicate-rs` builds confidence that our primitives
match the original model’s intent.

## 12. Roadmap Summary

Implementation tasks are tracked in `TODO.md` and include:

1. Parser/AST/module loader work.
2. Evaluator with continuations and serialization.
3. Syndicate primitive FFI layer.
4. Fiber scheduler and wait registry.
5. Session/capability management.
6. Standard library + compatibility bridge.
7. Tooling, tests, documentation updates.

Each milestone must be validated against the runtime’s reversibility story before
progressing to the next, ensuring we never compromise Duet’s core guarantees.
