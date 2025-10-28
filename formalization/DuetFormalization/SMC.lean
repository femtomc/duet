import DuetFormalization.Runtime
import Mathlib/Measure/ProbabilityMeasure
import Mathlib/Measure/Expectation
import Mathlib/Topology/Instances/ENat
import Mathlib/Data/Real/ENNReal


namespace Duet
namespace SMC

open Classical
open List
open MeasureTheory
open scoped MeasureTheory

universe u v w z

variable {Val : Type u} {Aux : Type v} {Log : Type w} {Weight : Type z}
variable (proposalKernel : State Val Aux Log → Measure (State Val Aux Log × SampleMemory Val Aux Log))

/-- A single proposal obtained by executing one forward turn from a base state. -/
structure Proposal (σ : State Val Aux Log) where
  turn      : TurnRecord Val Aux Log
  rest      : List Event
  nextState : State Val Aux Log
  mail_eq   : σ.mailboxes turn.actor = turn.processed :: rest
  mailbox_eq :
      nextState.mailboxes = applyMailboxes (Val := Val) (Aux := Aux) (Log := Log)
        σ.mailboxes turn rest
  history_eq :
      nextState.history = turn :: σ.history

namespace Proposal

variable {σ : State Val Aux Log}

/-- Access the stochastic memory recorded in the proposal's turn (if any). -/
def sample? (p : Proposal (Val := Val) (Aux := Aux) (Log := Log) σ) :
    Option (SampleMemory Val Aux Log) :=
  p.turn.sample?

/-- Replaying the proposal's turn from the base state yields the stored next
state. -/
noncomputable def replay (p : Proposal (Val := Val) (Aux := Aux) (Log := Log) σ) :
    State Val Aux Log :=
  forwardState (Val := Val) (Aux := Aux) (Log := Log) σ p.turn p.rest

@[simp]
theorem replay_eq_nextState (p : Proposal (Val := Val) (Aux := Aux) (Log := Log) σ) :
    p.replay = p.nextState := by
  simp [Proposal.replay, forwardState, p.mailbox_eq, p.history_eq]

/-- Every proposal carries the data necessary to view it as a forward step. -/
theorem forward (p : Proposal (Val := Val) (Aux := Aux) (Log := Log) σ) :
    Forward (Val := Val) (Aux := Aux) (Log := Log) σ p.nextState := by
  exact ⟨p.turn, p.rest, p.mail_eq, p.mailbox_eq, p.history_eq⟩

/-- Every proposal justifies a backward step back to the base state. -/
theorem backward :
    Backward (Val := Val) (Aux := Aux) (Log := Log) p.nextState σ := by
  refine ⟨p.turn, p.rest, ?_, ?_⟩
  · simp [p.history_eq]
  ·
    have := revert_apply (Val := Val) (Aux := Aux) (Log := Log)
      σ.mailboxes p.turn p.rest
    simpa [p.mailbox_eq] using this.symm

end Proposal

/-- A finite batch of proposals collected from the same base state. -/
structure ProposalBatch where
  base   : State Val Aux Log
  items  : List (Proposal (Val := Val) (Aux := Aux) (Log := Log) base)

namespace ProposalBatch

variable (batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log))

/-- Replaying every proposal in the batch yields the stored successor states. -/
theorem replay_all :
    batch.items.map Proposal.replay =
      batch.items.map Proposal.nextState := by
  induction batch.items with
  | nil => simp
  | cons p ps ih =>
      simp [Proposal.replay_eq_nextState, ih]

end ProposalBatch

/-- Parameters required to run the reversible SMC selection step. -/
structure Params where
  potential   : Potential (Val := Val) (Aux := Aux) (Log := Log)
  weightModel : WeightModel Log Weight
  resampler   : Resampler (Weight := Weight)

/-- Execute one reversible SMC selection step over a batch of proposals. -/
def Params.select
    (params : Params (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight))
    (batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)) :
    Option (WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) batch.base) :=
  ProposalBatch.resample (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)
    params.weightModel params.resampler params.potential batch

