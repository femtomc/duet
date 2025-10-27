//! Actors, facets, activation contexts, and entities
//!
//! Implements the Syndicated Actor model abstractions:
//! - Actors: isolated units of computation
//! - Facets: hierarchical scopes within actors
//! - Entities: behavior handlers attached to facets
//! - Activation: execution context for a turn

use std::collections::HashMap;
use std::sync::Arc;
use parking_lot::RwLock;

use super::state::{
    AssertionSet, CapabilityMap, FacetMap, PNCounter, StateDelta,
    AssertionDelta, FacetDelta, CapabilityDelta, AccountDelta,
    FacetMetadata, FacetStatus,
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
    ) -> anyhow::Result<(Vec<TurnOutput>, StateDelta)> {
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
    fn process_input(
        &self,
        activation: &mut Activation,
        input: TurnInput,
    ) -> anyhow::Result<()> {
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
                activation.assert(handle, value);
            }

            TurnInput::Retract { handle, .. } => {
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
    pub assertions_added: Vec<(Handle, preserves::value::IOValue)>,

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
    pub fn assert(&mut self, handle: Handle, value: preserves::value::IOValue) {
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
        payload: preserves::value::IOValue,
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
pub trait Entity: Send + Sync {
    /// Handle an incoming message
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::value::IOValue,
    ) -> anyhow::Result<()>;

    /// Handle a new assertion (pattern match)
    fn on_assert(
        &self,
        _activation: &mut Activation,
        _handle: &Handle,
        _value: &preserves::value::IOValue,
    ) -> anyhow::Result<()> {
        Ok(())
    }

    /// Handle a retraction
    fn on_retract(
        &self,
        _activation: &mut Activation,
        _handle: &Handle,
    ) -> anyhow::Result<()> {
        Ok(())
    }

    /// Handle facet stop
    fn on_stop(&self, _activation: &mut Activation) -> anyhow::Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestEntity;

    impl Entity for TestEntity {
        fn on_message(
            &self,
            activation: &mut Activation,
            payload: &preserves::value::IOValue,
        ) -> anyhow::Result<()> {
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
        let value = preserves::value::Value::symbol("test-data").wrap();

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
            payload: preserves::value::Value::symbol("test-message").wrap(),
        };

        let result = actor.execute_turn(vec![input]);
        assert!(result.is_ok());

        let (outputs, _delta) = result.unwrap();
        assert_eq!(outputs.len(), 1); // Should have echoed the message
    }
}
