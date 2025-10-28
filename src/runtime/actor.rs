//! Actors, facets, activation contexts, and entities
//!
//! Implements the Syndicated Actor model abstractions:
//! - Actors: isolated units of computation
//! - Facets: hierarchical scopes within actors
//! - Entities: behavior handlers attached to facets
//! - Activation: execution context for a turn

use parking_lot::RwLock;
use std::any::Any;
use std::collections::HashMap;
use std::sync::Arc;
use uuid::Uuid;

use super::error::{ActorError, ActorResult};
use super::pattern::{Pattern, PatternEngine};
use super::state::{
    AccountDelta, AssertionDelta, AssertionSet, CapId, CapabilityDelta, CapabilityMap,
    CapabilityMetadata, CapabilityStatus, CapabilityTarget, FacetDelta, FacetMap, FacetMetadata,
    FacetStatus, PNCounter, StateDelta,
};
use super::turn::{ActorId, FacetId, Handle, TurnInput, TurnOutput};

/// An actor: isolated unit of computation with its own state
pub struct Actor {
    /// Unique actor ID
    pub id: ActorId,

    /// Root facet
    pub root_facet: FacetId,

    /// All facets owned by this actor
    pub facets: Arc<RwLock<FacetMap>>,

    /// Assertions made by this actor
    pub assertions: Arc<RwLock<AssertionSet>>,

    /// Capabilities held by this actor
    pub capabilities: Arc<RwLock<CapabilityMap>>,

    /// Flow-control account
    pub account: Arc<RwLock<PNCounter>>,

    /// Entities attached to facets (tracked by instance ID)
    pub(crate) entities: Arc<RwLock<HashMap<FacetId, Vec<EntityEntry>>>>,

    /// Pattern engine for subscriptions
    pub pattern_engine: Arc<RwLock<PatternEngine>>,
}

pub(crate) struct EntityEntry {
    pub(crate) id: Uuid,
    pub(crate) entity_type: String,
    pub(crate) entity: Box<dyn Entity>,
}

impl Actor {
    /// Create a new actor
    pub fn new(id: ActorId) -> Self {
        let root_facet = FacetId::new();

        let mut facets = FacetMap::new();
        facets.facets.insert(
            root_facet.clone(),
            FacetMetadata {
                id: root_facet.clone(),
                parent: None,
                status: FacetStatus::Alive,
                actor: id.clone(),
            },
        );

        Self {
            id,
            root_facet,
            facets: Arc::new(RwLock::new(facets)),
            assertions: Arc::new(RwLock::new(AssertionSet::new())),
            capabilities: Arc::new(RwLock::new(CapabilityMap::new())),
            account: Arc::new(RwLock::new(PNCounter::new())),
            entities: Arc::new(RwLock::new(HashMap::new())),
            pattern_engine: Arc::new(RwLock::new(PatternEngine::new())),
        }
    }

    fn notify_assert(
        &self,
        activation: &mut Activation,
        handle: &Handle,
        value: &preserves::IOValue,
    ) -> ActorResult<()> {
        // Evaluate pattern engine
        let mut engine = self.pattern_engine.write();
        let pattern_matches = engine.eval_assert(handle, value);
        drop(engine);

        // Emit PatternMatched outputs
        for pattern_match in &pattern_matches {
            activation.outputs.push(TurnOutput::PatternMatched {
                pattern_id: pattern_match.pattern_id,
                handle: pattern_match.handle.clone(),
            });
        }

        // Dispatch on_assert callbacks
        let entities = self.entities.read();
        for pattern_match in pattern_matches {
            let engine = self.pattern_engine.read();
            if let Some(pattern) = engine.patterns.get(&pattern_match.pattern_id) {
                if let Some(entity_list) = entities.get(&pattern.facet) {
                    let prev_facet =
                        std::mem::replace(&mut activation.current_facet, pattern.facet.clone());
                    let result: ActorResult<()> = (|| {
                        for entry in entity_list {
                            activation.set_current_entity(Some(entry.id));
                            entry.entity.on_assert(
                                activation,
                                &pattern_match.handle,
                                &pattern_match.value,
                            )?;
                        }
                        Ok(())
                    })();
                    activation.set_current_entity(None);
                    activation.current_facet = prev_facet;
                    result?;
                }
            }
        }

        Ok(())
    }