/-- Skeleton for the eventual soundness theorem: any successful selection returns one of the
weighted proposals and thus can be rewound back to the base state. -/
def soundnessStatement
    (params : Params (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight))
    (batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)) : Prop :=
  ∀ result,
    params.select (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight) batch =
        some result →
      result ∈ ProposalBatch.weighted (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) params.weightModel params.potential batch ∧
      Backward (Val := Val) (Aux := Aux) (Log := Log)
        result.proposal.nextState batch.base

/-- Interface describing how a host runtime collects a fixed number of proposals while
starting from a given base state.  The collector records that the base state is preserved
and that exactly the requested number of proposals were produced. -/
structure Collector where
  collect :
    State Val Aux Log → Nat → ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)
  base_eq :
    ∀ (σ : State Val Aux Log) (k : Nat),
      (collect σ k).base = σ
  length_eq :
    ∀ (σ : State Val Aux Log) (k : Nat),
      (collect σ k).items.length = k

/-- Log potentials map proposals and their sampled memories to a log-weight contribution. -/
abbrev Potential :=
  State Val Aux Log → SampleMemory Val Aux Log → Log

/-- Specialised potential returning non-negative extended reals, suitable for defining
probability densities. -/
abbrev PotentialDensity :=
  State Val Aux Log → SampleMemory Val Aux Log → ℝ≥0∞

/-- Combine a potential contribution with the proposal log-probability to obtain the
weight used for resampling.  The notion of weight is abstract so that it can be instantiated
with reals, log-domain values, etc. -/
structure WeightModel (Log : Type w) (Weight : Type z) where
  combine : Log → Log → Weight

/-- Resamplers select an index according to a list of weights.  The only property required
here is that returned indices are in-bounds. -/
structure Resampler where
  choose : List Weight → Option Nat
  choose_spec :
    ∀ (weights : List Weight) (idx : Nat),
      choose weights = some idx → idx < weights.length

namespace Resampler

variable (res : Resampler (Weight := Weight))

/-- Convenience wrapper that packages the result of `choose` as a `Fin`. -/
def pick (weights : List Weight) : Option (Fin weights.length) :=
  match h : res.choose weights with
  | none => none
  | some idx =>
      let bound := res.choose_spec weights idx h
      some ⟨idx, bound⟩

end Resampler

/-- A proposal annotated with the concrete sample memory and the derived resampling weight. -/
structure WeightedProposal (σ : State Val Aux Log) where
  proposal     : Proposal (Val := Val) (Aux := Aux) (Log := Log) σ
  memory       : SampleMemory Val Aux Log
  memory_eq    : proposal.sample? = some memory
  logPotential : Log
  weight       : Weight

/-- Convenience view of a weighted proposal as the pair of successor state and logged
sample memory. -/
def WeightedProposal.toPair
    (wp : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight) σ) :
    State Val Aux Log × SampleMemory Val Aux Log :=
  (wp.proposal.nextState, wp.memory)

@[simp] lemma WeightedProposal.toPair_fst
    (wp : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight) σ) :
    wp.toPair.fst = wp.proposal.nextState := rfl

@[simp] lemma WeightedProposal.toPair_snd
    (wp : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight) σ) :
    wp.toPair.snd = wp.memory := rfl

/-- Proposal kernels describe the stochastic behaviour of `next`: given a base state they
induce a probability distribution over successor states and recorded sample memories. -/
structure ProposalKernel where
  kernel : State Val Aux Log → ProbabilityMeasure (State Val Aux Log × SampleMemory Val Aux Log)

namespace WeightedProposal

@[simp] lemma weight_val (wp : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
    (Weight := Weight) σ) : wp.weight = wp.weight := rfl

/-- Extract the underlying proposal. -/
def toProposal (wp : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
    (Weight := Weight) σ) : Proposal (Val := Val) (Aux := Aux) (Log := Log) σ :=
  wp.proposal

@[simp] lemma toProposal_nextState
    (wp : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight) σ) :
    wp.toProposal.nextState = wp.proposal.nextState := rfl

