# Workflow Definitions

This document captures the emerging design for Duet's programmable workflow language.

## Goals

* Express rich, user-authored orchestration programs without hardcoding policy in the runtime.
* Treat workflows as persistent dataspace assertions that the interpreter entity can react to.
* Compose existing primitives (agent prompts/responses, capability invocations, reactions) through a uniform schema.

## Language Overview

We treat workflows as programs that target the Syndicated Actor VM. A workflow
program defines:

* **Imports / requires** – modules that supply helper macros/functions (e.g.,
  `codebase/transcript`).
* **Roles** – named bindings to agents/entities/capabilities.
* **State graph** – a set of named states with actions, waits, and transitions.
* **Signals** – dataspace assertions that notify the interpreter when to advance.

Programs execute deterministically: the interpreter entity materialises actions
as activation outputs (messages, capability invocations, dataspace assertions)
and waits until the declared conditions occur in the dataspace.

Persistence, time-travel, and branching remain automatic; workflow execution is
just another stream of turns.

### Local bindings

Within function bodies you can introduce scoped bindings to keep expressions
readable:

- `(let ((name expr) …) body …)` evaluates each `expr` once and makes the
  results available to the subsequent forms. Bindings are visible for the rest
  of the enclosing body (including nested `let`s).
- `(let* ((name expr) …) body …)` is the sequential variant: each binding may
  reference the ones that precede it.

State bodies currently require explicit helper functions if you need the same
effect; we expect to generalise `let` across the whole language as the DSL
evolves.

## Representation

We adopt a Lisp-style S-expression syntax because:

* Dataspace assertions are already symbolic; the workflow definition can be stored verbatim.
* Nesting expresses branching/looping clearly without JSON ceremony.
* Future macros/templates become natural (`defworkflow`, `defstep`, etc.).

Example (pseudocode; concrete grammar below):

```
(workflow planner-worker-review
  (require codebase/transcript)
  (require codebase/workspace)

  (metadata
    (label "Planner/Worker review loop"))

  (roles
    (planner :agent-kind "claude-code" :handle-ref "agent-a")
    (implementer :agent-kind "claude-code" :handle-ref "agent-b"))

  (state plan
    (enter
      (log "Planner drafting plan")))
    (wait (signal plan/ready :scope @workflow-id)))

  (state handoff
    (action
      (send-prompt
        :agent implementer
        :template "Please implement the following plan:\n~a"
        :args ((signal-value plan/summary :scope @workflow-id))
        :tag handoff-request))
    (await (record agent-response :field 1 :equals handoff-request)))

  (state review
    (loop
      (action
        (send-prompt
          :agent planner
          :template "Review implementer's diff:\n~a"
          :args ((last-artifact implementer))
          :tag review-request))
      (await (record agent-response :field 1 :equals review-request))

      (branch
        (when (signal review/satisfied :scope @workflow-id)
          (goto complete))
        (otherwise
          (action
            (send-prompt
              :agent implementer
              :template "Planner feedback:\n~a"
              :args ((transcript extract-feedback review-request))
              :tag implementer-fix))
          (await (record agent-response :field 1 :equals implementer-fix))))))

  (state complete
    (enter (log "Workflow finished"))
    (terminal)))
```

**Notes**

* `@workflow-id` is a placeholder substitution inserted by the interpreter to scope signals.
* `signal` expressions refer to dataspace assertions; the interpreter waits for them.
* `send-prompt` emits a `send_message` command; templates interpolate runtime values.
* `await (record agent-response :field 1 :equals ...)` watches for `agent-response` assertions tagged with the supplied identifier.
* `await ready` is shorthand for `(await (signal ready))`, i.e. wait for a dataspace assertion labelled `ready`.

## Interpreter Expectations

1. Workflow definitions live under a recognised label (`workflow/definition`).
2. Instances are asserted with
   `(workflow-instance :definition <id> :label "feature-42" :branch main :context {...})`.
3. Interpreter entity:
   * Reacts to new instances.
   * Asserts `workflow/state` updates with step name, status, awaited condition.
   * Executes actions (send messages, invoke capabilities) directly through the activation context so every effect is part of the turn output stream.
   * Watches for assertions that satisfy waits (`signal`, `record`, future `tool-result`).
   * Emits `workflow/log` entries summarising progress/errors.

## Core Forms (Draft)