    fn process_pending_asserts(&self, activation: &mut Activation) -> ActorResult<()> {
        loop {
            let pending = activation.drain_pending_asserts();
            if pending.is_empty() {
                break;
            }

            for (handle, value) in pending {
                self.notify_assert(activation, &handle, &value)?;
            }
        }

        Ok(())
    }

    /// Spawn a child facet
    pub fn spawn_facet(&self, parent: &FacetId) -> FacetId {
        let facet_id = FacetId::new();
        let mut facets = self.facets.write();

        facets.facets.insert(
            facet_id.clone(),
            FacetMetadata {
                id: facet_id.clone(),
                parent: Some(parent.clone()),
                status: FacetStatus::Alive,
                actor: self.id.clone(),
            },
        );

        facet_id
    }

    /// Terminate a facet and all its children
    pub fn terminate_facet(&self, facet_id: &FacetId) {
        let mut facets = self.facets.write();

        // Mark this facet as terminated
        if let Some(metadata) = facets.facets.get_mut(facet_id) {
            metadata.status = FacetStatus::Terminated;
        }

        // Find and terminate all children
        let children: Vec<_> = facets
            .facets
            .iter()
            .filter(|(_, meta)| meta.parent.as_ref() == Some(facet_id))
            .map(|(id, _)| id.clone())
            .collect();

        for child in children {
            self.terminate_facet(&child);
        }
    }

    /// Apply a state delta to the actor's internal CRDTs
    pub fn apply_delta(&self, delta: &StateDelta) {
        {
            let mut assertions = self.assertions.write();
            assertions.apply(&delta.assertions);
        }

        {
            let mut facets = self.facets.write();
            facets.apply(&delta.facets);
        }

        {
            let mut capabilities = self.capabilities.write();
            capabilities.apply(&delta.capabilities);
        }

        {
            let mut account = self.account.write();
            account.apply(&delta.accounts);
        }
    }

    /// Execute a turn with the given inputs
    pub fn execute_turn(
        &self,
        inputs: Vec<TurnInput>,
    ) -> ActorResult<(Vec<TurnOutput>, StateDelta)> {
        // Create activation context
        let mut activation = Activation::new(self.id.clone(), self.root_facet.clone());

        // Process each input
        for input in inputs {
            self.process_input(&mut activation, input)?;
        }

        // Collect outputs and delta
        let outputs = activation.outputs.clone();
        let delta = activation.build_delta();

        Ok((outputs, delta))
    }

    /// Process a single input
    fn process_input(&self, activation: &mut Activation, input: TurnInput) -> ActorResult<()> {
        match input {
            TurnInput::ExternalMessage { facet, payload, .. } => {
                // Deliver message to entities on this facet
                let entities = self.entities.read();
                if let Some(entity_list) = entities.get(&facet) {
                    let prev_facet =
                        std::mem::replace(&mut activation.current_facet, facet.clone());
                    let result: ActorResult<()> = (|| {
                        for entry in entity_list {
                            activation.set_current_entity(Some(entry.id));
                            entry.entity.on_message(activation, &payload)?;
                        }
                        Ok(())
                    })();
                    activation.set_current_entity(None);
                    activation.current_facet = prev_facet;
                    result?;
                }
            }

            TurnInput::Assert { handle, value, .. } => {
                self.notify_assert(activation, &handle, &value)?;
                activation.assert(handle.clone(), value.clone());
                activation.pop_last_pending_assert();
            }

            TurnInput::Retract { handle, .. } => {
                // Evaluate pattern engine
                let mut engine = self.pattern_engine.write();
                let affected_patterns = engine.eval_retract(&handle);
                drop(engine);

                // Generate PatternUnmatched outputs
                for pattern_id in &affected_patterns {
                    activation.outputs.push(TurnOutput::PatternUnmatched {
                        pattern_id: *pattern_id,
                        handle: handle.clone(),
                    });
                }

                // Call entity on_retract callbacks for affected patterns
                let entities = self.entities.read();
                for pattern_id in affected_patterns {
                    let engine = self.pattern_engine.read();
                    if let Some(pattern) = engine.patterns.get(&pattern_id) {
                        if let Some(entity_list) = entities.get(&pattern.facet) {
                            let prev_facet = std::mem::replace(
                                &mut activation.current_facet,
                                pattern.facet.clone(),
                            );
                            let result: ActorResult<()> = (|| {
                                for entry in entity_list {
                                    activation.set_current_entity(Some(entry.id));
                                    entry.entity.on_retract(activation, &handle)?;
                                }
                                Ok(())
                            })();
                            activation.set_current_entity(None);
                            activation.current_facet = prev_facet;
                            result?;
                        }
                    }
                }
                drop(entities);

                // Record the retraction
                activation.retract(handle);
            }

            TurnInput::Sync { facet, .. } => {
                activation.outputs.push(TurnOutput::Synced { facet });
            }

            TurnInput::CapabilityInvocation { capability, payload } => {
                self.handle_capability_invocation(activation, capability, payload)?;
            }

            _ => {
                // Handle other input types
            }
        }

        self.process_pending_asserts(activation)
    }

