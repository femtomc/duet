# Duet Runtime – Implementation Guide

This guide captures the agreed design for Duet: a **causally consistent, time-travelable Syndicated Actor runtime** with persistent state, time-travel/forking, and CRDT-based branch merging. It is written to explain the system’s required behaviour, invariants, and module responsibilities—no inline “sample code,” only the information needed to implement and verify the runtime.

---

## 1. Goals & Non‑Goals

**Goals**
- Implement the Syndicated Actor model from first principles while remaining protocol-compatible (actors, facets, assertions, messages, sync, capability attenuation, flow-control accounts, linked tasks).
- Deterministic, causally ordered turns that can be replayed or rewound exactly.
- Persistent storage of every turn plus periodic full-state snapshots using `preserves-rs`.
- Time-travel debugging: step forward/backward, jump to any turn, fork branches, and merge via CRDT joins.
- CLI + control plane supporting message injection, stepping, branching, and inspection.
- Seamless integration of external services (LLM assistants, automation, etc.) as Syndicated entities with deterministic transcripts.

**Non-goals**
- Backwards compatibility with the legacy Rust runtime implementation.
- Optimisation of IO/storage early on; correctness and determinism come first.
- Graphical tooling (the CLI is the primary interface initially).

---

## 2. Alignment with the Syndicated Actor Model

Duet must respect the following Syndicate semantics:

- **Actors & facets**: each actor owns a tree of facets; turns run with an active facet context and may spawn child facets or terminate facets. `Activation`-style context is required for assertions, deferred actions, and flow-control bookkeeping.
- **Assertions & retractions**: assertions are durable until explicitly retracted or the asserting facet dies. Handles must be unique per actor, and retractions must be causally ordered after the original assertion.
- **Messages**: transient events delivered during a turn. Delivery ordering must obey causal precedence.
- **Sync**: entities can synchronise with peers (a turn that includes a `sync` request must enqueue the corresponding `Synced` reply).
- **Capabilities & attenuation**: external references (Caps) carry attenuation caveats that must be enforced when delivering inbound assertions/messages.
- **Flow-control via accounts**: work items are tracked against per-actor accounts. Borrow/repay semantics ensure bounded parallelism.
- **Linked tasks & stop hooks**: facets can register stop actions and linked tasks that execute outside actor turns but interact through controlled re-entry.
- **External service participation**: helpers such as LLM assistants are modelled as entities/actors reached via capabilities; their requests/responses must respect attenuation and be replayable.

Throughout this guide every module description references the Syndicated primitive it implements. The runtime should expose the same public concepts (`Actor`, `Facet`, `Entity`, `Activation`, `Cap`, etc.) even if specific APIs differ from the legacy crate.

---

## 3. Architectural Overview

```
duet/
├── Cargo.toml
├── src/
│   ├── lib.rs                // Runtime library entry point
│   ├── runtime/
│   │   ├── mod.rs            // Runtime orchestrator & public API
│   │   ├── actor.rs          // Actors, facets, activation contexts, entities
│   │   ├── state.rs          // CRDT components & state delta representation
│   │   ├── scheduler.rs      // Deterministic turn scheduler + flow control
│   │   ├── turn.rs           // Turn metadata, preserves schema, hashing
│   │   ├── journal.rs        // Append-only turn log writer/reader
│   │   ├── snapshot.rs       // Snapshot creation/loading, interval policy
│   │   ├── branch.rs         // Branch DAG, time travel, CRDT merge
│   │   ├── storage.rs        // Filesystem layout helpers, atomic writes
│   │   ├── schema.rs         // Preserves schema registration & validation
│   │   ├── pattern.rs        // Pattern matching & subscription engine
│   │   ├── control.rs        // Runtime control facade used by CLI/tests
│   │   └── link.rs           // Network links for distributed actors (future)
├── python/
│   └── duet/
│       ├── pyproject.toml    // Python package definition
│       ├── src/duet/
│       │   ├── __init__.py
│       │   ├── __main__.py   // Entry point (python -m duet)
│       │   ├── cli.py        // Command dispatcher + Rich output helpers
│       │   └── protocol/     // Control client, request helpers
│       └── README.md
└── tests/
    ├── integration.rs        // End-to-end execution, time-travel, fork
    └── determinism.rs        // Replay determinism & branch convergence
```

**Key runtime flows**
- External input (CLI, timers, linked tasks, out-of-process services) enqueue `TurnInput` events tagged with logical clocks and actor IDs.
- The Python CLI interacts with the Rust runtime through the control API, exchanging structured commands/responses over a stable protocol (see Section 11).
- Scheduler selects the next enabled turn (lowest causal order) and executes it within a fresh activation context.
- Turn execution produces outputs (assertions, retractions, messages, facet actions) and a CRDT state delta.
- Journal writer commits the turn record (preserves-packed) and, when required, snapshot manager persists a full-state checkpoint.
- Branch manager updates branch metadata and exposes APIs for stepping, rewinding, forking, and merging.

### External Services

Any out-of-process helper (LLM assistants such as Claude Code or Codex, custom automation, etc.) is modelled as an ordinary Syndicated entity reachable via capabilities. The runtime follows standard rules:

- Requests to a service run as linked tasks that enqueue a `TurnInput::ExternalResponse` (or similar) before the turn commits, carrying the precise payload that was sent to the service.
- Responses are appended to the journal as `TurnInput`s containing the returned data; replay consumes the stored data instead of re-invoking the service.
- Capability attenuation governs what the service may assert or message back into the system.
- Flow-control tokens cover the entire lifecycle (borrow when the request is issued, repay on completion or timeout).
- Convenience adapters for specific services (LLM APIs, tooling stubs) live in integration code outside the core runtime; they simply follow the same input/output pattern described above.

---

## 4. Storage Layout & Persistence Format

Runtime files are stored beneath a configurable root (`.duet/` by default):

```
.duet/
├── config.json                    // Runtime configuration (snapshot interval, etc.)
├── meta/
│   └── <branch>.index             // Turn offsets, snapshot pointers, branch metadata
├── journal/
│   └── <branch>/
│       └── segment-000.turnlog    // Append-only turn records (preserves-packed)
└── snapshots/
    └── <branch>/
        └── turn-00000064.snapshot // Periodic full-state checkpoints
```

- **Turn log format**: each entry is a `TurnRecord` encoded using `preserves::PackedWriter`. Records include actor ID, branch, logical clock, causal parent turn, inputs, outputs, CRDT delta, and debug timestamp.
- **Snapshots**: `RuntimeSnapshot` structures, also encoded with preserves, containing the joined CRDT state for every actor/facet plus outstanding assertion handles and capability info.
- **Index files**: sidecar metadata (e.g., mapping from turn ID → (segment, offset)). Format can be bespoke but must support efficient random access; preserve necessary info to rebuild after crash (e.g., rebuild index by scanning segments).

All writes must be atomic: write to temporary file and rename; fsync directories as needed for durability.

---

## 5. Runtime Semantics & Invariants

1. **Deterministic turns**  
   - Turns only depend on their inputs, actor state, and deterministic scheduling metadata (logical clock, branch).  
   - Every turn is given a deterministic `TurnId` computed from the canonical preserves encoding of `(actor, clock, inputs)`.  
   - Replay of the same journal must reproduce identical state deltas and outputs.

2. **Causal ordering**  
   - Scheduler enforces happens-before constraints (messages cannot overtake earlier outputs; retractions come after assertions).  
   - Logical clocks track per-actor turn counts and are included in turn records.

3. **CRDT-based state**  
   - Actor internal state (assertion sets, capability tables, dataflow fields, ledger balances) is represented as lattices supporting associative/commutative/idempotent joins.  
   - Turn execution returns a delta joinable onto the actor state. Deltas must be serializable as part of the `TurnRecord`.

4. **Snapshots**  
   - Snapshot interval is configurable; snapshots capture an already-joined state (no pending deltas) at a given turn.  
   - On recovery, load the latest snapshot ≤ target turn and replay subsequent turns from the journal.

5. **Branches & time travel**  
   - Branch metadata records ancestry (parent branch + base turn).  
   - Rewinding resets active branch head to an earlier turn by loading appropriate snapshot and replaying forward.  
   - Forking creates a new branch directory referencing the base snapshot; new turns append to the new branch’s journal.

6. **Deterministic external side-effects**  
   - Any interaction with nondeterministic sources (wall clock, random numbers, network responses, file IO acknowledgements, OS timers) must be modelled as explicit `TurnInput` events whose data is persisted in the journal before the turn commits.  
   - During replay, the runtime never re-reads nondeterministic sources; it simply replays the recorded inputs.

7. **Flow-control accounts**  
   - Each actor owns at least one account; turns borrow tokens proportional to queued work before executing and repay upon completion.  
   - If an account exceeds the configured credit limit, the scheduler must block further work on that actor until tokens are repaid.  
   - Account balances persist across turns and are included in snapshots/deltas so time travel preserves outstanding debt.

8. **Crash recovery guarantees**  
   - On startup, the runtime must scan journal segments sequentially, stop at the first decoding error or checksum failure, truncate the partially written data, rebuild indexes, and resume from the last valid turn.  
   - Snapshot metadata must be validated (matching turn IDs, branch ancestry); corrupted snapshots should be ignored in favour of replaying from an earlier checkpoint.

6. **Merging**  
   - Given branches A and B diverging at LCA turn `T`, merge by loading `T`’s snapshot, replaying to get states `SA` and `SB`, then joining their CRDT states.  
   - Produce a synthetic “merge turn” whose delta represents the difference between `SA` and the joined state.  
   - Record provenance (source branch, LCA, conflict notes) in metadata for observability.

---

## 6. Module Responsibilities

### 6.1 `runtime::turn`
- Define the `TurnRecord` schema and associated `TurnInput`/`TurnOutput` enums (assert/retract/message/sync/timer/spawn/external-request/external-response).  
- Provide deterministic hashing (`compute_turn_id`) by preserves-encoding `(actor, clock, inputs)` and hashing with `blake3`.  
- Implement `encode`/`decode` helpers using preserves; all other modules rely on these for persistence.  
- Define the `preserves-schema` descriptions for turn records and register them during startup so persisted data is self-describing.

