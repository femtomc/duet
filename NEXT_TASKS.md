# Next Implementation Tasks

This checklist is now superseded by **NEXT_STEPS.md**, which tracks the
multi-sprint roadmap for interpreter and runtime evolution. Keeping it around
for historical context, but all new planning should happen in that document.

---

## Legacy TODO Snapshot (March 2024)

These were the original bring-up tasks for the runtime. Items that are still
relevant have either shipped or have been rolled into the new roadmap.

- Deterministic scheduler & flow control basics (✅ implemented).
- Branching, time travel, and CRDT merge primitives (✅ implemented; polishing
  continues as part of ongoing work).
- Pattern engine & subscriptions (✅ live, slated for further ergonomics work).
- CLI/control protocol, external service adapters, distributed links, CRDT GC,
  and observability polish (partially complete; any remaining work is tracked
  per sprint in `NEXT_STEPS.md`).

Refer to the new roadmap for up-to-date priorities.
