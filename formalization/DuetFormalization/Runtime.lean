namespace Duet

open Classical

universe u v w

/-- Actors are represented as natural-number identifiers in the model. -/
abbrev ActorId := Nat

/-- Runtime events that may be exchanged between actors.  Payloads are omitted
because the reversible semantics only depends on their structural role. -/
inductive Event
| assert
| retract
| message
| sync
| stopEntity
| stopFacet
deriving DecidableEq, Inhabited, Repr

/-- Event emitted from a turn together with its destination actor. -/
structure OutgoingEvent where
  actor : ActorId
  event : Event
deriving DecidableEq, Inhabited, Repr

/-- Metadata recorded for stochastic turns so that replay can reuse the original
sample and its log-probability.  The `aux` payload is user-defined data that
weighting functions may depend on (e.g., observation features). -/
structure SampleMemory (Val : Type u) (Aux : Type v) (Log : Type w) where
  value   : Val
  logProb : Log
  aux     : Aux
deriving Repr

/-- Turn records track the processed event, produced events, and (optionally)
the stochastic memory when the turn corresponded to a random draw. -/
structure TurnRecord (Val : Type u) (Aux : Type v) (Log : Type w) where
  actor    : ActorId
  processed : Event
  produced  : List OutgoingEvent
  sample?   : Option (SampleMemory Val Aux Log)
deriving Repr

/-- Global runtime state containing the per-actor mailboxes and the committed
turn history (newest turn at the head). -/
@[ext]
structure State (Val : Type u) (Aux : Type v) (Log : Type w) where
  mailboxes : ActorId → List Event
  history   : List (TurnRecord Val Aux Log)

variable {Val : Type u} {Aux : Type v} {Log : Type w}

/-- Abstract function describing the mailbox updates performed when a turn is
committed.  The third argument captures the residual queue of the actor whose
event was processed (i.e. the tail after removing the consumed event). -/
axiom applyMailboxes :
  (ActorId → List Event) → TurnRecord Val Aux Log → List Event → (ActorId → List Event)

/-- Abstract function describing the mailbox updates required to revert the
effects of a committed turn.  It uses the same residual queue that was present
when the turn executed. -/
axiom revertMailboxes :
  (ActorId → List Event) → TurnRecord Val Aux Log → List Event → (ActorId → List Event)

/-- Reverting immediately after applying a turn restores the original mailbox
state.  This captures the key reversible runtime invariant. -/
axiom revert_apply :
  ∀ (mail : ActorId → List Event) (record : TurnRecord Val Aux Log) (rest : List Event),
    revertMailboxes (applyMailboxes mail record rest) record rest = mail

/-- Construct the successor state produced by committing a turn. -/
noncomputable def forwardState (σ : State Val Aux Log) (record : TurnRecord Val Aux Log)
    (rest : List Event) : State Val Aux Log :=
  { mailboxes := applyMailboxes σ.mailboxes record rest,
    history := record :: σ.history }

/-- Forward-step relation: committing `record` transforms `σ` into `τ`. -/
def Forward (σ τ : State Val Aux Log) : Prop :=
  ∃ record rest,
    σ.mailboxes record.actor = record.processed :: rest ∧
    τ.mailboxes = applyMailboxes σ.mailboxes record rest ∧
    τ.history = record :: σ.history

/-- Backward-step relation: undoing the most recent record transforms `σ` into `τ`. -/
def Backward (σ τ : State Val Aux Log) : Prop :=
  ∃ record rest,
    σ.history = record :: τ.history ∧
    τ.mailboxes = revertMailboxes σ.mailboxes record rest

/-- A backward step taken immediately after the corresponding forward step
returns the runtime to its previous state. -/
theorem backward_forward {σ : State Val Aux Log} {record : TurnRecord Val Aux Log}
    {rest : List Event}
    (h : σ.mailboxes record.actor = record.processed :: rest) :
    let σ' := forwardState (Val := Val) (Aux := Aux) (Log := Log) σ record rest
    Backward σ' σ := by
  intro σ'
  refine ⟨record, rest, ?_, ?_⟩
  · simp [σ', forwardState]
  ·
    have := revert_apply (Val := Val) (Aux := Aux) (Log := Log) σ.mailboxes record rest
    simpa [σ', forwardState] using this.symm

/-- A committed forward step can always be undone immediately, recovering the
previous runtime state. -/
theorem Forward.backward {σ τ : State Val Aux Log}
    (h : Forward (Val := Val) (Aux := Aux) (Log := Log) σ τ) :
    Backward (Val := Val) (Aux := Aux) (Log := Log) τ σ := by
  rcases h with ⟨record, rest, hEvent, hMail, hHist⟩
  have τ_eq :
      τ = forwardState (Val := Val) (Aux := Aux) (Log := Log) σ record rest := by
    cases τ
    cases hHist
    cases hMail
    rfl
  subst τ_eq
  exact backward_forward (Val := Val) (Aux := Aux) (Log := Log)
    (σ := σ) (record := record) (rest := rest) hEvent

end Duet