### 6.2 `runtime::state`
- Implement CRDT components:  
  - OR-set for assertions keyed by `(actor_id, handle)` with tombstones for retractions.  
  - Map-based lattices for facet trees and capability attenuation stacks.  
  - Counters/PN-counters for flow-control accounts.  
- Additional structures needed for timers, linked tasks, and external service transcripts (e.g., append-only log CRDT capturing requests/responses).  
- Provide `StateDelta` objects representing the effect of a turn and functions to apply/merge deltas.  
- Ensure all components implement efficient `join`, `diff`, and `is_empty` operations to support snapshots and merges.  
- Surface preserves schemas for each CRDT structure so state snapshots and deltas can be encoded/decoded consistently.
- Provide cross-node metadata (node IDs, link checkpoints) so distributed merges can reconcile remote assertions.
- Maintain indexes required for pattern matching (e.g., attribute maps, trie structures) so subscriptions can be evaluated quickly during turn execution.

### 6.3 `runtime::actor` & `runtime::registry`
- Define `Actor`, `Facet`, `Entity`, and `Activation` abstractions mirroring Syndicate semantics.
- Manage facet lifecycle (creation, termination, stop hooks, child relationships).
- Track flow-control accounts and outstanding work (borrow/repay).
- Execute turns: construct activation context, deliver inputs to entities, enforce invariants (no nested turns, commit/rollback semantics).
- Emit `StateDelta` objects by comparing pre/post CRDT state or by accumulating operations during the turn.
- Ensure any nondeterministic behaviour encountered in entities (timers, IO callbacks, randomness, external services) is routed through the scheduler as persisted `TurnInput`s before being observed inside the activation.
- Provide APIs for linked tasks, timer registration, and external service requests; their completions must enqueue deterministic scheduler events that capture return values or elapsed durations.
- Enforce capability attenuation when delivering messages/assertions (including those sourced from external tools) so every action respects the capability model.
- Support distributed references: outbound Caps can embed `(node_id, actor_id, facet_id)` locators and per-link attenuation data.
- Allow entities to register/unregister pattern subscriptions; ensure subscriptions run within the activation using the pattern engine and generate events as ordinary turn inputs/outputs.

#### Entity Registration & Hydration

The runtime provides infrastructure for registering entity types and persisting entity instances across restarts and time-travel:

- **Entity Registry** (`runtime::registry`): Global singleton mapping type names to factory functions. Application code registers types at startup via `EntityRegistry::global().register(name, factory)`. Factories take a `preserves::IOValue` config and return `Box<dyn Entity>`.

- **Entity Manager**: Tracks metadata for all entity instances (actor, facet, type, config, pattern subscriptions) in `.duet/meta/entities.json`. Metadata persists independently of turn execution so entities can be reconstructed during replay.

- **Dataspace-First Design**: Entities should express most state via assertions, capabilities, and facets (the CRDT-backed dataspace). This ensures:
  - State is automatically persisted in snapshots/journal
  - Time-travel replays state correctly
  - Branch merges are conflict-free (CRDT semantics)
  - No manual hydration code needed

- **HydratableEntity Trait** (optional): For rare cases where private state can't live in the dataspace (e.g., expensive caches, ephemeral derived data):
  - Implement `snapshot_state()` to capture private state as `preserves::IOValue`.
  - Implement `restore_state()` to hydrate from snapshot during replay.
  - Register the type with `EntityRegistry::register_hydratable`, which stores snapshot/restore adapters and ensures snapshots include the private state blob. During `goto`/replay the runtime restores these blobs before entities resume.
  - **Merge behavior**: If two branches have different private state, a merge warning is generated and one state wins arbitrarily. This is unavoidable for non-CRDT state—prefer dataspace-backed state when possible.

- **Pattern Subscriptions**: Entity metadata persists the full pattern specification, so hydration and time-travel re-register watches automatically. Declarative assertions made inside a turn (via `Activation::assert`) are routed through the pattern engine, meaning local assertions trigger `PatternMatched` notifications the same way external assertions do.

#### Built-in Entities (`codebase` module)

The crate ships with a small catalog of entities registered via `codebase::register_codebase_entities()` (called automatically when a runtime is created):

- `echo` – Accepts incoming messages and asserts `(echo <topic> <payload>)` into the dataspace. The topic defaults to `"echo"`, or you can configure it by passing a preserves string as the entity config.
- `counter` – A hydratable counter that maintains an integer value across time travel. Each message increments by the signed integer payload (defaulting to `1`) and asserts `(counter <value>)`.

Applications can register their own entity types alongside these defaults using the global `EntityRegistry`.

### 6.4 `runtime::scheduler`
- Maintain ready queues per actor, keyed by logical clock and causal dependencies.  
- Accept external events (messages injected via CLI, timers, sync completions, external service responses) tagged with actor & facet IDs.  
- Guarantee deterministic selection order; record scheduling cause (e.g., `External`, `Message`, `Timer`).  
- Interface with flow-control by blocking actors whose account exceeds credit limit.  
- Expose APIs: `enqueue(input)`, `next_turn()`, `advance(n)`, `jump_to(turn_id)`, `rewind(turn_id)`.  
- Inject all nondeterministic sources (wall-clock time, random seeds, OS notifications) via explicit scheduler inputs so turns remain deterministic under replay.
- Maintain deterministic timer/external queues: timer registrations from actors become scheduled `TurnInput::Timer` events with recorded deadlines, and adapters deliver `TurnInput::ExternalResponse` events containing recorded transcripts; during replay, these fire according to stored data, not the wall clock.
- Integrate inter-node message queues: inbound network traffic is converted into `TurnInput::RemoteMessage` events with source metadata; outbound events are logged before being sent.