    fn handle_capability_invocation(
        &self,
        activation: &mut Activation,
        capability_id: CapId,
        payload: preserves::IOValue,
    ) -> ActorResult<()> {
        let metadata = {
            let capabilities = self.capabilities.read();
            capabilities
                .capabilities
                .get(&capability_id)
                .cloned()
        }
        .ok_or_else(|| {
            ActorError::InvalidActivation(format!(
                "Capability {} not found",
                capability_id
            ))
        })?;

        if metadata.status == CapabilityStatus::Revoked {
            return Err(ActorError::InvalidActivation(format!(
                "Capability {} has been revoked",
                capability_id
            )));
        }

        let issuer_entity = metadata.issuer_entity.ok_or_else(|| {
            ActorError::InvalidActivation(format!(
                "Capability {} is missing issuer entity metadata",
                capability_id
            ))
        })?;

        let facet_id = metadata.issuer_facet.clone();

        let mut entities = self.entities.write();
        let entity_list = entities
            .get_mut(&facet_id)
            .ok_or_else(|| ActorError::FacetNotFound(facet_id.0.to_string()))?;

        let entry = entity_list
            .iter_mut()
            .find(|entry| entry.id == issuer_entity)
            .ok_or_else(|| ActorError::InvalidActivation(format!(
                "Entity {} not found for capability {}",
                issuer_entity, capability_id
            )))?;

        let prev_facet =
            std::mem::replace(&mut activation.current_facet, facet_id.clone());
        activation.set_current_entity(Some(issuer_entity));
        let result = entry
            .entity
            .on_capability_invoke(activation, &metadata, &payload)?;
        activation.set_current_entity(None);
        activation.current_facet = prev_facet;

        activation.outputs.push(TurnOutput::CapabilityResult {
            capability: capability_id,
            result,
        });

        Ok(())
    }

    /// Attach an entity to a facet
    pub fn attach_entity(
        &self,
        entity_id: Uuid,
        entity_type: String,
        facet: FacetId,
        entity: Box<dyn Entity>,
    ) {
        let mut entities = self.entities.write();
        entities
            .entry(facet)
            .or_insert_with(Vec::new)
            .push(EntityEntry {
                id: entity_id,
                entity_type,
                entity,
            });
    }

    /// Detach an entity by ID
    pub fn detach_entity(&self, entity_id: Uuid) -> bool {
        let mut entities = self.entities.write();

        let mut removed = false;

        for (_facet, list) in entities.iter_mut() {
            let original_len = list.len();
            list.retain(|entry| entry.id != entity_id);
            if list.len() != original_len {
                removed = true;
            }
        }

        // Prune empty facets
        entities.retain(|_, list| !list.is_empty());

        removed
    }

    /// Register a pattern subscription
    pub fn register_pattern(&self, pattern: Pattern) -> uuid::Uuid {
        let assertions_snapshot = {
            let assertions = self.assertions.read();
            if assertions.active.is_empty() {
                None
            } else {
                Some(assertions.clone())
            }
        };

        let mut engine = self.pattern_engine.write();
        let pattern_id = engine.register(pattern.clone());

        if let Some(snapshot) = assertions_snapshot {
            engine.seed_matches_from_assertions(&pattern, &self.id, &snapshot);
        }

        pattern_id
    }