| Form | Purpose |
|------|---------|
| `(workflow <name> …)` | Top-level declaration. |
| `(require <module>)` | Imports helper macros/functions from the standard library. |
| `(roles (name :agent-kind …) …)` | Binds workflow roles to agent/kind/handle metadata. |
| `(state <name> …)` | Declares a state. Elements inside describe entry actions, waits, or terminal behaviour. |
| `(enter <expr>)` | Optional entry hook for logging/asserting on state entry. |
| `(action …)` | Emits an action, such as `send-prompt`, `invoke-tool`, `assert`. |
| `(wait <condition>)` | Blocks progression until the condition is satisfied (only valid before other actions). |
| `(await <condition>)` | Blocks after an action; can be repeated inside loops. |
| `(branch (when <cond> <body>) (otherwise <body>))` | Conditional branching. |
| `(loop …)` | Repeats nested actions/awaits until a branch exits. |
| `(terminal)` | Marks workflow completion. |

If you omit explicit `(state …)` forms, the interpreter wraps the top-level
action/await forms in an implicit `(state main …)` block. This keeps quick
scripts concise while preserving the state-machine semantics under the hood.

Loops automatically carry a safety cap (currently 1,000 iterations per turn) to
prevent runaway control programs. If a loop exceeds that bound the interpreter
fails fast with a descriptive error so the broker cannot wedge the runtime.

Conditions (non-exhaustive):

* `(signal <label> :scope … :fields …)` – waits for an assertion with the given label and optional filters.
* `(record <label> :field <index> :equals <value>)` – waits for a dataspace record whose field matches the supplied value (e.g. agent responses tagged by request id).
* `(tool-result :tag <id>)` – waits for an `interpreter-tool-result` assertion with the supplied tag.

Actions (non-exhaustive):

* `(send-prompt :agent <role> :template <fmt> :args (<expr> …) :tag <id>)`
* `(invoke-tool :role <role> :capability <alias-or-uuid> [:payload <expr>] [:tag <id>])`
* `(spawn-entity :role <role> [:entity-type <id>] [:agent-kind <kind>] [:config <expr>])` –
  mint an `entity/spawn` capability-backed request. You can supply an explicit
  entity type, reference an agent kind (`"claude-code"`, etc.), or rely on the
  role’s existing `:agent-kind` / `:entity-type` properties. The interpreter
  updates the role bindings with the spawned actor/facet/entity identifiers and
  asserts `(interpreter-entity <instance-id> <role> <actor> <facet> <entity-id> <entity-type> [<agent-kind>] [role-properties …])`
  records for observability. The trailing `role-properties` payload mirrors the
  interpreter’s role bindings so other entities can discover and reuse the
  spawned actors. When the resolved entity corresponds to a Duet agent, the
  interpreter also registers a wildcard `agent-request` pattern on the new
  facet so prompts flow to the agent without extra plumbing.
* `(attach-entity :role <role> [:facet <uuid>] [:entity-type <id>] [:agent-kind <kind>] [:config <expr>])` –
  instantiate an entity inside the current actor. By default the entity is
  attached to the active facet, but you can provide a facet UUID (for example
  one returned by `(spawn-facet …)`). Role bindings are updated with the
  interpreter actor/facet identifiers, and when an agent kind is supplied the
  interpreter automatically registers an `agent-request` pattern so the entity
  sees local dataspace assertions without spawning a helper actor.
* `(observe (signal <label> [:scope …]) <handler-program>)` – register a persistent
  observer that runs `handler-program` whenever the dataspace emits the matching
  signal. The interpreter stores observers as
  `(interpreter-observer <id> <condition> <handler-ref> <facet-id>)` records so
  they survive hydration and time-travel.
* `(log <text>)`
* `(assert <value>)`
* `(retract <value>)`
* `(register-pattern :role <role> :pattern <value> [:property <name>])` – register a
  dataspace pattern on behalf of the entity bound to `<role>`. The interpreter
  infers the facet/entity ids from the role binding and calls
  `register_pattern_for_entity`, so the entity receives assertions matching the
  supplied preserves pattern. When `:property` is provided the generated pattern
  UUID is written back into the role’s properties, making it accessible through
  `(role-property …)` for later retraction or logging.
* `(unregister-pattern :role <role> [:pattern <expr>] [:property <name>])` – remove a
  previously registered pattern. When `:pattern` is omitted the interpreter looks up
  the identifier from the supplied (or default `agent-request-pattern`) property, clears
  that property, and unregisters the subscription at the runtime level.
* `(detach-entity :role <role>)` – detach the entity bound to `<role>`, remove any
  interpreter metadata for it, and clear the standard role properties (`actor`,
  `facet`, `entity`, `entity-type`, `agent-kind`, `agent-request-pattern`). Combine with
  `unregister-pattern` when custom subscriptions were added.

