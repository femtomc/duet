//! Integration tests for entity registration, persistence, and hydration
//!
//! Tests the complete flow of entity lifecycle across time-travel and restarts.

use duet::runtime::actor::{Activation, CapabilitySpec, Entity, HydratableEntity};
use duet::runtime::error::{ActorError, ActorResult, RuntimeError};
use duet::runtime::pattern::Pattern;
use duet::runtime::registry::{EntityMetadata, EntityRegistry};
use duet::runtime::state::CapabilityTarget;
use duet::runtime::turn::{ActorId, FacetId, Handle};
use duet::runtime::{Control, RuntimeConfig};
use once_cell::sync::Lazy;
use std::convert::TryFrom;
use std::fs;
use std::sync::atomic::{AtomicI64, AtomicUsize, Ordering};
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

/// Entity that grants a deterministic capability when it receives the "grant" symbol.
struct CapabilityIssuerEntity;

impl Entity for CapabilityIssuerEntity {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        if let Some(symbol) = payload.as_symbol() {
            if symbol.as_ref() == "grant" {
                let spec = CapabilitySpec {
                    holder: activation.actor_id.clone(),
                    holder_facet: activation.current_facet.clone(),
                    target: Some(CapabilityTarget {
                        actor: activation.actor_id.clone(),
                        facet: Some(activation.current_facet.clone()),
                    }),
                    kind: "test/grant".to_string(),
                    attenuation: Vec::new(),
                };
                activation.grant_capability(spec);
            }
        }

        Ok(())
    }
}

/// Entity that exercises the flow-control borrow/repay helpers based on integer payloads.
struct FlowControlEntity;

impl Entity for FlowControlEntity {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        if let Some(amount) = payload
            .as_signed_integer()
            .and_then(|value| i64::try_from(value.as_ref()).ok())
        {
            if amount > 0 {
                activation.borrow_tokens(amount);
            } else if amount < 0 {
                activation.repay_tokens(-amount);
            }
        }

        Ok(())
    }
}

const PRODUCER_HANDLE_UUID: Uuid = Uuid::from_u128(0xfeedfacefeedcafe1234567890abcdef);

static PATTERN_ASSERT_COUNT: Lazy<Arc<AtomicUsize>> = Lazy::new(|| Arc::new(AtomicUsize::new(0)));

static REGISTER_PATTERN_FIXTURE: Lazy<()> = Lazy::new(|| {
    EntityRegistry::global().register("pattern-producer", |_config| Ok(Box::new(PatternProducer)));

    EntityRegistry::global().register("pattern-watcher", |_config| {
        Ok(Box::new(PatternWatcher {
            on_assert: Arc::clone(&PATTERN_ASSERT_COUNT),
        }))
    });
});

struct PatternProducer;

impl Entity for PatternProducer {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        if let Some(symbol) = payload.as_symbol() {
            match symbol.as_ref() {
                "assert" => {
                    let handle = Handle(PRODUCER_HANDLE_UUID);
                    activation.assert(handle, preserves::IOValue::symbol("note"));
                }
                "retract" => {
                    let handle = Handle(PRODUCER_HANDLE_UUID);
                    activation.retract(handle);
                }
                _ => {}
            }
        }
        Ok(())
    }
}

struct PatternWatcher {
    on_assert: Arc<AtomicUsize>,
}