    /// Unregister a pattern subscription
    pub fn unregister_pattern(&self, pattern_id: uuid::Uuid) {
        let mut engine = self.pattern_engine.write();
        engine.unregister(pattern_id);
    }
}

/// Activation context: mutable state during a turn
pub struct Activation {
    /// Actor executing this turn
    pub actor_id: ActorId,

    /// Current facet context
    pub current_facet: FacetId,

    /// Root facet of the actor
    pub root_facet: FacetId,

    /// Outputs collected during this turn
    pub outputs: Vec<TurnOutput>,

    /// Assertions made
    pub assertions_added: Vec<(Handle, preserves::IOValue)>,

    /// Assertions retracted
    pub assertions_retracted: Vec<Handle>,

    /// Assertions emitted locally that still need pattern dispatch
    pending_asserts: Vec<(Handle, preserves::IOValue)>,

    /// Facets spawned
    pub facets_spawned: Vec<FacetMetadata>,

    /// Facets terminated
    pub facets_terminated: Vec<FacetId>,

    /// Flow-control: tokens borrowed
    pub tokens_borrowed: i64,

    /// Flow-control: tokens repaid
    pub tokens_repaid: i64,

    /// Capabilities granted during this turn
    pub capabilities_granted: Vec<CapabilityMetadata>,

    /// Capabilities revoked during this turn
    pub capabilities_revoked: Vec<CapId>,

    /// Currently executing entity (if any)
    current_entity: Option<Uuid>,
}

/// Specification for granting a capability during a turn
pub struct CapabilitySpec {
    /// Actor that will hold the capability
    pub holder: ActorId,
    /// Facet on the holder that receives it
    pub holder_facet: FacetId,
    /// Optional target scope for the capability
    pub target: Option<CapabilityTarget>,
    /// Semantic kind (e.g., "workspace/edit")
    pub kind: String,
    /// Attenuation caveats encoded as preserves values
    pub attenuation: Vec<preserves::IOValue>,
}

impl Activation {
    /// Create a new activation context
    pub fn new(actor_id: ActorId, current_facet: FacetId) -> Self {
        Self {
            actor_id,
            current_facet: current_facet.clone(),
            root_facet: current_facet,
            outputs: Vec::new(),
            assertions_added: Vec::new(),
            assertions_retracted: Vec::new(),
            pending_asserts: Vec::new(),
            facets_spawned: Vec::new(),
            facets_terminated: Vec::new(),
            tokens_borrowed: 0,
            tokens_repaid: 0,
            capabilities_granted: Vec::new(),
            capabilities_revoked: Vec::new(),
            current_entity: None,
        }
    }

    /// Make an assertion
    pub fn assert(&mut self, handle: Handle, value: preserves::IOValue) {
        self.assertions_added.push((handle.clone(), value.clone()));
        self.pending_asserts.push((handle.clone(), value.clone()));
        self.outputs.push(TurnOutput::Assert { handle, value });
    }

    /// Retract an assertion
    pub fn retract(&mut self, handle: Handle) {
        self.assertions_retracted.push(handle.clone());
        self.outputs.push(TurnOutput::Retract { handle });
    }

    /// Drain pending local assertions for pattern processing
    pub fn drain_pending_asserts(&mut self) -> Vec<(Handle, preserves::IOValue)> {
        self.pending_asserts.drain(..).collect()
    }

    /// Remove the most recently queued pending assertion (used for external inputs)
    pub fn pop_last_pending_assert(&mut self) {
        self.pending_asserts.pop();
    }

    pub(crate) fn set_current_entity(&mut self, entity: Option<Uuid>) {
        self.current_entity = entity;
    }

    /// Send a message to another actor/facet
    pub fn send_message(
        &mut self,
        target_actor: ActorId,
        target_facet: FacetId,
        payload: preserves::IOValue,
    ) {
        self.outputs.push(TurnOutput::Message {
            target_actor,
            target_facet,
            payload,
        });
    }

