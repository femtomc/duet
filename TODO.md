# Background
We’re shifting the Duet control plane from mutation-capable NDJSON commands to a passive transport. A long-lived control interpreter running inside the runtime will mediate every mutation (workspace edits, agent orchestration, reaction registration). Clients discover this interpreter via a dataspace assertion, send requests to it, and receive structured responses; later, per-session interpreters with scoped capabilities will run client-side workflows. All mutations must remain journaled and capability-secure, and existing CLI/GUI tooling should keep working through a temporary bridge while we migrate.

# Enhancements Needed
1. Language & IR ergonomics: add let/bindings, richer wait predicates (label-only, pattern, timeout), better validation errors, and recursion/loop guardrails so the control interpreter broker is readable and safe.
2. Module/import system: introduce a minimal module loader and bundled standard library (`workspace.duet`, `agent.duet`, `reaction.duet`, etc.) so interpreter programs can share helpers.
3. Capability management: track and hand out workspace/tool capabilities, support attenuated capabilities for session interpreters, refresh/revoke capabilities, and log capability activity.
4. Control interpreter request protocol: define dataspace schema for `control-request`/`control-response` with IDs, payloads, status; implement a broker loop that observes requests, runs helpers, and emits responses/progress/errors.
5. Standard helper programs: implement interpreter programs for workspace rescan/list/read/write, reaction register/unregister/list, interpreter definition/instance management, and optional agent orchestration helpers.
6. Supervision & discovery: supervise the control interpreter (restart/backoff), expose health/status, and include version/feature metadata in the discovery assertion/handshake.
7. Service bridge: add passive forwarding so legacy NDJSON commands enqueue control requests and return interpreter responses until clients switch.
8. Client refactoring: update CLI/GUI to read the control interpreter locator, issue control requests, tail responses, and manage per-session interpreters with scoped capabilities.
9. Documentation & tooling: align language docs with implemented features, document the control-request protocol and capability policy, add tests/examples/lints for interpreter modules.