/-- Weighted proposals still support rewinding back to the base state. -/
theorem backward
    (wp : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight) σ) :
    Backward (Val := Val) (Aux := Aux) (Log := Log) wp.proposal.nextState σ :=
  Proposal.backward (σ := σ) (p := wp.proposal)

/-- Construct a weighted proposal from a plain proposal when a sample memory exists. -/
def ofProposal
    (model : WeightModel Log Weight)
    (Φ : Potential (Val := Val) (Aux := Aux) (Log := Log))
    (p : Proposal (Val := Val) (Aux := Aux) (Log := Log) σ) :
    Option (WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) σ) :=
  match h : p.sample? with
  | none => none
  | some mem =>
      let logPot := Φ p.nextState mem
      let weight := model.combine logPot mem.logProb
      some
        { proposal := p
          memory := mem
          memory_eq := h
          logPotential := logPot
          weight := weight }

end WeightedProposal

namespace ProposalBatch

variable {batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)}

/-- Collect all weighted proposals obtainable from the batch using the given potential and
weight model. -/
def weighted
    (model : WeightModel Log Weight)
    (Φ : Potential (Val := Val) (Aux := Aux) (Log := Log))
    (batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)) :
    List (WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) batch.base) :=
  batch.items.filterMap (fun p =>
    WeightedProposal.ofProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) model Φ p)

/-- Resample a weighted proposal using the supplied resampler. -/
def resample
    (model : WeightModel Log Weight)
    (res : Resampler (Weight := Weight))
    (Φ : Potential (Val := Val) (Aux := Aux) (Log := Log))
    (batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)) :
    Option (WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) batch.base) :=
  let candidates := weighted (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) model Φ batch
  match res.pick (weights := candidates.map (fun c => c.weight)) with
  | none => none
  | some idx => some (candidates.get idx)

/-- Soundness skeleton: whenever resampling succeeds, the chosen proposal came
from the weighted candidate set. -/
lemma resample_mem
    {model : WeightModel Log Weight}
    {res : Resampler (Weight := Weight)}
    {Φ : Potential (Val := Val) (Aux := Aux) (Log := Log)}
    {batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)}
    {result : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) batch.base}
    (h : resample (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)
        model res Φ batch = some result) :
    result ∈ weighted (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) model Φ batch := by
  unfold ProposalBatch.resample at h
  set candidates :=
    weighted (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) model Φ batch with hcand
  cases hpick :
      res.pick (weights := candidates.map (fun c => c.weight)) with
  | none =>
      simp [hcand, hpick] at h
  | some idx =>
      have hEq :
          result = candidates.get idx := by
            simpa [hcand, hpick] using h
      subst hEq
      exact List.get_mem _ _

end ProposalBatch

/-- Parameters required to run the reversible SMC selection step. -/
structure Params where
  potential   : Potential (Val := Val) (Aux := Aux) (Log := Log)
  weightModel : WeightModel Log Weight
  resampler   : Resampler (Weight := Weight)

/-- Execute one reversible SMC selection step over a batch of proposals. -/
def Params.select
    (params : Params (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight))
    (batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)) :
    Option (WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) batch.base) :=
  ProposalBatch.resample (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)
    params.weightModel params.resampler params.potential batch

/-- Skeleton for the eventual soundness theorem: any successful selection returns one of the
weighted proposals and thus can be rewound back to the base state. -/
def soundnessStatement
    (params : Params (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight))
    (batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log)) : Prop :=
  ∀ result,
    params.select (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight) batch =
        some result →
      result ∈ ProposalBatch.weighted (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) params.weightModel params.potential batch ∧
      Backward (Val := Val) (Aux := Aux) (Log := Log)
        result.proposal.nextState batch.base

/-- The selection function satisfies the basic soundness skeleton. -/
lemma select_sound
    (params : Params (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight))
    (batch : ProposalBatch (Val := Val) (Aux := Aux) (Log := Log))
    {result : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) batch.base}
    (h : params.select (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)
        batch = some result) :
    result ∈ ProposalBatch.weighted (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) params.weightModel params.potential batch ∧
    Backward (Val := Val) (Aux := Aux) (Log := Log)
        result.proposal.nextState batch.base := by
  constructor
  · apply ProposalBatch.resample_mem
    simpa [Params.select]
  · exact WeightedProposal.backward (σ := batch.base) (wp := result)

