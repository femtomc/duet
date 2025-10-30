use std::sync::Once;

use duet::interpreter::protocol::wait_record_from_value;
use duet::interpreter::{InstanceRecord, InstanceStatus, InterpreterEntity, RUN_MESSAGE_LABEL};
use duet::runtime::RuntimeConfig;
use duet::runtime::actor::{Activation, Entity};
use duet::runtime::control::Control;
use duet::runtime::error::ActorResult;
use duet::runtime::registry::EntityCatalog;
use duet::runtime::turn::{ActorId, FacetId, Handle};
use preserves::IOValue;
use tempfile::TempDir;

struct SignalEntity;

impl Entity for SignalEntity {
    fn on_message(&self, activation: &mut Activation, _payload: &IOValue) -> ActorResult<()> {
        let signal = IOValue::record(IOValue::symbol("ready"), Vec::new());
        activation.assert(Handle::new(), signal);
        Ok(())
    }
}

static REGISTER_ENTITIES: Once = Once::new();

fn ensure_entities_registered() {
    REGISTER_ENTITIES.call_once(|| {
        let catalog = EntityCatalog::global();
        InterpreterEntity::register(catalog);
        catalog.register("test-signal", |_config| Ok(Box::new(SignalEntity)));
    });
}

#[test]
fn interpreter_resumes_when_signal_asserted() {
    ensure_entities_registered();

    let temp_dir = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp_dir.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    let mut control = Control::init(config).unwrap();

    let interpreter_actor = ActorId::new();
    let interpreter_facet = FacetId::new();

    control
        .register_entity(
            interpreter_actor.clone(),
            interpreter_facet.clone(),
            "interpreter".to_string(),
            IOValue::symbol("unused"),
        )
        .unwrap();

    let program = r#"(workflow wait-demo)
(state start
  (await (signal ready))
  (goto done))
(state done (terminal))"#;

    let run_payload = IOValue::record(
        IOValue::symbol(RUN_MESSAGE_LABEL),
        vec![IOValue::new(program.to_string())],
    );

    {
        let runtime = control.runtime_mut();
        runtime.send_message(
            interpreter_actor.clone(),
            interpreter_facet.clone(),
            run_payload,
        );
        runtime
            .step()
            .unwrap()
            .expect("turn should execute for interpreter run");
    }

    let assertions = control
        .runtime()
        .assertions_for_actor(&interpreter_actor)
        .expect("interpreter assertions available");

    let waiting_record = assertions
        .iter()
        .filter_map(|(_, value)| InstanceRecord::parse(value))
        .find(|record| matches!(record.status, InstanceStatus::Waiting(_)))
        .expect("interpreter should be waiting");

    let instance_id = waiting_record.instance_id.clone();

    let wait_asserted = assertions.iter().any(|(_, value)| {
        wait_record_from_value(value)
            .map(|wait| wait.instance_id == instance_id)
            .unwrap_or(false)
    });
    assert!(wait_asserted, "wait record should be present");

    let signal_actor = ActorId::new();
    let signal_facet = FacetId::new();

    control
        .register_entity(
            signal_actor.clone(),
            signal_facet.clone(),
            "test-signal".to_string(),
            IOValue::symbol("unused"),
        )
        .unwrap();

    {
        let runtime = control.runtime_mut();
        runtime.send_message(
            signal_actor.clone(),
            signal_facet.clone(),
            IOValue::symbol("trigger"),
        );
        runtime
            .step()
            .unwrap()
            .expect("turn should execute for signal");
    }

    control.step(10).unwrap();

    for _ in 0..10 {
        let waits_remaining = control
            .runtime()
            .assertions_for_actor(&interpreter_actor)
            .expect("interpreter assertions for polling")
            .iter()
            .any(|(_, value)| {
                wait_record_from_value(value)
                    .map(|wait| wait.instance_id == instance_id)
                    .unwrap_or(false)
            });
        if !waits_remaining {
            break;
        }
        control.step(1).unwrap();
    }

    let final_assertions = control
        .runtime()
        .assertions_for_actor(&interpreter_actor)
        .expect("interpreter assertions after resume");

    let completed = final_assertions
        .iter()
        .filter_map(|(_, value)| InstanceRecord::parse(value))
        .find(|record| record.instance_id == instance_id);

    for record in final_assertions
        .iter()
        .filter_map(|(_, value)| InstanceRecord::parse(value))
    {
        println!(
            "final instance {} status {:?}",
            record.instance_id, record.status
        );
    }

    match completed {
        Some(record) => assert!(
            matches!(record.status, InstanceStatus::Completed),
            "interpreter instance should complete"
        ),
        None => panic!("no interpreter instance assertion after resume"),
    }

    let wait_remaining = final_assertions.iter().any(|(_, value)| {
        wait_record_from_value(value)
            .map(|wait| wait.instance_id == instance_id)
            .unwrap_or(false)
    });
    assert!(
        !wait_remaining,
        "wait record should be retracted after resume"
    );
}