### 6.5 `runtime::journal`
- Append turn records to the active branch’s current segment; rotate when size threshold met.  
- Maintain in-memory and on-disk indexes mapping turn IDs to (segment, offset).  
- Provide read iterators (`iter_from(turn_id)`), random access (`read(turn_id)`), and rebuild-on-startup logic.  
- Handle crash recovery: detect partial writes, truncate to last valid record (validate preserves decoding and checksum if needed), regenerate indexes from surviving records, and warn callers when truncation occurs.
- Include peer metadata in turn records so journal replay can reconstruct remote links and message ordering.
- Provide helper assertions for invariant tests (e.g., turn ID monotonicity, single active facet) used by unit/integration suites.

### 6.6 `runtime::snapshot`
- Create snapshots on configurable interval or explicit request.  
- Collect actor/facet CRDT states, outstanding timers/messages, branch metadata.  
- Persist using preserves; ensure atomic writes.  
- Load snapshots for time travel, forking, and recovery.  
- Validate snapshot metadata (branch id, turn id, checksums) during load; reject corrupt snapshots and fall back to replay.  
- Expose `nearest_snapshot(turn_id)` for branch manager.

### 6.7 `runtime::branch`
- Track branch DAG: parent branch, base turn, head turn, active snapshot pointer.  
- Implement `fork(new_branch, turn_id)`, `rewind(turn_id)`, `goto(turn_id)`.  
- Implement merge orchestrator: identify LCA, load states, compute CRDT join, emit merge turn, update metadata.  
- Provide GC hooks to delete unused snapshots/journal segments once branches are pruned.
- Coordinate distributed merges: record remote branch ancestry by node so multi-node histories remain consistent.

### 6.8 `runtime::storage`
- Hold filesystem paths and ensure directory creation.  
- Provide helpers for atomic writes (temp file + rename), segment naming, branch directories, and metadata read/write.  
- Abstract away absolute paths so other modules operate on logical handles.  
- Expose utilities used during recovery to enumerate segments, validate filenames, and truncate/replace files safely.
- Maintain per-peer directories (e.g., `links/<node_id>/`) storing link checkpoints or pending outbound messages.

### 6.9 `runtime::pattern`
- Compile and evaluate dataspace patterns (matching clauses, predicates) against the assertion store.  
- Maintain subscription tables keyed by facet/entity, with efficient incremental updates when assertions change.  
- Emit match/mismatch events back into the scheduler as standard messages/assertions so pattern matches participate in turn logging and merges.  
- Provide hooks for schema-aware pattern compilation so custom entities can register new predicates.  
- Accept patterns encoded as preserves values following the Syndicate DSL; hashed identifiers keep subscriptions stable across replay, while clients may override with explicit IDs when needed.

### 6.10 `runtime::control`
- Offer a high-level API for embedding or CLI use:  
  - `send_message`, `step`, `step_n`, `back`, `goto`, `fork`, `merge`, `status`, `history`.  
  - Manage active branch context.  
  - Surface metrics (turn count, head turn, pending inputs, snapshot interval).

### 6.11 `runtime::schema`
- Centralise all preserves schema definitions for turn records, state deltas, capabilities, external request/response payloads, and CRDT components.  
- Register schemas on startup so journal/snapshot files carry stable identifiers and can be read by tooling (including offline inspection of service transcripts).  
- Provide validation utilities used in tests to ensure schema evolution maintains backward compatibility.

### 6.12 `runtime::link` (future extension)
- Manage peer connections for distributed actors: capability negotiation, link lifecycle, message framing.  
- Encode inter-node messages using preserves; integrate with scheduler and journal modules.  
- Handle reconnection strategies, back-pressure, and link-level flow control while preserving deterministic replay semantics.

---

## 7. CRDT & Merge Semantics

To support automatic branch merges, every persistent state component is modelled with an explicit CRDT (Conflict-free Replicated Data Type). Joins are associative, commutative, and idempotent so replay and merge order never affect the result. Summary:

### Dataspace (Assertions & Retractions)
- **Structure:** Observed-Remove Set keyed by `(handle, value)` plus a tombstone set for retractions.  
- **Join:** union of adds minus union of tombstones.  
- **Semantics:** Any branch that retracts a handle suppresses it until a later turn reasserts it. Handle reuse is treated as a new assertion after retraction.  
- **Conflicts:** Concurrent different-valued assertions without retraction generate merge warnings; dataspace remains mergeable.

### Facet Lifecycle
- **Structure:** Map `FacetId ->` monotone status register (`Alive → Terminated → Removed`) with metadata (creator, timestamps).  
- **Join:** pointwise max by status ordering; metadata merged by earliest creation/latest termination.  
- **Semantics:** Once any branch terminates a facet, the merged state keeps it terminated. Metadata allows UI to indicate “revived” facets if reopened later.

