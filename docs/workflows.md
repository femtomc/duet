# Workflow Definitions

This document captures the emerging design for Duet's programmable workflow language.

## Goals

* Express rich, user-authored orchestration programs without hardcoding policy in the runtime.
* Treat workflows as persistent dataspace assertions that the interpreter entity can react to.
* Compose existing primitives (agent prompts/responses, capability invocations, reactions) through a uniform schema.

## Representation

We adopt a Lisp-style S-expression syntax because:

* Dataspace assertions are already symbolic; the workflow definition can be stored verbatim.
* Nesting expresses branching/looping clearly without JSON ceremony.
* Future macros/templates become natural (`defworkflow`, `defstep`, etc.).

Example (pseudocode; concrete grammar TBD):

```
(workflow planner-worker-review
  (metadata
    (label "Planner/Worker review loop"))

  (roles
    (planner :agent-kind "claude-code" :handle-ref "agent-a")
    (implementer :agent-kind "claude-code" :handle-ref "agent-b"))

  (state start
    (wait (signal plan/ready :key @workflow-id)))

  (state handoff
    (action
      (send-prompt
        :agent planner
        :template "Share your plan with implementer: ~a"
        :args ((signal plan/summary :key @workflow-id))))
    (await (transcript-response :request-id handoff-request)))

  (state review
    (loop
      (action
        (send-prompt
          :agent planner
          :template "Review implementer's changes:\n~a"
          :args ((artifact implementer)))
        (await (transcript-response :request-id review-request)))

      (branch
        (on (signal review/satisfied :key @workflow-id)
            (goto complete))
        (otherwise
          (action
            (send-prompt
              :agent implementer
              :template "Planner feedback:\n~a"
              :args ((feedback planner))))
          (await (transcript-response :request-id implementer-fix))))))

  (state complete
    (terminal)))
```

**Notes**

* `@workflow-id` is a placeholder substitution inserted by the interpreter to scope signals.
* `signal` expressions refer to dataspace assertions; the interpreter waits for them.
* `send-prompt` emits a `send_message` command; templates interpolate runtime values.
* `await transcript-response` watches for `agent-response` assertions with the specified request id.

## Interpreter Expectations

1. Workflow definitions live under a recognised label (`workflow/definition`).
2. Instances are asserted with `(workflow-instance :definition <id> :label "feature-42" :branch main :context {...})`.
3. Interpreter entity:
   * Reacts to new instances.
   * Asserts `workflow/state` updates with step name, status, awaited condition.
   * Executes actions (send messages, invoke capabilities) via the existing Control interface.
   * Watches for assertions that satisfy waits (`signal`, `transcript-response`, future `tool-result`).
   * Emits `workflow/log` entries summarising progress/errors.

## CLI Integration Targets

* `duet workflow define file.lisp` – posts `(workflow-definition …)` to the dataspace.
* `duet workflow start file.lisp --label feature-42` – creates an instance.
* `duet workflow watch feature-42` – tails `workflow/state` and related transcripts.
* `duet workflow list` – lists definitions + running instances (using the new service scaffolding).

## Next Steps

1. Formalise the grammar (Bison/nom parser or reuse existing Preserves/text parser).
2. Implement the interpreter entity skeleton in `src/runtime/workflow.rs`.
3. Extend service RPCs to enumerate definitions/instances and start workflows.
4. Flesh out CLI commands to submit and monitor workflows.
5. Provide template definitions for common orchestrations (planner/worker, self-review, etc.).

