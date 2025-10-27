# Next Implementation Tasks

## 1. Deterministic Scheduler & Flow Control
- Implement `Scheduler` event loop, ready queues (single/multi-actor).
- Hook scheduler into `Runtime::execute_turn` path; ensure single active facet invariant per turn.
- Integrate flow-control accounts: borrow/repay tokens, blocking when over credit limit.
- Unit tests: turn ordering, account exhaustion, deadlock prevention.
- Property tests: fuzz event delivery order, ensure deterministic outcomes.

## 2. Branching & Time Travel
- Flesh out `BranchManager` to support fork, switch, update head, and LCA lookup.
- Implement rewind/goto/branch switching in runtime; coordinate with journal & snapshot.
- Add snapshot lookup (`nearest_snapshot`) and replay mechanism.
- Tests: fork/goto/back scenarios, branch history integrity, snapshot replay correctness.

## 3. Pattern Engine & Subscriptions
- Implement pattern compilation/evaluation against assertion CRDT.
- Maintain subscription tables with deterministic IDs.
- Integrate pattern matches into scheduler outputs and control notifications.
- Tests: pattern matches upon assert/retract, branch rewind/match recomputation.

## 4. CLI & Control Protocol Implementation
- Implement control commands (status, history, step/back/goto, fork/merge, watch/unwatch, connect/disconnect peers).
- Expand the Python CLI (`python/duet`) with additional subcommands/formatting; keep it script-friendly for smoke tests and future TUIs.
- Tests: control protocol golden transcripts, CLI integration with mocked runtime.

## 5. External Service Integrations
- Design adapter interface for LLM/automation services (request/response preserving).
- Implement deterministic transcripts as turn inputs/outputs.
- Flow-control integration for long-running tasks.
- Provide mock service implementations for tests.

## 6. Distributed Links (Future phase after core features)
- Define link protocol (preserves framing, capability negotiation).
- Log remote messages as deterministic turn inputs; ensure replay safety.
- Control commands: connect_peer, disconnect_peer, list_links; notifications for link_status.

## 7. CRDT Merge & Branch GC
- Implement CRDT joins for all state components (assertions, facets, capabilities, timers, transcripts, flow control, subscriptions).
- Merge turn generation + warnings/conflicts reporting.
- Branch garbage collection of unused snapshots/journal segments.
- Tests: merge compatibility scenarios, conflict reporting accuracy.

## 8. Observability & Polish
- Instrument tracing/logging across scheduler/journal/merge paths.
- Surface metrics for CLI (pending inputs, snapshot latency, branch stats).
- Document runtime APIs, add doc comments (resolve current missing_docs warnings).
- Provide developer tooling scripts (format, lint, test). 