### Capabilities
- **Structure:** Map `CapId -> {attenuation_chain, status}`.  
- **Join:**  
  - Status: `Revoked` dominates `Active`.  
  - Attenuation: intersection/most restrictive combined cage of caveats; union of provenance metadata.  
- **Semantics:** Capabilities never become more permissive after merge. Revocation anywhere revokes everywhere.

### Timers & Linked Tasks
- **Structure:** Map keyed by logical ID storing monotone state (`Scheduled(deadline)` → `Fired` → `Completed`/`Cancelled`). Distinct reschedules receive unique IDs to preserve history.  
- **Join:** choose earliest creation metadata; final state is max in the state lattice. Pending schedules from both branches coexist.  
- **Semantics:** Cancelled or fired timers stay resolved; snoozes in different branches produce separate scheduled events that both fire.

### External Service Transcripts
- **Structure:** Grow-only log (G-Set ordered by `(turn_id, sequence)`).  
- **Join:** union sorted by turn/sequence; no dedup.  
- **Semantics:** All prompts/responses, including failures, survive merges for auditability.

### Flow-Control Accounts
- **Structure:** PN-counter per actor plus ledger of outstanding loans keyed by work item.  
- **Join:** counters add; outstanding entries unioned by ID.  
- **Semantics:** Merged balances reflect total debt. Scheduler enforces credit limits post-merge; UI warns if limits exceeded due to merge.

### Pattern Subscriptions
- **Structure:** Map of subscription IDs to pattern descriptors plus cached match sets keyed by handle.  
- **Join:** union of subscription definitions; match caches recomputed deterministically from the merged dataspace (they are derived, not primary state).  
- **Semantics:** Subscriptions persist across merges; conflicting predicates produce merge warnings so users/agents can review why matches changed.

### Conflict Reporting
- Each join can optionally produce `MergeWarning` entries (e.g., conflicting assertion values). Warnings are stored in the merge turn metadata so the CLI can display a report.  
- No merge aborts automatically; users/agents resolve warnings by issuing new turns (reasserting, revoking caps, etc.).

### Extensibility
- Custom entities can implement a `MergeableState` trait providing `join`/`diff`. If not provided, merges default to “manual resolution required,” generating warnings without touching state.

### Distributed Links
- Remote messages/assertions arriving from another node are logged as `TurnInput::RemoteMessage { source_node, turn_id, payload }`. Joins treat these like any other input, so branch merges across nodes remain deterministic.  
- Capability CRDT entries store origin node metadata to keep attenuations consistent across peers.  
- Flow-control counters track per-node outstanding work, enabling mergeable back-pressure information.
- Pattern subscriptions remain local to the node that registered them; remote assertions still trigger matches because the dataspace is updated before evaluations. Future link extensions may allow remote subscriptions if needed.

> **Note:** These CRDT definitions live behind module boundaries (`runtime::state`, `runtime::actor`). Adjusting semantics later means updating those modules plus their schema encodings—higher-level APIs remain stable. This keeps future refinements localized.

---

## 8. CLI Functionality (Python Command-Line)

The `duet` CLI is intentionally stateless: each invocation launches `duetd`, performs a single command, prints the response with Rich, and exits. This makes it easy to script or drive via agents. Core subcommands include:

- `status` – display the active branch, head turn, queued inputs, and snapshot interval.  
- `history [--branch BRANCH] [--start N] [--limit M]` – list recent turns for a branch (turn id, actor, summary).  
- `send --actor ACTOR_ID --facet FACET_ID --payload <preserves text>` – inject a message or work item.  
- `register-entity --actor ACTOR_ID --facet FACET_ID --entity-type TYPE [--config VALUE]` – persist an entity and attach it to a facet.  
- `list-entities [--actor ACTOR_ID]` – enumerate registered entities (optionally filtered by actor).  
- `goto --turn-id TURN_ID [--branch BRANCH]` / `back [--count N] [--branch BRANCH]` – time-travel operations.  
- `fork --new-branch NAME [--source BRANCH] [--from-turn TURN_ID]` – create a branch.  
- `merge --source SRC --target DEST` – merge one branch into another and report warnings.  
- `raw COMMAND '{"param": ...}'` – send an arbitrary control-plane request for experimentation.

The CLI uses the control protocol defined in Section 11; extending it with additional subcommands should flow through the same request helpers so they remain testable.

---

## 9. Testing & Validation Strategy

1. **Unit tests per module**  
   - `turn`: encoding/decoding round-trips, deterministic hashing.  
   - `state`: CRDT join/idempotence, delta application, conflict scenarios.  
   - `actor`: activation commit/rollback, assertion lifecycle, flow-control invariants.  
   - `journal`: append + read, segment rollover, crash recovery, index rebuild.  
   - `snapshot`: save/load equivalence, nearest-snapshot lookup.  
   - `branch`: forking, rewinding, merge join semantics.  
   - `schema`: schema registration produces stable IDs; incompatible changes trigger test failures.  
   - `pattern`: subscription evaluation, incremental updates, and deterministic replay of matches.  
   - `control`: request parsing, response serialization, error codes, protocol version negotiation.  
   - `link` (when enabled): handshake/teardown, message framing, and idempotent replay of remote inputs.

