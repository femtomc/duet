<div align="center">
  <img src="logo-circle.png" alt="Duet Logo" width="300"/>

  # Duet

  **Programmable conversations in a time machine**
</div>

`duet` is a (CLI tool, programming language, actor model, runtime) system designed 
to make collaborative work with agents (human, artificial, any kind!) as ergonomic as possible.

It's opinionated and powerful:
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

Today, each of these concerns is met with some shameless fork of VS Code or non-configurable logic added to
a new agentic harness, inside some "agentic IDE" written by some new start up that (probably, eventually) will
have you to pay a subscription model. It's embarrassing, really.

`duet` is an open-sourced _programmable_ tool based on careful design. It will always be free, it's 
extremely friendly and programmable, and it obviates the need for these (frankly) stupid agent wrappers.
I (a condescending PhD student who does research in programming languages) 
built this _because I want to use it_, not because I want to sell you something.

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
backwards in time to checkpoints, you can fork the conversation off in new directions, etc. Ultimately, this is what convinced 
me that the single agent chat interfaces are doomed. If one wants to control a team, the tools provided to you by Claude Code
and Codex are _woefully_ underthought.

That's the backend of `duet` -- a persistent, time-traveling syndicated actor virtual machine. What's the frontend?

There's a CLI front end which conveniently exposes a "single agent chat interface",
  except with a bunch of nice convenient querying APIs that allow you to quickly find
  conversations of interest, etc.

## Programmable?

Oh yeah! I forgot to mention: _there's a Lisp with an interpreter embedded as an entity within the actor model_. 
Did you think I'd have you organizing your agent teams through a CLI interface? No, that's a job for a programming language.