    /// Spawn a child facet
    pub fn spawn_facet(&mut self, parent: Option<FacetId>) -> FacetId {
        let facet_id = FacetId::new();
        let metadata = FacetMetadata {
            id: facet_id.clone(),
            parent: parent.clone(),
            status: FacetStatus::Alive,
            actor: self.actor_id.clone(),
        };

        self.facets_spawned.push(metadata);
        self.outputs.push(TurnOutput::FacetSpawned {
            facet: facet_id.clone(),
            parent,
        });

        facet_id
    }

    /// Terminate a facet
    pub fn terminate_facet(&mut self, facet: FacetId) {
        self.facets_terminated.push(facet.clone());
        self.outputs.push(TurnOutput::FacetTerminated { facet });
    }

    /// Borrow flow-control tokens
    pub fn borrow_tokens(&mut self, amount: i64) {
        self.tokens_borrowed += amount;
    }

    /// Repay flow-control tokens
    pub fn repay_tokens(&mut self, amount: i64) {
        self.tokens_repaid += amount;
    }

    /// Grant a new capability with a freshly generated identifier
    pub fn grant_capability(&mut self, spec: CapabilitySpec) -> CapId {
        let capability_id = Uuid::new_v4();
        self.grant_capability_with_id(capability_id, spec)
    }

    /// Grant (or re-grant) a capability using a specific identifier
    pub fn grant_capability_with_id(
        &mut self,
        capability_id: CapId,
        spec: CapabilitySpec,
    ) -> CapId {
        let issuer_entity = self.current_entity.expect(
            "Capabilities can only be granted from within an entity activation",
        );

        let CapabilitySpec {
            holder,
            holder_facet,
            target,
            kind,
            attenuation,
        } = spec;

        let metadata = CapabilityMetadata {
            id: capability_id,
            issuer: self.actor_id.clone(),
            issuer_facet: self.current_facet.clone(),
            issuer_entity: Some(issuer_entity),
            holder: holder.clone(),
            holder_facet: holder_facet.clone(),
            target: target.clone(),
            kind: kind.clone(),
            attenuation: attenuation.clone(),
            status: CapabilityStatus::Active,
        };

        if let Some(existing) = self
            .capabilities_granted
            .iter_mut()
            .find(|meta| meta.id == capability_id)
        {
            *existing = metadata.clone();
        } else {
            self.capabilities_granted.push(metadata.clone());
        }

        // Ensure the capability is not marked as revoked in this turn
        self.capabilities_revoked
            .retain(|existing| *existing != capability_id);

        self.outputs.push(TurnOutput::CapabilityGranted {
            capability: capability_id,
            issuer: metadata.issuer.clone(),
            issuer_facet: metadata.issuer_facet.clone(),
            issuer_entity: metadata.issuer_entity,
            holder: metadata.holder.clone(),
            holder_facet: metadata.holder_facet.clone(),
            target: metadata.target.clone(),
            kind: metadata.kind.clone(),
            attenuation: metadata.attenuation.clone(),
        });

        capability_id
    }

    /// Revoke a capability by identifier
    pub fn revoke_capability(&mut self, capability_id: CapId) {
        let was_new = if self
            .capabilities_revoked
            .iter()
            .any(|existing| *existing == capability_id)
        {
            false
        } else {
            self.capabilities_revoked.push(capability_id);
            true
        };

        if let Some(existing) = self
            .capabilities_granted
            .iter_mut()
            .find(|meta| meta.id == capability_id)
        {
            existing.status = CapabilityStatus::Revoked;
        }

        if was_new {
            self.outputs.push(TurnOutput::CapabilityRevoked {
                capability: capability_id,
            });
        }
    }

    /// Build the state delta from this activation
    pub fn build_delta(&self) -> StateDelta {
        let mut assertions = AssertionDelta::default();

        for (handle, value) in &self.assertions_added {
            assertions.added.push((
                self.actor_id.clone(),
                handle.clone(),
                value.clone(),
                uuid::Uuid::new_v4(), // Generate version ID
            ));
        }

        for handle in &self.assertions_retracted {
            assertions.retracted.push((
                self.actor_id.clone(),
                handle.clone(),
                uuid::Uuid::new_v4(), // Version should match the original
            ));
        }

        let facets = FacetDelta {
            spawned: self.facets_spawned.clone(),
            terminated: self.facets_terminated.clone(),
        };

        let accounts = AccountDelta {
            borrowed: self.tokens_borrowed,
            repaid: self.tokens_repaid,
        };

        let capabilities = CapabilityDelta {
            granted: self.capabilities_granted.clone(),
            revoked: self.capabilities_revoked.clone(),
        };

        StateDelta {
            assertions,
            facets,
            capabilities,
            timers: super::state::TimerDelta::default(),
            accounts,
        }
    }
}