2. **Integration tests**  
   - Single actor scenario with assertions/messages; run sequence, persist, reload, replay, verify state equality.  
   - Time-travel test: execute multiple turns, rewind, branch, diverge, ensure original branch unchanged.  
   - Merge test: create divergence, merge via CRDT join, verify final state matches manual join.  
   - Crash recovery test: simulate partial journal write / corrupted snapshot and verify graceful truncation and replay.  
   - External service integration test: run a conversation involving an out-of-process helper (or mock), ensure request/response transcripts replay exactly.  
   - Pattern integration test: register a watch, assert/retract facts across branches, ensure match notifications and merges behave as expected.  
   - Control protocol smoke test: exercise handshake, status, step, watch/unwatch, merge, link commands using recorded transcripts.  
   - Determinism harness: run same inputs twice, compare journal hashes and final state signatures.

3. **Property-based / fuzz testing (stretch goal)**  
   - Shuffle delivery order of independent inputs to confirm scheduler determinism.  
   - Random CRDT operations to ensure join is associative/commutative/idempotent.  
   - Generate random work/repay sequences to validate flow-control invariants and ensure no negative balances or credit bypasses occur.  
   - Randomly inject external success/failure responses to validate determinism and circuit-breaking logic.  
   - Property-test pattern subscriptions by randomly generating assertion streams and verifying match sets after joins.  
   - Fuzz journal recovery: synthesize partial writes/truncations and ensure validate/rebuild produce consistent indexes.  
   - Fuzz control protocol by generating random command sequences and asserting responses stay deterministic.

4. **Core invariant assertions (behavioural unit tests)**  
   - **Single-active-facet**: during turn execution, assert only one facet is marked `Active`; nested turns must panic in tests.  
   - **Turn ID monotonicity**: verify `TurnRecord::compute_turn_id` produces strictly increasing IDs per actor/logical clock and matches the IDs stored in the journal.  
   - **Deterministic commit**: run the same activation twice (with recorded inputs) and assert produced `StateDelta` hashes match and no rollback actions leak.  
   - **Capability attenuation**: ensure merging two capability states never increases available caveats; unit test join ordering permutations.  
   - **Subscription lifecycle**: watching/unwatching patterns must not leave stale entries; after branch rewind the subscription table must match the snapshot.  
   - **Flow-control credit**: borrowing without repayment should block scheduler advance when credit limit reached; repayment unblocks.  
   - **Journal recovery**: truncate mid-write, restart, ensure runtime discards partial turn and last valid turn remains replayable.  
   - **Remote input replay** (once links land): injecting identical `TurnInput::RemoteMessage` sequences twice yields identical local state.

5. **Manual/CLI validation**  
   - Scripts under `tests/` executing CLI commands to confirm UX and runtime alignment.

---

## 10. Implementation Phases

Each phase should end with green unit/integration tests before proceeding.

1. **Core Runtime Data Structures**  
   - Define turn schema, CRDT components, activation/actor scaffolding, storage helpers, runtime skeleton (`lib.rs`).  
   - Implement preserves schema registration module and ensure schema hashes are fixed.  
   - Add minimal tests for encoding, CRDT joins, activation invariants, schema registration.

2. **Persistence Layer**  
   - Implement journal writer/reader, snapshot manager, runtime startup/recovery path.  
   - Verify append/replay, snapshot load, and crash recovery (truncation + index rebuild) through tests.

3. **Deterministic Scheduler & Flow Control**  
   - Build scheduler loop, ready queues, flow-control, activation execution path.  
   - Tests for causal ordering, account credit enforcement.

4. **Branching & Time Travel**  
   - Implement branch management, rewind, goto, fork primitives.  
   - Ensure snapshots and journals cooperate when switching branches.

5. **CLI & Control Plane**  
   - Finalise runtime control API, implement CLI subcommands, integration tests driving CLI scenarios.

6. **External Service Integrations**  
   - Implement adapters or helper APIs for communicating with out-of-process services (LLMs, automation), ensuring deterministic transcripts and capability attenuation policies.  
   - Integrate flow-control and scheduler events for service requests/responses.  
   - Provide mock implementations for tests.

7. **Pattern Engine & Subscriptions**  
   - Implement `runtime::pattern`, including pattern compilation, subscription storage, and incremental evaluation.  
   - Expose control/CLI commands for managing watchers; add notifications for match changes.  
   - Extend tests to cover pattern matches across time travel and merges.

8. **Distributed Links**  
   - Introduce `runtime::link`, define link protocol (preserves-based framing, capability negotiation).  
   - Ensure incoming/outgoing remote events are journalled and replay-safe.  
   - Add CLI commands for connecting/disconnecting peers and list active links.

9. **CRDT Merge & Branch GC**  
   - Implement merge joins, conflict reporting, metadata updates, garbage collection hooks.

10. **Observability & Polish**  
   - Tracing/logging, performance instrumentation, documentation updates, optional advanced CLI features.

---

## 11. Control Protocol

The Rust runtime exposes a process-level control protocol that the Python CLI (and other clients) use. Design goals: human/agent friendly, versioned, stream-friendly, and language-agnostic.

