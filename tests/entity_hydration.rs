//! Integration tests for entity registration, persistence, and hydration
//!
//! Tests the complete flow of entity lifecycle across time-travel and restarts.

use duet::runtime::actor::{Activation, Entity, HydratableEntity};
use duet::runtime::error::{ActorError, ActorResult};
use duet::runtime::pattern::Pattern;
use duet::runtime::registry::{EntityMetadata, EntityRegistry};
use duet::runtime::turn::{ActorId, FacetId};
use duet::runtime::{Control, RuntimeConfig};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use tempfile::TempDir;
use uuid::Uuid;

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
        let id = control
            .register_entity(actor_id, facet_id, "counter".to_string(), entity_config)
            .unwrap();

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
    let entity_id = control
        .register_entity(
            actor_id.clone(),
            facet_id.clone(),
            "test-entity".to_string(),
            preserves::IOValue::symbol("config"),
        )
        .unwrap();

    // Create some turn history
    let mut turn_ids = Vec::new();
    for i in 0..5 {
        let turn_id = control
            .send_message(
                actor_id.clone(),
                facet_id.clone(),
                preserves::IOValue::new(i),
            )
            .unwrap();
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

#[test]
fn test_entity_patterns_persist_through_restart_and_time_travel() {
    EntityRegistry::global().register("pattern-entity", |_config| {
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

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    // Register entity and pattern
    let entity_id;
    let mut turn_ids = Vec::new();
    {
        let mut control = Control::init(config.clone()).unwrap();

        entity_id = control
            .register_entity(
                actor_id.clone(),
                facet_id.clone(),
                "pattern-entity".to_string(),
                preserves::IOValue::symbol("config"),
            )
            .unwrap();

        let pattern = Pattern {
            id: Uuid::new_v4(),
            pattern: preserves::IOValue::symbol("<_>"),
            facet: facet_id.clone(),
        };

        control
            .register_pattern_for_entity(entity_id, pattern)
            .unwrap();

        // Drive a few turns so history exists
        for i in 0..3 {
            let turn_id = control
                .send_message(
                    actor_id.clone(),
                    facet_id.clone(),
                    preserves::IOValue::new(i),
                )
                .unwrap();
            turn_ids.push(turn_id);
        }

        let entities = control.list_entities();
        assert_eq!(entities.len(), 1);
        assert_eq!(entities[0].pattern_count, 1);

        // Verify metadata on disk captures the pattern definition
        let meta_path = temp.path().join("meta").join("entities.json");
        let contents = std::fs::read_to_string(&meta_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&contents).unwrap();
        let pattern_len = parsed
            .as_object()
            .and_then(|map| map.values().next())
            .and_then(|meta| meta.get("patterns"))
            .and_then(|patterns| patterns.as_array())
            .map(|arr| arr.len())
            .unwrap_or_default();
        assert_eq!(pattern_len, 1);

        // Ensure metadata can be deserialized back into EntityMetadata
        let deserialized: std::collections::HashMap<Uuid, EntityMetadata> =
            serde_json::from_str(&contents).unwrap();
        let meta = deserialized.values().next().unwrap();
        assert_eq!(meta.patterns.len(), 1);
    }

    // Restart runtime and ensure pattern metadata is intact
    {
        let control = Control::new(config.clone()).unwrap();
        let entities = control.list_entities();
        assert_eq!(entities.len(), 1);
        assert_eq!(entities[0].pattern_count, 1);
    }

    // Time travel backwards and verify pattern remains registered
    {
        let mut control = Control::new(config).unwrap();
        control.goto(turn_ids[1].clone()).unwrap();

        let entities = control.list_entities();
        assert_eq!(entities.len(), 1);
        assert_eq!(entities[0].pattern_count, 1);
    }
}

#[test]
fn test_hydratable_entity_state_restored_on_goto() {
    struct HydratedCounter {
        value: Mutex<i64>,
        restored: Arc<AtomicI64>,
    }

    impl Entity for HydratedCounter {
        fn on_message(
            &self,
            _activation: &mut Activation,
            payload: &preserves::IOValue,
        ) -> ActorResult<()> {
            let value = payload
                .as_string()
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            *self.value.lock().unwrap() = value;
            Ok(())
        }
    }

    impl HydratableEntity for HydratedCounter {
        fn snapshot_state(&self) -> preserves::IOValue {
            let value = *self.value.lock().unwrap();
            preserves::IOValue::new(value.to_string())
        }

        fn restore_state(&mut self, state: &preserves::IOValue) -> ActorResult<()> {
            let text = match state.as_string() {
                Some(s) => s,
                None => {
                    return Err(ActorError::InvalidActivation(
                        "expected string for hydratable state".to_string(),
                    ));
                }
            };

            let value: i64 = match text.parse() {
                Ok(v) => v,
                Err(e) => {
                    return Err(ActorError::InvalidActivation(format!(
                        "invalid integer: {e}"
                    )));
                }
            };

            *self.value.lock().unwrap() = value;
            self.restored.store(value, Ordering::SeqCst);
            Ok(())
        }
    }

    let restored = Arc::new(AtomicI64::new(0));

    EntityRegistry::global().register_hydratable("hydrated-counter", {
        let restored = restored.clone();
        move |_config| {
            Ok(HydratedCounter {
                value: Mutex::new(0),
                restored: restored.clone(),
            })
        }
    });

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 1,
        flow_control_limit: 100,
        debug: false,
    };

    let mut control = Control::init(config).unwrap();

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    control
        .register_entity(
            actor_id.clone(),
            facet_id.clone(),
            "hydrated-counter".to_string(),
            preserves::IOValue::symbol("cfg"),
        )
        .unwrap();

    let turn_id = control
        .send_message(
            actor_id.clone(),
            facet_id.clone(),
            preserves::IOValue::new("42".to_string()),
        )
        .unwrap();

    restored.store(-1, Ordering::SeqCst);

    control.goto(turn_id).unwrap();

    assert_eq!(restored.load(Ordering::SeqCst), 42);
}