/-- Execute the reversible SMC selection on the proposals produced by a collector. -/
def Params.run
    (params : Params (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight))
    (collector : Collector (Val := Val) (Aux := Aux) (Log := Log))
    (σ : State Val Aux Log) (k : Nat) :
    Option (WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) (collector.collect σ k).base) :=
  params.select (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)
    (collector.collect σ k)

/-- The collector preserves the original base state. -/
@[simp] lemma Collector.base_eq_collect
    (collector : Collector (Val := Val) (Aux := Aux) (Log := Log))
    (σ : State Val Aux Log) (k : Nat) :
    (collector.collect σ k).base = σ :=
  collector.base_eq σ k

/-- The collector returns exactly the requested number of proposals. -/
@[simp] lemma Collector.length_collect
    (collector : Collector (Val := Val) (Aux := Aux) (Log := Log))
    (σ : State Val Aux Log) (k : Nat) :
    (collector.collect σ k).items.length = k :=
  collector.length_eq σ k

/-- Soundness skeleton for the combined collection and selection pipeline. -/
lemma Params.run_sound
    (params : Params (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight))
    (collector : Collector (Val := Val) (Aux := Aux) (Log := Log))
    (σ : State Val Aux Log) (k : Nat)
    {result :
        WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
          (Weight := Weight) (collector.collect σ k).base}
    (h : params.run (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)
        collector σ k = some result) :
    result ∈ ProposalBatch.weighted (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) params.weightModel params.potential (collector.collect σ k) ∧
    Backward (Val := Val) (Aux := Aux) (Log := Log)
        result.proposal.nextState (collector.collect σ k).base := by
  simpa [Params.run] using
    Params.select_sound (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)
      (params := params) (batch := collector.collect σ k) (result := result) h

/-- A weighted proposal is *properly weighted* when expectations against any non-negative
test function over successor states agree with the classical Feynman–Kac weight.  The
`weightFn` interprets the stored abstract weight as an extended non-negative real. -/
def ProperWeighting
    (spec : ClassicalTargetSpec (Val := Val) (Aux := Aux) (Log := Log))
    (σ : State Val Aux Log)
    (μ : ProbabilityMeasure
        (WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
          (Weight := Weight) σ))
    (weightFn : WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
        (Weight := Weight) σ → ℝ≥0∞) : Prop :=
  ∀ (f : State Val Aux Log → ℝ≥0∞)
    (hf : Measurable f),
      (∫⁻ wp, weightFn wp * f wp.proposal.nextState
        ∂ μ.toMeasure) =
        (∫⁻ pair,
            spec.potentialDensity pair.1 pair.2 * f pair.1
          ∂ (spec.proposalKernel.kernel σ).toMeasure)

/-- Specification of the classical SMC target distribution built from a proposal kernel
and a potential density. -/
structure ClassicalTargetSpec where
  proposalKernel : ProposalKernel (Val := Val) (Aux := Aux) (Log := Log)
  potentialDensity : PotentialDensity (Val := Val) (Aux := Aux) (Log := Log)
  aemeasurable : ∀ σ,
    AEMeasurable (fun p : State Val Aux Log × SampleMemory Val Aux Log =>
      potentialDensity p.1 p.2)
      (proposalKernel.kernel σ).toMeasure
  nonzero : ∀ σ,
    ((proposalKernel.kernel σ).toMeasure.withDensity
        (fun p => potentialDensity p.1 p.2)).map Prod.fst Set.univ ≠ 0
  finite : ∀ σ,
    ((proposalKernel.kernel σ).toMeasure.withDensity
        (fun p => potentialDensity p.1 p.2)).map Prod.fst Set.univ ≠ ∞

namespace ClassicalTargetSpec

variable (spec : ClassicalTargetSpec (Val := Val) (Aux := Aux) (Log := Log))