impl Entity for PatternWatcher {
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
        self.on_assert.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }

    fn on_retract(&self, _activation: &mut Activation, _handle: &Handle) -> ActorResult<()> {
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
fn test_workspace_capability_read_and_write() {
    use preserves::IOValue;

    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path();
    fs::write(workspace_root.join("hello.txt"), "hello world").unwrap();

    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
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
            "workspace".to_string(),
            IOValue::new(workspace_root.to_string_lossy().to_string()),
        )
        .unwrap();

    // Create an initial catalog snapshot
    control
        .send_message(
            actor_id.clone(),
            facet_id.clone(),
            IOValue::symbol("workspace-rescan"),
        )
        .unwrap();

    // Request read capability for hello.txt
    let read_request = IOValue::record(
        IOValue::symbol("workspace-read"),
        vec![IOValue::new("hello.txt".to_string())],
    );
    control
        .send_message(actor_id.clone(), facet_id.clone(), read_request.clone())
        .unwrap();

    let read_cap = control
        .list_capabilities()
        .into_iter()
        .find(|cap| cap.kind == "workspace/read")
        .expect("read capability should be granted");

    let read_result = control
        .invoke_capability(read_cap.id, read_request.clone())
        .unwrap();

    let text = read_result
        .as_string()
        .expect("expected string result from read");
    assert_eq!(text.as_ref(), "hello world");

    // Request write capability and overwrite the file
    let write_grant = IOValue::record(
        IOValue::symbol("workspace-write"),
        vec![IOValue::new("hello.txt".to_string())],
    );
    control
        .send_message(actor_id.clone(), facet_id.clone(), write_grant)
        .unwrap();

    let write_cap = control
        .list_capabilities()
        .into_iter()
        .find(|cap| cap.kind == "workspace/write")
        .expect("write capability should be granted");

    let write_payload = IOValue::record(
        IOValue::symbol("workspace-write"),
        vec![
            IOValue::new("hello.txt".to_string()),
            IOValue::new("updated".to_string()),
        ],
    );

    let write_result = control
        .invoke_capability(write_cap.id, write_payload)
        .unwrap();

    assert_eq!(
        write_result
            .as_symbol()
            .expect("write returns symbol")
            .as_ref(),
        "ok"
    );

    let file_contents = fs::read_to_string(workspace_root.join("hello.txt")).unwrap();
    assert_eq!(file_contents, "updated");

    // Read again via capability to confirm the catalog sees the change
    let read_result_again = control
        .invoke_capability(read_cap.id, read_request)
        .unwrap();

    let text_again = read_result_again
        .as_string()
        .expect("expected string result");
    assert_eq!(text_again.as_ref(), "updated");
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
fn test_pattern_retractions_survive_restart() {
    Lazy::force(&REGISTER_PATTERN_FIXTURE);
    PATTERN_ASSERT_COUNT.as_ref().store(0, Ordering::SeqCst);

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 100,
        debug: false,
    };

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();
    let pattern_id = Uuid::new_v4();

    let turn_after_assert = {
        let mut control = Control::init(config.clone()).unwrap();
        let _producer_id = control
            .register_entity(
                actor_id.clone(),
                facet_id.clone(),
                "pattern-producer".to_string(),
                preserves::IOValue::symbol("config"),
            )
            .unwrap();
        let watcher_id = control
            .register_entity(
                actor_id.clone(),
                facet_id.clone(),
                "pattern-watcher".to_string(),
                preserves::IOValue::symbol("config"),
            )
            .unwrap();

        let pattern = Pattern {
            id: pattern_id,
            pattern: preserves::IOValue::symbol("<_>"),
            facet: facet_id.clone(),
        };
        control
            .register_pattern_for_entity(watcher_id, pattern)
            .unwrap();

        let turn = control
            .send_message(
                actor_id.clone(),
                facet_id.clone(),
                preserves::IOValue::symbol("assert"),
            )
            .unwrap();

        assert_eq!(
            PATTERN_ASSERT_COUNT.as_ref().load(Ordering::SeqCst),
            1,
            "watcher should observe initial assertion"
        );

        let matches = control
            .runtime()
            .pattern_matches(&actor_id, &pattern_id)
            .expect("actor should exist");
        assert_eq!(
            matches.len(),
            1,
            "pattern engine retains match before restart"
        );
        turn
    };

    let mut control = Control::new(config).unwrap();

    // Ensure restart did not spuriously deliver extra matches
    assert_eq!(
        PATTERN_ASSERT_COUNT.as_ref().load(Ordering::SeqCst),
        1,
        "restart should not re-trigger assertion callbacks"
    );

    // Rehydrate state to the turn where the assertion was active
    control.goto(turn_after_assert.clone()).unwrap();

    let matches = control
        .runtime()
        .pattern_matches(&actor_id, &pattern_id)
        .expect("actor should exist after restart");
    assert_eq!(
        matches.len(),
        1,
        "pattern engine should seed match data during hydration"
    );
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

#[test]
fn test_capabilities_persist_across_time_travel_and_restart() {
    EntityRegistry::global().register("cap-issuer", |_config| Ok(Box::new(CapabilityIssuerEntity)));

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    let grant_turn = {
        let mut control = Control::init(config.clone()).unwrap();

        control
            .register_entity(
                actor_id.clone(),
                facet_id.clone(),
                "cap-issuer".to_string(),
                preserves::IOValue::symbol("cfg"),
            )
            .unwrap();

        // Produce an initial turn so that time-travel has a baseline state
        control
            .send_message(
                actor_id.clone(),
                facet_id.clone(),
                preserves::IOValue::symbol("noop"),
            )
            .unwrap();

        // Issue the capability
        let grant_turn = control
            .send_message(
                actor_id.clone(),
                facet_id.clone(),
                preserves::IOValue::symbol("grant"),
            )
            .unwrap();

        let capabilities = control.list_capabilities();
        assert_eq!(capabilities.len(), 1);
        let cap = &capabilities[0];
        assert_eq!(cap.issuer, actor_id);
        assert_eq!(cap.holder, actor_id);
        assert_eq!(cap.kind, "test/grant");

        // Rewind to before the capability was issued
        control.back(1).unwrap();
        assert!(control.list_capabilities().is_empty());

        // Jump forward again and ensure the capability reappears
        control.goto(grant_turn.clone()).unwrap();
        assert_eq!(control.list_capabilities().len(), 1);

        grant_turn
    };

    // Restart the runtime and ensure capability metadata is restored
    {
        let mut control = Control::new(config.clone()).unwrap();
        control.goto(grant_turn.clone()).unwrap();
        let caps = control.list_capabilities();
        assert_eq!(caps.len(), 1);
        let cap = &caps[0];
        assert_eq!(cap.kind, "test/grant");
        assert_eq!(cap.issuer, actor_id);
        assert_eq!(cap.holder, actor_id);
        assert_eq!(cap.target.as_ref().unwrap().actor, actor_id);
    }

    // Time travel backwards after restart clears the capability state
    {
        let mut control = Control::new(config).unwrap();
        control.back(1).unwrap();
        assert!(control.list_capabilities().is_empty());
    }
}

#[test]
fn test_flow_control_account_balance_and_time_travel() {
    EntityRegistry::global().register("flow-control", |_config| Ok(Box::new(FlowControlEntity)));

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 5,
        debug: false,
    };

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    {
        let mut control = Control::init(config.clone()).unwrap();

        control
            .register_entity(
                actor_id.clone(),
                facet_id.clone(),
                "flow-control".to_string(),
                preserves::IOValue::symbol("cfg"),
            )
            .unwrap();

        // Produce an initial turn to ensure rewind has a stable checkpoint
        control
            .send_message(
                actor_id.clone(),
                facet_id.clone(),
                preserves::IOValue::new(preserves::SignedInteger::from(0)),
            )
            .unwrap();

        // Borrow more than the flow-control limit
        control
            .send_message(
                actor_id.clone(),
                facet_id.clone(),
                preserves::IOValue::new(preserves::SignedInteger::from(6)),
            )
            .unwrap();

        assert_eq!(control.runtime().scheduler().account_balance(&actor_id), 6);

        // Scheduler should refuse to schedule further work for the actor while debt exceeds limit
        let err = control
            .send_message(
                actor_id.clone(),
                facet_id.clone(),
                preserves::IOValue::new(preserves::SignedInteger::from(-1)),
            )
            .expect_err("flow control should block the actor");
        match err {
            RuntimeError::Init(message) => assert!(message.contains("No turn executed")),
            other => panic!("unexpected error variant: {:?}", other),
        }
    };

    // Rewinding should reset the account balance on a fresh runtime
    {
        let mut control = Control::new(config.clone()).unwrap();
        control.back(1).unwrap();
        assert_eq!(control.runtime().scheduler().account_balance(&actor_id), 0);
    }
}
