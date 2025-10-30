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
as control-plane requests (send message, invoke capability, emit assertion) and
waits until the declared conditions occur in the dataspace.

Persistence, time-travel, and branching remain automatic; workflow execution is
just another stream of turns.

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
      (emit (log "Planner drafting plan")))
    (wait (signal plan/ready :scope @workflow-id)))

  (state handoff
    (action
      (send-prompt
        :agent implementer
        :template "Please implement the following plan:\n~a"
        :args ((signal-value plan/summary :scope @workflow-id))
        :tag handoff-request))
    (await (record agent-response :field 0 :equals handoff-request)))

  (state review
    (loop
      (action
        (send-prompt
          :agent planner
          :template "Review implementer's diff:\n~a"
          :args ((last-artifact implementer))
          :tag review-request))
      (await (record agent-response :field 0 :equals review-request))

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
          (await (record agent-response :field 0 :equals implementer-fix))))))

  (state complete
    (enter (emit (log "Workflow finished")))
    (terminal)))
```

**Notes**

* `@workflow-id` is a placeholder substitution inserted by the interpreter to scope signals.
* `signal` expressions refer to dataspace assertions; the interpreter waits for them.
* `send-prompt` emits a `send_message` command; templates interpolate runtime values.
* `await (record agent-response :field 0 :equals ...)` watches for `agent-response` assertions tagged with the supplied identifier.

## Interpreter Expectations

1. Workflow definitions live under a recognised label (`workflow/definition`).
2. Instances are asserted with
   `(workflow-instance :definition <id> :label "feature-42" :branch main :context {...})`.
3. Interpreter entity:
   * Reacts to new instances.
   * Asserts `workflow/state` updates with step name, status, awaited condition.
   * Executes actions (send messages, invoke capabilities) via the existing Control interface.
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

Conditions (non-exhaustive):

* `(signal <label> :scope … :fields …)` – waits for an assertion with the given label and optional filters.
* `(record <label> :field <index> :equals <value>)` – waits for a dataspace record whose field matches the supplied value (e.g. agent responses tagged by request id).
* `(tool-result :tag <id>)` – future extension for capability completions.

Actions (non-exhaustive):

* `(send-prompt :agent <role> :template <fmt> :args (<expr> …) :tag <id>)`
* `(invoke-tool :role <role> :capability <symbol> :payload <expr> :tag <id>)`
* `(emit (log <text>))`
* `(emit (assert <value>))`
* `(emit (retract <value>))`

Expressions available in templates / args may include:

* `(signal-value <label> :scope … :field <n>)`
* `(transcript extract-feedback <tag>)`
* `(last-artifact <role>)`

These derive from the standard library modules. The core interpreter only understands
action/condition skeletons; specific helpers are provided by modules.

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
4. **Execute** – interpret instructions turn-by-turn, emitting runtime commands
   via the existing `Control` facade.

An AOT compiler could eventually translate workflows into a set of actors/facets
without a runtime interpreter, but the first milestone is an interpreter entity
that consumes the AST.

## CLI Integration Targets

* `duet workflow define file.lisp` – posts `(workflow-definition …)` to the dataspace.
* `duet workflow start file.lisp --label feature-42` – creates an instance.
* `duet workflow watch feature-42` – tails `workflow/state` and related transcripts.
* `duet workflow list` – lists definitions + running instances (using the new service scaffolding).

## Next Steps

1. Extend the parser/IR builder with macro/templating support as needed.
2. Implement the interpreter runtime in `src/interpreter/runtime.rs` so programs
   emit real actor VM actions.
3. Extend service RPCs to enumerate definitions/instances and start programs (currently stubs).
4. Flesh out CLI commands (`duet workflow define/start/watch/list`).
5. Provide template definitions for common orchestrations (planner/worker, self-review, etc.).