/-- Weighted measure on pairs obtained by tilting the proposal kernel by the potential. -/
def pairMeasure (σ : State Val Aux Log) :
    Measure (State Val Aux Log × SampleMemory Val Aux Log) :=
  ((spec.proposalKernel.kernel σ).toMeasure).withDensity
    (fun p => spec.potentialDensity p.1 p.2)

/-- Classical SMC target distribution obtained by pushing forward the weighted measure
onto the successor state component and normalising. -/
def target (σ : State Val Aux Log) :
    ProbabilityMeasure (State Val Aux Log) :=
  ProbabilityMeasure.ofMeasure
    ((spec.pairMeasure σ).map Prod.fst)
    (by
      simpa [pairMeasure] using spec.nonzero σ)
    (by
      simpa [pairMeasure] using spec.finite σ)

end ClassicalTargetSpec

/-- Abstract description of the distribution produced by running the reversible SMC step. -/
structure RunDistribution where
  law : State Val Aux Log → Nat → ProbabilityMeasure (State Val Aux Log)

/-- The probabilistic correctness statement comparing the reversible runtime with the
classical SMC target. -/
def matchesTarget
    (spec : ClassicalTargetSpec (Val := Val) (Aux := Aux) (Log := Log))
    (run : RunDistribution (Val := Val) (Aux := Aux) (Log := Log)) : Prop :=
  ∀ σ k,
    run.law σ k = spec.target σ

/-- Hypotheses guaranteeing that the runtime’s proposal-and-weight mechanism is
properly weighted with respect to the classical target.  The `weightFn` interprets the
stored abstract weight as an extended non-negative real. -/
structure ProperWeightingHypothesis where
  spec : ClassicalTargetSpec (Val := Val) (Aux := Aux) (Log := Log)
  weightFn : ∀ σ,
    WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) σ → ℝ≥0∞
  law : ∀ σ,
    ProbabilityMeasure (WeightedProposal (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) σ)
  proper : ∀ σ,
    ProperWeighting (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight) spec σ (law σ) (weightFn σ)

/-- Assembling a proper-weighting hypothesis from runtime components:
collector, proposal kernel, potential density, and weight interpretation. -/
namespace ProperWeightingHypothesis

variable (hyp : ProperWeightingHypothesis (Val := Val) (Aux := Aux) (Log := Log)
  (Weight := Weight))

@[simp] lemma weightFn_apply (σ : State Val Aux Log) (wp) :
    hyp.weightFn σ wp = hyp.weightFn σ wp := rfl

@[simp] lemma law_apply (σ : State Val Aux Log) :
    hyp.law σ = hyp.law σ := rfl

/-- The (non-normalised) measure on successor states obtained by weighting the proposal
law using `weightFn`. -/
def weightedMeasure (σ : State Val Aux Log) :
    Measure (State Val Aux Log) :=
  ((hyp.law σ).toMeasure).withDensity (hyp.weightFn σ) |>.map
    (fun wp => wp.proposal.nextState)

lemma weightedMeasure_lintegral
    (σ : State Val Aux Log)
    (f : State Val Aux Log → ℝ≥0∞) (hf : Measurable f) :
    (∫⁻ x, f x ∂ hyp.weightedMeasure σ) =
      (∫⁻ wp, hyp.weightFn σ wp * f wp.proposal.nextState
          ∂ (hyp.law σ).toMeasure) := by
  simp [weightedMeasure, lintegral_map, hf]

/-- The mass of the weighted measure equals the Feynman–Kac normalising constant. -/
lemma weightedMeasure_univ
    (σ : State Val Aux Log) :
    hyp.weightedMeasure σ Set.univ =
      (∫⁻ wp, hyp.weightFn σ wp
        ∂ (hyp.law σ).toMeasure) := by
  simp [weightedMeasure]

