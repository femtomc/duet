//! Actors, facets, activation contexts, and entities
//!
//! Implements the Syndicated Actor model abstractions:
//! - Actors: isolated units of computation
//! - Facets: hierarchical scopes within actors
//! - Entities: behavior handlers attached to facets
//! - Activation: execution context for a turn

use parking_lot::RwLock;
use std::collections::HashMap;
use std::sync::Arc;

use super::error::ActorResult;
use super::pattern::{Pattern, PatternEngine};
use super::state::{
    AccountDelta, AssertionDelta, AssertionSet, CapabilityDelta, CapabilityMap, FacetDelta,
    FacetMap, FacetMetadata, FacetStatus, PNCounter, StateDelta,
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

    /// Entities attached to facets
    pub entities: Arc<RwLock<HashMap<FacetId, Vec<Box<dyn Entity>>>>>,

    /// Pattern engine for subscriptions
    pub pattern_engine: Arc<RwLock<PatternEngine>>,
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
                    for entity in entity_list {
                        entity.on_message(activation, &payload)?;
                    }
                }
            }

            TurnInput::Assert { handle, value, .. } => {
                // Evaluate pattern engine
                let mut engine = self.pattern_engine.write();
                let pattern_matches = engine.eval_assert(&handle, &value);
                drop(engine);

                // Generate PatternMatched outputs
                for pattern_match in &pattern_matches {
                    activation.outputs.push(TurnOutput::PatternMatched {
                        pattern_id: pattern_match.pattern_id,
                        handle: pattern_match.handle.clone(),
                    });
                }

                // Call entity on_assert callbacks for matched patterns
                let entities = self.entities.read();
                for pattern_match in pattern_matches {
                    // Find entities subscribed to this pattern (facet-based lookup)
                    // For now, we'll need to look up the facet from the pattern
                    let engine = self.pattern_engine.read();
                    if let Some(pattern) = engine.patterns.get(&pattern_match.pattern_id) {
                        if let Some(entity_list) = entities.get(&pattern.facet) {
                            for entity in entity_list {
                                entity.on_assert(activation, &handle, &value)?;
                            }
                        }
                    }
                }
                drop(entities);

                // Record the assertion
                activation.assert(handle, value);
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
                            for entity in entity_list {
                                entity.on_retract(activation, &handle)?;
                            }
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

            _ => {
                // Handle other input types
            }
        }

        Ok(())
    }

    /// Attach an entity to a facet
    pub fn attach_entity(&self, facet: FacetId, entity: Box<dyn Entity>) {
        let mut entities = self.entities.write();
        entities.entry(facet).or_insert_with(Vec::new).push(entity);
    }

    /// Remove all entities from a facet
    ///
    /// Returns the number of entities removed.
    pub fn remove_entities_from_facet(&self, facet: &FacetId) -> usize {
        let mut entities = self.entities.write();
        if let Some(entity_list) = entities.remove(facet) {
            entity_list.len()
        } else {
            0
        }
    }

    /// Register a pattern subscription
    pub fn register_pattern(&self, pattern: Pattern) -> uuid::Uuid {
        let mut engine = self.pattern_engine.write();
        engine.register(pattern)
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

    /// Outputs collected during this turn
    pub outputs: Vec<TurnOutput>,

    /// Assertions made
    pub assertions_added: Vec<(Handle, preserves::IOValue)>,

    /// Assertions retracted
    pub assertions_retracted: Vec<Handle>,

    /// Facets spawned
    pub facets_spawned: Vec<FacetMetadata>,

    /// Facets terminated
    pub facets_terminated: Vec<FacetId>,

    /// Flow-control: tokens borrowed
    pub tokens_borrowed: i64,

    /// Flow-control: tokens repaid
    pub tokens_repaid: i64,
}

impl Activation {
    /// Create a new activation context
    pub fn new(actor_id: ActorId, current_facet: FacetId) -> Self {
        Self {
            actor_id,
            current_facet,
            outputs: Vec::new(),
            assertions_added: Vec::new(),
            assertions_retracted: Vec::new(),
            facets_spawned: Vec::new(),
            facets_terminated: Vec::new(),
            tokens_borrowed: 0,
            tokens_repaid: 0,
        }
    }

    /// Make an assertion
    pub fn assert(&mut self, handle: Handle, value: preserves::IOValue) {
        self.assertions_added.push((handle.clone(), value.clone()));
        self.outputs.push(TurnOutput::Assert { handle, value });
    }

    /// Retract an assertion
    pub fn retract(&mut self, handle: Handle) {
        self.assertions_retracted.push(handle.clone());
        self.outputs.push(TurnOutput::Retract { handle });
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

        StateDelta {
            assertions,
            facets,
            capabilities: CapabilityDelta::default(),
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
pub trait Entity: Send + Sync {
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

        actor.attach_entity(facet.clone(), Box::new(TestEntity));

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
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;

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
        assert!(outputs
            .iter()
            .any(|o| matches!(o, TurnOutput::PatternMatched { .. })));

        // Now retract
        let inputs = vec![TurnInput::Retract {
            actor: actor.id.clone(),
            handle,
        }];

        let (outputs, _) = actor.execute_turn(inputs).unwrap();

        // Should have triggered on_retract callback
        assert_eq!(retract_count.load(Ordering::SeqCst), 1);

        // Should have PatternUnmatched in outputs
        assert!(outputs
            .iter()
            .any(|o| matches!(o, TurnOutput::PatternUnmatched { .. })));
    }
}