### 11.1 Transport
- Primary transport is newline-delimited JSON (NDJSON) over the runtime process’s stdin/stdout when launched in “service” mode.  
- Optional `--socket <path>` flag makes the runtime listen on a Unix-domain socket (future TCP support).  
- Messages are UTF-8 JSON objects terminated by `\n`; the runtime never emits partial lines.  
- Clients must read asynchronously and handle interleaved responses and event notifications.

### 11.2 Envelope
```jsonc
// request
{"id": "uuid-or-int", "command": "status", "params": { ... }}

// response
{"id": "uuid-or-int", "result": { ... }}

// error response
{"id": "uuid-or-int", "error": { "code": "string", "message": "human readable", "details": { ... } }}

// notification (async event)
{"event": "turn_completed", "data": { ... }}
```
- `id` is required on requests/responses; `null` ids are reserved for one-way notifications.  
- `command` and `event` names use snake_case.  
- `params`, `result`, and `details` are JSON objects; absent fields imply empty objects.  
- All payloads include a `protocol_version` field in handshake responses for validation.

### 11.3 Command Set (v1)
| Command | Params | Result | Notes |
| --- | --- | --- | --- |
| `handshake` | `{ "client": "duet-cli", "protocol_version": "1.0.0" }` | `{ "protocol_version": "1.0.0", "runtime": { "version": "...", "features": [...] } }` | must be first command; runtime rejects mismatched major versions |
| `status` | optional `{ "branch": "name" }` | `{ "active_branch": "...", "turn_head": 123, "pending_inputs": [...], "snapshot_interval": 50 }` | |
| `list_branches` | `{}` | `{ "branches": [ { "name": "...", "head_turn": 42, "base": {...} } ] }` | |
| `history` | `{ "branch": "name", "start": 100, "limit": 50 }` | `{ "turns": [ TurnSummary ] }` | `TurnSummary` includes turn id, actor, cause, outputs hash |
| `step` | `{ "branch": "name", "count": 1 }` | `{ "executed": [ TurnSummary ] }` | count defaults to 1 |
| `goto` | `{ "branch": "name", "turn_id": 120 }` | `{ "head": 120 }` | rewinds/fast-forwards |
| `back` | `{ "branch": "name", "count": 1 }` | `{ "head": new_head }` | convenience wrapper around `goto` |
| `send_message` | `{ "branch": "name", "target": { "actor": "...", "facet": "..." }, "payload": { ... }, "format": "preserves-text" }` | `{ "queued_turn": turn_id }` | payload supplied as preserves text/binary encoded data |
| `inject_assertion` | `{ "branch": "name", "handle": "...", "value": { ... }, "action": "assert"|"retract" }` | `{ "queued_turn": turn_id }` | |
| `watch_pattern` | `{ "branch": "name", "pattern": { ... }, "watch_id": "optional" }` | `{ "watch_id": "stable-id" }` | installs pattern subscription; patterns are preserves-encoded, runtime derives a deterministic ID (unless overridden) and reports matches via notifications |
| `unwatch_pattern` | `{ "watch_id": "uuid" }` | `{ "unwatched": true }` | |
| `fork` | `{ "source": "main", "new_branch": "experiment", "from_turn": 150 }` | `{ "branch": "experiment", "base_turn": 150 }` | |
| `merge` | `{ "source": "experiment", "target": "main" }` | `{ "merge_turn": turn_id, "warnings": [ ... ] }` | result includes conflict metadata |
| `connect_peer` | `{ "node_id": "uuid", "address": "unix:///path" }` | `{ "link_id": "..." }` | establish network link |
| `disconnect_peer` | `{ "link_id": "..." }` | `{ "status": "disconnected" }` | |
| `list_links` | `{}` | `{ "links": [ { "link_id": "...", "node_id": "...", "status": "connected" } ] }` | |
| `schema` | `{}` | `{ "version": "1.0.0", "hashes": { ... } }` | exposes preserves schema hashes |
| `subscribe` | `{ "events": ["turn_completed", "branch_updated"] }` | `{ "subscribed": [...] }` | |
| `unsubscribe` | `{ "events": [...] }` | `{ "unsubscribed": [...] }` | |
| `shutdown` | `{}` | `{ "status": "shutting_down" }` | runtime performs graceful shutdown |

All commands are deterministic; runtime processes them sequentially per connection.

### 11.4 Notifications
- `turn_completed`: emitted after each executed turn; payload includes `branch`, `turn`, `summary`.  
- `branch_updated`: branch head changed (after `step`, `goto`, `merge`).  
- `merge_warning`: emitted when `merge` produced conflicts; details list affected handles/caps.  
- `pattern_match`: emitted when a watched pattern gains or loses matches; data includes `watch_id`, `kind` (`added`/`removed`), and affected handles.  
- `link_status`: link connected/disconnected events with node metadata.  
- `runtime_log`: optional human-readable logs or errors.

Clients can use `subscribe` to opt in; otherwise they only receive responses.

### 11.5 Error Handling
- Standard error codes:  
  - `invalid_command`, `invalid_params`, `not_found`, `conflict`, `unsupported_version`, `internal_error`.  