/// Entity: behavior handler attached to a facet
///
/// Entities respond to messages, assertions, and other events.
/// They must be Send + Sync for concurrent access.
///
/// # Dataspace-First Design
///
/// Entities should express most state via assertions, capabilities, and facets
/// (the CRDT-backed dataspace). This ensures state is automatically persisted,
/// replayed during time-travel, and merged conflict-free across branches.
///
/// Private state should be rare. If needed, implement HydratableEntity.
pub trait Entity: Send + Sync + Any {
    /// Handle an incoming message
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()>;

    /// Handle a new assertion (pattern match)
    fn on_assert(
        &self,
        _activation: &mut Activation,
        _handle: &Handle,
        _value: &preserves::IOValue,
    ) -> ActorResult<()> {
        Ok(())
    }

    /// Handle a retraction
    fn on_retract(&self, _activation: &mut Activation, _handle: &Handle) -> ActorResult<()> {
        Ok(())
    }

    /// Handle facet stop
    fn on_stop(&self, _activation: &mut Activation) -> ActorResult<()> {
        Ok(())
    }

    /// Handle capability invocation (default: unsupported)
    fn on_capability_invoke(
        &self,
        _activation: &mut Activation,
        _capability: &CapabilityMetadata,
        _payload: &preserves::IOValue,
    ) -> ActorResult<preserves::IOValue> {
        Err(ActorError::InvalidActivation(
            "capability invocation not supported by this entity".into(),
        ))
    }
}

/// Optional trait for entities with private state that can't live in the dataspace
///
/// # When to Use
///
/// Most entities should NOT implement this trait. Use it only when:
/// - State is truly ephemeral/derived and can't be modeled as assertions
/// - Performance requires caching that's expensive to rebuild from dataspace
/// - The state is inherently local and non-collaborative
///
/// # Merge Behavior
///
/// If two branches have different private state for the same entity, a merge
/// warning is generated. One state wins arbitrarily—this is unavoidable for
/// non-CRDT state. Prefer dataspace-backed state for conflict-free merges.
///
/// # Example
///
/// ```ignore
/// struct CachedEntity {
///     cache: HashMap<String, Value>,
/// }
///
/// impl HydratableEntity for CachedEntity {
///     fn snapshot_state(&self) -> preserves::IOValue {
///         // Serialize cache to preserves
///         preserves::IOValue::new(self.cache.len())
///     }
///
///     fn restore_state(&mut self, state: &preserves::IOValue) -> ActorResult<()> {
///         // Restore cache from preserves
///         self.cache.clear();
///         Ok(())
///     }
/// }
/// ```
pub trait HydratableEntity: Entity {
    /// Capture private state for snapshotting
    ///
    /// Returns a preserves value representing this entity's private state.
    /// Called during snapshot creation.
    fn snapshot_state(&self) -> preserves::IOValue;

    /// Restore private state from a snapshot
    ///
    /// Called during replay/time-travel to restore entity to a previous state.
    /// Returns an error if restoration fails (e.g., invalid state format).
    fn restore_state(&mut self, state: &preserves::IOValue) -> ActorResult<()>;
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestEntity;

    impl Entity for TestEntity {
        fn on_message(
            &self,
            activation: &mut Activation,
            payload: &preserves::IOValue,
        ) -> ActorResult<()> {
            // Echo the message back
            activation.send_message(
                activation.actor_id.clone(),
                activation.current_facet.clone(),
                payload.clone(),
            );
            Ok(())
        }
    }

    #[test]
    fn test_actor_creation() {
        let actor = Actor::new(ActorId::new());
        assert!(actor.facets.read().facets.contains_key(&actor.root_facet));
    }

