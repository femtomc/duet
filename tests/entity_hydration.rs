//! Integration tests for entity registration, persistence, and hydration
//!
//! Tests the complete flow of entity lifecycle across time-travel and restarts.

use duet::runtime::{Control, RuntimeConfig};
use duet::runtime::registry::EntityRegistry;
use duet::runtime::turn::{ActorId, FacetId};
use duet::runtime::actor::{Activation, Entity};
use duet::runtime::error::ActorResult;
use tempfile::TempDir;

/// Simple test entity that counts messages
struct CounterEntity {
    count: std::sync::Arc<std::sync::atomic::AtomicUsize>,
}

impl Entity for CounterEntity {
    fn on_message(
        &self,
        _activation: &mut Activation,
        _payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        self.count.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Ok(())
    }
}

#[test]
fn test_entity_registration_persists_across_restart() {
    // Register the entity type in the global registry
    EntityRegistry::global().register("counter", |_config| {
        Ok(Box::new(CounterEntity {
            count: std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        }))
    });

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 100,
        debug: false,
    };

    let entity_id = {
        let mut control = Control::init(config.clone()).unwrap();

        let actor_id = ActorId::new();
        let facet_id = FacetId::new();
        let entity_config = preserves::IOValue::symbol("test-config");

        // Register entity
        let id = control.register_entity(
            actor_id,
            facet_id,
            "counter".to_string(),
            entity_config,
        ).unwrap();

        // Verify it's registered
        let entities = control.list_entities();
        assert_eq!(entities.len(), 1);
        assert_eq!(entities[0].entity_type, "counter");

        id
    };

    // Restart the runtime (simulates process restart)
    {
        let control = Control::new(config).unwrap();

        // Verify entity metadata was persisted and loaded
        let entities = control.list_entities();
        assert_eq!(entities.len(), 1);
        assert_eq!(entities[0].id, entity_id);
        assert_eq!(entities[0].entity_type, "counter");
    }
}

#[test]
fn test_entity_survives_time_travel() {
    EntityRegistry::global().register("test-entity", |_config| {
        Ok(Box::new(CounterEntity {
            count: std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        }))
    });

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 100,
        debug: false,
    };

    let mut control = Control::init(config).unwrap();

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    // Register entity
    let entity_id = control.register_entity(
        actor_id.clone(),
        facet_id.clone(),
        "test-entity".to_string(),
        preserves::IOValue::symbol("config"),
    ).unwrap();

    // Create some turn history
    let mut turn_ids = Vec::new();
    for i in 0..5 {
        let turn_id = control.send_message(
            actor_id.clone(),
            facet_id.clone(),
            preserves::IOValue::new(i),
        ).unwrap();
        turn_ids.push(turn_id);
    }

    // Entity should still be registered
    assert_eq!(control.list_entities().len(), 1);

    // Go back in time
    control.goto(turn_ids[2].clone()).unwrap();

    // Entity metadata should still be present
    // (metadata persists independently of turn execution)
    let entities = control.list_entities();
    assert_eq!(entities.len(), 1);
    assert_eq!(entities[0].id, entity_id);
}