- Errors include descriptive `message` and machine-readable `details` (e.g., `{ "missing": ["branch"] }`).  
- Unknown commands → `invalid_command`; unsupported features → `unsupported_version`.

### 11.6 Versioning
- Semantic versioning. Major version changes may alter envelope/command semantics; runtime rejects mismatched majors.  
- Minor upgrades add optional fields or commands; clients should ignore unknown fields.  
- Runtime advertises supported versions in the handshake response (`features` list).  
- Documentation for each version lives in `docs/control-protocol-vX.Y.md`.

### 11.7 Testing & Tooling
- Provide golden files of request/response sequences under `tests/control`.  
- Build a `duetctl` smoke test that exercises handshake, status, step, merge, and verifies NDJSON structure.  
- CLI unit tests mock the runtime by replaying scripted NDJSON transcripts.  
- Runtime integration test ensures incompatible protocol versions fail fast.

---

## 12. Immediate Next Steps

1. Align the crate layout with the architecture diagram (`src/lib.rs` for the runtime, CLI binary under `src/bin/duet-cli.rs`).  
2. Implement Phase 1 modules (including schema registration) following the responsibilities outlined above.  
3. Establish baseline tests (encoding round-trips, CRDT joins) to lock in invariants early.  
4. Keep the guide updated as abstractions evolve—add clarifications, edge cases, and lessons learned.  
5. For distributed support, start capturing node identifiers in turn records even before link transport ships; this avoids migrations later.

This guide should remain the single source of truth for Duet’s architecture. Update it whenever design decisions change so future implementation work stays aligned with the Syndicated Actor runtime requirements we established.

---

## 13. Implementation Best Practices

### 13.1 Rust Runtime Guidelines

- **Project structure**: expose the public API from `src/lib.rs` and keep binaries or demos under `src/bin`. Use `pub(crate)` whenever possible to limit the export surface; document invariants with Rustdoc comments adjacent to the types they describe.
- **Error handling**: represent domain errors with `thiserror` enums inside each module; prefer returning structured errors up the stack rather than erasing them. Reserve coarse-grained wrappers (e.g., `ControlError`) for external boundaries. Avoid panics except for impossible conditions validated by tests.
- **Async & concurrency**: run inside Tokio’s multi-threaded runtime. Never block within async code—push CPU-heavy work into `spawn_blocking` tasks. Use `parking_lot` primitives for shared state and keep lock scopes minimal to avoid priority inversion.
- **Persistence hygiene**: centralise filesystem writes so every append uses temp files + atomic rename, followed by directory fsyncs. Validate preserves encodings in debug builds and include version identifiers in metadata for future migrations.
- **Tracing & metrics**: instrument scheduler decisions, turn commits, snapshot creation, and branch operations with `tracing` spans. Emit IDs (turn, actor, branch) so the Python CLI can correlate events.
- **Testing discipline**: favour module-level unit tests and scenario-based integration tests under `tests/`. Provide deterministic stubs for external services by emitting pre-canned `TurnInput::ExternalResponse` events.
- **IPC protocol**: expose a stable command protocol (e.g., JSON-RPC) via stdin/stdout pipes or a Unix socket. Serialize with `serde` in Rust; mirror the schema in Python. This keeps lifetimes simple and avoids tight FFI coupling.

### 13.2 Python CLI Guidelines

- **Package layout**: follow the structure sketched in Section 3 (package metadata, `src/duet`, protocol client, and CLI entry point). Ensure `__main__.py` enables `python -m duet` so both `uv run` and the installed `duet` script behave the same.
- **Command model**: keep the CLI stateless and composable—implement subcommands (`status`, `history`, `send`, `register-entity`, etc.) that spawn `duetd`, perform a handshake, execute one request, pretty-print the result with `rich`, and exit. This makes it easy for agents or shell scripts to drive the runtime.
- **Runtime discovery**: auto-detect the daemon binary (defaulting to `target/debug/duetd` or falling back to `duetd` on `PATH`), while allowing overrides via `DUETD_BIN` or `--duetd-bin` so different builds can be tested.
- **Testing**: exercise the CLI with subprocess-based smoke tests (e.g., via `uv run` in CI) and reuse the service protocol tests to lock down JSON envelopes. Add unit tests for any argument parsing helpers.
- **Future extensions**: a Textual/TUI experience can layer on top of the same control client later. Keep the protocol helpers and command dispatch isolated so richer front-ends can reuse them without duplication.

### 13.3 Cross-Language Integration Strategy

- **Shared protocol**: define `control::Command`/`control::Response` structs once, document them (e.g., `docs/control-protocol.md`), and version them. Include the protocol version in every handshake so the CLI and runtime can detect mismatches.
- **Process model**: keep long-lived state in the Rust runtime. The Python CLI should be stateless; on reconnect, it queries status/history via the control API. Optionally ship a lightweight daemon (`duetd`) the CLI launches or connects to.
- **Resilience**: ensure the CLI can surface runtime restarts or protocol errors, offering retry prompts. Conversely, the runtime should time out idle CLI sessions without impacting other clients.
- **Security**: capabilities already bound in the runtime apply equally to external services triggered by the CLI. Mask credentials in logs and avoid exposing capability attenuation details unless explicitly requested for debugging.