    #[test]
    fn test_facet_spawn() {
        let actor = Actor::new(ActorId::new());
        let root = actor.root_facet.clone();
        let child = actor.spawn_facet(&root);

        let facets = actor.facets.read();
        assert!(facets.facets.contains_key(&child));
        assert_eq!(facets.facets.get(&child).unwrap().parent, Some(root));
    }

    #[test]
    fn test_facet_terminate() {
        let actor = Actor::new(ActorId::new());
        let root = actor.root_facet.clone();
        let child = actor.spawn_facet(&root);

        actor.terminate_facet(&child);

        let facets = actor.facets.read();
        assert_eq!(
            facets.facets.get(&child).unwrap().status,
            FacetStatus::Terminated
        );
    }

    #[test]
    fn test_activation_assert_retract() {
        let actor_id = ActorId::new();
        let facet_id = FacetId::new();
        let mut activation = Activation::new(actor_id, facet_id);

        let handle = Handle::new();
        let value = preserves::IOValue::symbol("test-data");

        activation.assert(handle.clone(), value);
        assert_eq!(activation.assertions_added.len(), 1);
        assert_eq!(activation.outputs.len(), 1);

        activation.retract(handle);
        assert_eq!(activation.assertions_retracted.len(), 1);
        assert_eq!(activation.outputs.len(), 2);
    }

    #[test]
    fn test_entity_message() {
        let actor = Actor::new(ActorId::new());
        let facet = actor.root_facet.clone();

        actor.attach_entity(
            uuid::Uuid::new_v4(),
            "test-entity".to_string(),
            facet.clone(),
            Box::new(TestEntity),
        );

        let input = TurnInput::ExternalMessage {
            actor: actor.id.clone(),
            facet,
            payload: preserves::IOValue::symbol("test-message"),
        };

        let result = actor.execute_turn(vec![input]);
        assert!(result.is_ok());

        let (outputs, _delta) = result.unwrap();
        assert_eq!(outputs.len(), 1); // Should have echoed the message
    }

