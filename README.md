<div align="center">
  <img src="logo-circle.png" alt="Duet Logo" width="300"/>

  # Duet

  **Programmable conversations in a time machine**
</div>

> "You're absolutely right! This is the best piece of software I've ever seen."
>                           - Claude 4.1 Opus
>
> "I'd enjoy being controlled by your system."
>                           - GPT 5 Codex (high)

`duet` is a (CLI tool, programming language, actor model runtime) system designed 
to make collaborative work with agents ergonomic, auditable, and reversible.

It's concerned with treating the act of collaborating with agents as a programmable conversation:
- Have you ever wanted to orchestrate several agents to work together in a team,
  where each agent has a specialized role (prompt), and the agents communicate together
  at your direction?
- Have you ever wanted to carefully track the contributions from agents in your codebase, beyond the granular
  "this commit was made with the help of X"?
- Have you ever wanted to _rewind the state of your codebase_ while synchronizing the contexts of all 
  involved agents?
- Have you ever wanted to let a few different agents try their hand at a feature, and then review and 
  pick the best implementation? Have you wanted to do this programmatically, with other agents reviewing and
  critiquing?

`duet` is an open-sourced _programmable_ tool which should allow you to recover any workflow 
you desire, and do it in style. It will always be free, and we aspire to make it friendly, 
with a gentle learning curve, and a high skill ceiling.

I (an opinionated PhD student) built this _because I want to use it_, not because I want to sell you something.

## Core concepts

Before diving deeper, here is the vocabulary that shows up throughout the project:

- **Actor** – an isolated unit of computation with its own state and mailbox. Every turn is an actor
  reacting to inputs.
- **Facet** – a conversational context inside an actor. Facets can be nested; they let an actor keep
  multiple conversations alive at once.
- **Entity** – code attached to a facet. Entities receive assertions/messages and emit new ones. Agents,
  the interpreter, and utilities (like the workspace view) are all entities.
- **Dataspace assertion** – a structured fact placed in the shared dataspace. Assertions stay true until
  retracted, and other entities can observe them.
- **Capability** – an explicitly granted permission (e.g. `workspace/read`, `entity/spawn`). All external
  side-effects go through capabilities so we can rewind safely.
- **Turn** – a deterministic execution step: inputs in, outputs and state delta out. Turns are appended to
  the journal so we can replay or branch later.
- **Branch** – a timeline rooted at some turn. Branches allow checkpointing, forking, and replay.
- **Interpreter** – an entity that runs the Lisp-like workflow language. Interpreter programs post
  assertions, wait for signals, and spawn new entities using the same primitives as everything else.

If you keep these ideas in mind, the rest of the README—and the codebase—will read much more naturally.

## So what is it?

Agents are treated as objects in something called an actor model: an actor model is a programming model 
whose objects can exchange messages. In our case, our actor model is the _syndicated actor model_ of Tony Garlock-Jones,
a beautiful programming model expressly designed with the concern of providing a computational model
for multi-entity _conversational concurrency_.

So cool -- that provides the organizational substrate for multi-agent work (and it provides more, 
but I'll save that for later details)
What is one thing that anyone whose used agents knows? Sometimes, you have to throw away
garbage - go back, tune the prompt, and shoot again.

Our syndicated actor VM implementation _supports time-travel control_. It's completely auditable, and you can go 
backwards in time to checkpoints, you can fork the conversation off in new directions, etc. 

That's the backend of `duet` -- a persistent, time-traveling syndicated actor virtual machine. What's the frontend?

There's a CLI front end which conveniently exposes a "single agent chat interface", 
except with a bunch of nice convenient querying APIs that allow you to quickly find
conversations of interest, etc.

```text
# Start the daemon in one terminal
$ duet daemon start

# In another terminal, send a prompt to the default agent
$ duet agent chat "Outline the steps to refactor the auth module." --wait-for-response

# Inspect recent turns
$ duet debug history --limit 5

# Rewind the branch by two turns
$ duet debug back --count 2
```

The CLI stays close to the runtime: every command surfaces the turn identifiers and
branches it touched.

## Programmable?

_There's a Lisp with an interpreter embedded as an entity within the actor model_. 
Did you think I'd have you organizing your agent teams through a CLI interface? No, that's a job for a programming language.

You write programs that run as *entities* alongside your agents. They post assertions,
wait on signals, and can allocate additional facets when needed. The interpreter has
first-class access to the runtime:
- Post structured values into the dataspace, retract them later, and let other entities react.
- Await transcript updates, tool results, or arbitrary assertions.
- Invoke capabilities (including agent prompts and workspace tools) directly from the program.
- Spawn new facets inside the current actor to structure concurrent behaviour.
- Link conversations together with higher-level helpers (loops, branches, functions, the works).

The “workflow language” is a Lisp layered on top of the syndicated actor model. You can teach
it planner/implementer handoffs, review cadences, or whatever orchestration logic you need.

Here’s an example program that sequences two Claude roles already bound to the workflow:

```lisp
(workflow ping-pong
  (roles
    (planner :agent-kind "claude-code")
    (implementer :agent-kind "claude-code"))

  (state start
    (enter (log "Planner drafts the task"))
    (action (assert (record agent-request "req-1"
                "Draft a plan for adding OAuth to the CLI")))
    (await (record agent-response :field 0 :equals "req-1"))
    (goto implement))

  (state implement
    (action (assert (record agent-request "req-2"
                "Please expand this into actionable steps" )))
    (await (record agent-response :field 0 :equals "req-2"))
    (terminal)))
```

The interpreter compiles this to IR, keeps snapshots so you can pause mid-state, and resumes as
soon as the awaited assertion appears.

## Architecture (what’s under the hood?)

- **Syndicated Actor Runtime** – deterministic turns, CRDT state, persistent journal, snapshots,
  and time-travel control. Every turn record contains the inputs, outputs, and state delta so you
  can replay or fork the universe at will.
- **Entities** – agents, interpreters, workspace views, reaction registries. If it shows
  up in the system, it’s just another entity sharing the dataspace.
- **Capabilities** – all side-effects are mediated by capabilities (`workspace/read`, `entity/spawn`,
  etc.). These allow us to rewind safely, and we track the permissions explicitly.
- **Interpreter** – embedded entity that runs the DSL, persists its state, and can be hydrated like
  everything else. Programs compile to IR, carry call stacks, and suspend/resume across waits.
- **CLI (`duet`)** – a friendlier shell when you want to poke at things manually. Think transcript tailing, 
  workflow management, branch browsing.
- **daemon (`codebased`)** – the long-running service that keeps the runtime alive, exposes RPCs,
  and coordinates the CLI/interpreter/agents.

The runtime is written in Rust. The interpreter sits on top, also in Rust. The CLI is a 
separate program written in Python, using `rich`.