`(invoke-tool …)` emits an `interpreter-tool-request` record containing the workflow
instance id, correlation tag, role metadata, capability alias, capability UUID,
payload (when provided), and optional role properties. The runtime immediately
executes the capability and asserts an `interpreter-tool-result` record with the
same tag:

```
(interpreter-tool-result <instance-id> <tag> <role> <capability-alias> <capability-uuid> <result> [role-properties …])
```

On success `<result>` is whatever the capability returned; on failure the
runtime publishes `(tool-error <message>)`. Pair the action with `await
(tool-result :tag …)` to suspend until the invocation completes.

Expressions available in templates / args may include:

* `(signal-value <label> :scope … :field <n>)`
* `(transcript extract-feedback <tag>)`
* `(last-artifact <role>)`

These derive from the standard library modules. The core interpreter only understands
action/condition skeletons; specific helpers are provided by modules.

### Choosing Between `spawn-entity` and `attach-entity`

Both forms update an interpreter role with freshly created entity metadata, but
they target different lifetimes:

- Use `(spawn-entity …)` when you want a brand-new actor in the runtime. This
  matches the default Duet agent experience (each Claude Code instance gets its
  own actor/facet pair) and is appropriate for remote integrations or anything
  that should keep state outside the interpreter process.
- Use `(attach-entity …)` when you want to host a helper entity inside the
  interpreter’s own actor. Attached entities share the interpreter’s lifecycle
  and are ideal for lightweight utilities, observers, or mock agents used
  during testing.

The interpreter records both cases with `(interpreter-entity …)` assertions that
carry the resolved actor/facet ids. Replay and time-travel hydrate those
records first, then re-run the corresponding spawn/attach outputs so downstream
patterns continue to function deterministically.

### Building Helper Functions

Reusable orchestration logic belongs in ordinary functions. For example, the
two-agent demo (`examples/workflows/two_agents.duet`, mirrored automatically into
`.duet/programs/examples/two_agents.duet`) defines a single helper that addresses any role:

```
(defn send-request (role request-id prompt)
  (action
    (assert (record agent-request
                    (role-property role "entity")
                    request-id
                    prompt))))
```

By funnelling prompts through helpers like this, programs avoid duplication
when introducing more agents or alternate conversation flows. Future standard
library modules will package common helpers so workflows can `(require
lang/agents)` instead of redefining them inline.

## Syntax Reference

The surface language is an S-expression grammar with the following tokens:

```
program   ::= form*
form      ::= list | atom
list      ::= '(' form* ')'
atom      ::= symbol | keyword | string | integer | float | boolean
symbol    ::= [^\s();"]+
keyword   ::= ':' symbol
string    ::= '"' (<any char except " or \> | escape)* '"'
escape    ::= '\\"' | '\\\\' | '\\n' | '\\r' | '\\t'
integer   ::= ['+'|'-']? [0-9]+
float     ::= ['+'|'-']? [0-9]+ '.' [0-9]+
boolean   ::= 'true' | 'false'
```

Whitespace is insignificant. Line comments start with `;` and continue to the
end of the line.

## Compiler / Interpreter Pipeline

1. **Parse** the S-expression into an AST (`WorkflowProgram`).
2. **Validate / Build IR** – translate forms into a typed `ProgramIr`, resolving
   roles, states, actions, waits, and branches.
3. **Instantiate** – spawn an interpreter entity per instance with:
   * In-memory state (current state name, pending waits, request tags).
   * Dataspace handles for logging progress.
4. **Execute** – interpret instructions turn-by-turn, emitting runtime outputs
   through the activation context so the journal captures every effect.

An AOT compiler could eventually translate workflows into a set of actors/facets
without a runtime interpreter, but the first milestone is an interpreter entity
that consumes the AST.

## CLI Touchpoints

The CLI exposes the interpreter through existing command groups:

* `duet query workflows` – lists interpreter definitions, active instances, and example programs
  discovered under `.duet/programs`.
* `duet run workflow-start path/to/program.duet [--label feature-42]` – starts a workflow using the
  supplied source.
* `duet run workflow-start --interactive path/to/program.duet` – launches the Rich TUI to follow
  execution and satisfy prompts in place.

Additional management commands (pause/resume, definition registration from dataspace, richer
inspection) will land under the existing `run` / `query` namespaces instead of a dedicated
`workflow` subgroup.

## Next Steps

1. Extend the parser/IR builder with macro/templating support as needed.
2. Implement additional service RPCs for pausing, resuming, and enumerating historical instances.
3. Flesh out CLI affordances (selecting instances, interactive resume) on top of the `run` / `query`
   groups.
4. Provide template definitions for common orchestrations (planner/worker, self-review, etc.).