    #[test]
    fn test_pattern_integration() {
        use crate::runtime::pattern::Pattern;
        use std::sync::Arc;
        use std::sync::atomic::{AtomicUsize, Ordering};

        // Entity that counts pattern matches
        struct PatternEntity {
            assert_count: Arc<AtomicUsize>,
            retract_count: Arc<AtomicUsize>,
        }

        impl Entity for PatternEntity {
            fn on_message(
                &self,
                _activation: &mut Activation,
                _payload: &preserves::IOValue,
            ) -> ActorResult<()> {
                Ok(())
            }

            fn on_assert(
                &self,
                _activation: &mut Activation,
                _handle: &Handle,
                _value: &preserves::IOValue,
            ) -> ActorResult<()> {
                self.assert_count.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }

            fn on_retract(
                &self,
                _activation: &mut Activation,
                _handle: &Handle,
            ) -> ActorResult<()> {
                self.retract_count.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
        }

        let actor = Actor::new(ActorId::new());
        let facet = actor.root_facet.clone();

        // Register a wildcard pattern
        let pattern = Pattern {
            id: uuid::Uuid::new_v4(),
            pattern: preserves::IOValue::symbol("<_>"),
            facet: facet.clone(),
        };

        actor.register_pattern(pattern);

        // Attach entity
        let assert_count = Arc::new(AtomicUsize::new(0));
        let retract_count = Arc::new(AtomicUsize::new(0));

        actor.attach_entity(
            uuid::Uuid::new_v4(),
            "pattern-entity".to_string(),
            facet.clone(),
            Box::new(PatternEntity {
                assert_count: assert_count.clone(),
                retract_count: retract_count.clone(),
            }),
        );

        // Make an assertion
        let handle = Handle::new();
        let value = preserves::IOValue::symbol("test-value");

        let inputs = vec![TurnInput::Assert {
            actor: actor.id.clone(),
            handle: handle.clone(),
            value,
        }];

        let (outputs, _) = actor.execute_turn(inputs).unwrap();

        // Should have triggered on_assert callback
        assert_eq!(assert_count.load(Ordering::SeqCst), 1);

        // Should have PatternMatched in outputs
        assert!(
            outputs
                .iter()
                .any(|o| matches!(o, TurnOutput::PatternMatched { .. }))
        );

        // Now retract
        let inputs = vec![TurnInput::Retract {
            actor: actor.id.clone(),
            handle,
        }];

        let (outputs, _) = actor.execute_turn(inputs).unwrap();

        // Should have triggered on_retract callback
        assert_eq!(retract_count.load(Ordering::SeqCst), 1);

        // Should have PatternUnmatched in outputs
        assert!(
            outputs
                .iter()
                .any(|o| matches!(o, TurnOutput::PatternUnmatched { .. }))
        );
    }

    #[test]
    fn test_local_assert_triggers_pattern() {
        use crate::runtime::pattern::Pattern;

        struct SelfAssertEntity;

        impl Entity for SelfAssertEntity {
            fn on_message(
                &self,
                activation: &mut Activation,
                _payload: &preserves::IOValue,
            ) -> ActorResult<()> {
                let handle = Handle::new();
                let value = preserves::IOValue::symbol("local-value");
                activation.assert(handle, value);
                Ok(())
            }
        }

        let actor = Actor::new(ActorId::new());
        let facet = actor.root_facet.clone();

        let pattern = Pattern {
            id: uuid::Uuid::new_v4(),
            pattern: preserves::IOValue::symbol("local-value"),
            facet: facet.clone(),
        };

        actor.register_pattern(pattern);

        actor.attach_entity(
            uuid::Uuid::new_v4(),
            "self-assert".to_string(),
            facet.clone(),
            Box::new(SelfAssertEntity),
        );

        let input = TurnInput::ExternalMessage {
            actor: actor.id.clone(),
            facet,
            payload: preserves::IOValue::symbol("trigger"),
        };

        let (outputs, _) = actor.execute_turn(vec![input]).unwrap();

        assert!(
            outputs
                .iter()
                .any(|o| matches!(o, TurnOutput::PatternMatched { .. })),
            "local assertions should trigger pattern matches"
        );
    }

    #[test]
    fn test_grant_capability_emits_output_with_metadata() {
        let actor_id = ActorId::new();
        let actor = Actor::new(actor_id.clone());
        let mut activation = Activation::new(actor_id.clone(), actor.root_facet.clone());
        let entity_id = Uuid::new_v4();
        activation.set_current_entity(Some(entity_id));

        let spec = CapabilitySpec {
            holder: actor_id.clone(),
            holder_facet: actor.root_facet.clone(),
            target: Some(CapabilityTarget {
                actor: actor_id.clone(),
                facet: Some(actor.root_facet.clone()),
            }),
            kind: "test/grant".into(),
            attenuation: vec![preserves::IOValue::symbol("allow")],
        };

        let cap_id = activation.grant_capability(spec);

        assert_eq!(activation.capabilities_granted.len(), 1);
        match activation.outputs.last() {
            Some(TurnOutput::CapabilityGranted {
                capability,
                issuer,
                holder,
                kind,
                attenuation,
                ..
            }) => {
                assert_eq!(*capability, cap_id);
                assert_eq!(*issuer, actor_id);
                assert_eq!(*holder, actor_id);
                assert_eq!(kind, "test/grant");
                assert_eq!(attenuation, &vec![preserves::IOValue::symbol("allow")]);
            }
            other => panic!("unexpected output: {other:?}"),
        }
    }

    #[test]
    fn test_revoke_capability_emits_event() {
        let actor_id = ActorId::new();
        let actor = Actor::new(actor_id.clone());
        let mut activation = Activation::new(actor_id.clone(), actor.root_facet.clone());
        let entity_id = Uuid::new_v4();
        activation.set_current_entity(Some(entity_id));

        let cap_id = activation.grant_capability(CapabilitySpec {
            holder: actor_id.clone(),
            holder_facet: actor.root_facet.clone(),
            target: None,
            kind: "test/grant".into(),
            attenuation: Vec::new(),
        });

        activation.revoke_capability(cap_id);

        assert!(
            activation
                .outputs
                .iter()
                .any(|output| matches!(output, TurnOutput::CapabilityRevoked { capability } if *capability == cap_id)),
            "revoking should emit a capability revoked output"
        );
    }
}