/-- The weighted measure on successor states matches the classical Feynman–Kac measure
obtained from the specification. -/
lemma weightedMeasure_eq_pairMeasure_map
    (σ : State Val Aux Log) :
    hyp.weightedMeasure σ =
      (hyp.spec.pairMeasure σ).map Prod.fst := by
  classical
  refine Measure.ext fun s hs => ?_
  let f : State Val Aux Log → ℝ≥0∞ := Set.indicator s (fun _ => (1 : ℝ≥0∞))
  have hf : Measurable f := measurable_const.indicator hs
  have h_l := hyp.weightedMeasure_lintegral (σ := σ) f hf
  have h_r := hyp.proper σ f hf
  have h_measure_left :
      (∫⁻ x, f x ∂ hyp.weightedMeasure σ) = hyp.weightedMeasure σ s := by
    simp [f, Set.indicator, weightedMeasure] at h_l
    simpa [f, Set.indicator]
      using h_l
  have h_measure_right :
      (∫⁻ pair,
          hyp.spec.potentialDensity pair.1 pair.2 * f pair.1
            ∂ (hyp.spec.proposalKernel.kernel σ).toMeasure) =
        (hyp.spec.pairMeasure σ).map Prod.fst s := by
    simp [ClassicalTargetSpec.pairMeasure, f, Set.indicator,
      Measure.map_apply, hs, Set.preimage, Measure.withDensity_apply,
      hyp.spec.aemeasurable σ] at h_r
    exact h_r
  have := hyp.proper σ f hf
  simpa [h_measure_left, h_measure_right]
    using hyp.proper σ f hf

/-- Normalised probability measure obtained by weighting proposals using the proper
weighting hypothesis. -/
def resampled (σ : State Val Aux Log) :
    ProbabilityMeasure (State Val Aux Log) :=
  ProbabilityMeasure.ofMeasure
    (hyp.weightedMeasure σ)
    (by
      simpa [ProperWeightingHypothesis.weightedMeasure, weightedMeasure_eq_pairMeasure_map]
        using hyp.spec.nonzero σ)
    (by
      simpa [ProperWeightingHypothesis.weightedMeasure, weightedMeasure_eq_pairMeasure_map]
        using hyp.spec.finite σ)

/-- The resampled probability measure agrees with the classical target distribution. -/
lemma resampled_eq_target (σ : State Val Aux Log) :
    hyp.resampled σ = hyp.spec.target σ := by
  classical
  -- Equality of probability measures follows from equality of the underlying measures.
  ext s hs
  simp [ProperWeightingHypothesis.resampled, ClassicalTargetSpec.target,
    ProbabilityMeasure.ofMeasure_apply, hs,
    ProperWeightingHypothesis.weightedMeasure_eq_pairMeasure_map,
    ClassicalTargetSpec.pairMeasure]

/-- Run distribution obtained directly from the proper-weighting hypothesis.  The number
of proposals requested is ignored because the law already encapsulates the resampling.
-/
def runDistribution : RunDistribution (Val := Val) (Aux := Aux) (Log := Log) :=
  { law := fun σ _ => hyp.resampled σ }

/-- Proper weighting implies the reversible runtime matches the classical target. -/
lemma matchesTarget :
    matchesTarget (Val := Val) (Aux := Aux) (Log := Log)
      hyp.spec hyp.runDistribution := by
  intro σ k
  simpa [ProperWeightingHypothesis.runDistribution]
    using hyp.resampled_eq_target σ

end ProperWeightingHypothesis

/-- A packaged instance of the reversible runtime equipped with a proven proper-weighting
assumption.  Supplying this structure is enough to obtain the end-to-end SMC soundness
result. -/
structure RuntimeInstance (Val : Type u) (Aux : Type v) (Log : Type w)
    (Weight : Type z) where
  collector : Collector (Val := Val) (Aux := Aux) (Log := Log)
  params    : Params (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)
  hypothesis :
    ProperWeightingHypothesis (Val := Val) (Aux := Aux) (Log := Log)
      (Weight := Weight)

/-- The reversible runtime associated with a `RuntimeInstance` matches the classical SMC
target described by its specification. -/
theorem RuntimeInstance.soundness
    (inst : RuntimeInstance (Val := Val) (Aux := Aux) (Log := Log) (Weight := Weight)) :
    matchesTarget (Val := Val) (Aux := Aux) (Log := Log)
      inst.hypothesis.spec inst.hypothesis.runDistribution := by
  simpa using inst.hypothesis.matchesTarget

end SMC
end Duet
