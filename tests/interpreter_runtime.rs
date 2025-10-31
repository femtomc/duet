use std::sync::Once;

use duet::interpreter::protocol::{TOOL_RESULT_RECORD_LABEL, wait_record_from_value};
use duet::interpreter::{InstanceRecord, InstanceStatus, InterpreterEntity, RUN_MESSAGE_LABEL};
use duet::runtime::RuntimeConfig;
use duet::runtime::actor::{Activation, CapabilitySpec, Entity};
use duet::runtime::control::Control;
use duet::runtime::error::ActorResult;
use duet::runtime::registry::EntityCatalog;
use duet::runtime::state::{CapabilityMetadata, CapabilityTarget};
use duet::runtime::turn::{ActorId, FacetId, Handle};
use duet::util::io_value::record_with_label;
use preserves::IOValue;
use tempfile::TempDir;
use uuid::Uuid;

struct SignalEntity;

impl Entity for SignalEntity {
    fn on_message(&self, activation: &mut Activation, _payload: &IOValue) -> ActorResult<()> {
        let signal = IOValue::record(IOValue::symbol("ready"), Vec::new());
        activation.assert(Handle::new(), signal);
        Ok(())
    }
}

struct ToolCapabilityEntity {
    capability: Uuid,
}

impl ToolCapabilityEntity {
    fn from_config(config: IOValue) -> Self {
        let capability_str = config
            .as_string()
            .expect("capability id must be a string")
            .to_string();
        let capability = Uuid::parse_str(&capability_str).expect("invalid capability id");
        Self { capability }
    }
}

impl Entity for ToolCapabilityEntity {
    fn on_message(&self, activation: &mut Activation, payload: &IOValue) -> ActorResult<()> {
        if let Some(view) = record_with_label(payload, "grant-capability") {
            if view.len() >= 2 {
                let actor_uuid =
                    Uuid::parse_str(view.field_string(0).expect("holder actor id").as_ref())
                        .expect("invalid actor id");
                let facet_uuid =
                    Uuid::parse_str(view.field_string(1).expect("holder facet id").as_ref())
                        .expect("invalid facet id");

                let holder_actor = ActorId::from_uuid(actor_uuid);
                let holder_facet = FacetId::from_uuid(facet_uuid);
                let spec = CapabilitySpec {
                    holder: holder_actor.clone(),
                    holder_facet: holder_facet.clone(),
                    target: Some(CapabilityTarget {
                        actor: holder_actor.clone(),
                        facet: Some(holder_facet.clone()),
                    }),
                    kind: "test/tool".into(),
                    attenuation: Vec::new(),
                };
                activation.grant_capability_with_id(self.capability, spec);
            }
        }
        Ok(())
    }

    fn on_capability_invoke(
        &self,
        _activation: &mut Activation,
        _metadata: &CapabilityMetadata,
        _payload: &IOValue,
    ) -> ActorResult<IOValue> {
        Ok(IOValue::record(
            IOValue::symbol("tool-output"),
            vec![IOValue::new("ok".to_string())],
        ))
    }
}

struct LocalEchoEntity;

impl Entity for LocalEchoEntity {
    fn on_message(&self, activation: &mut Activation, payload: &IOValue) -> ActorResult<()> {
        if payload
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref() == "ping")
            .unwrap_or(false)
        {
            let record =
                IOValue::record(IOValue::symbol("attached-handled"), vec![payload.clone()]);
            activation.assert(Handle::new(), record);
        }
        Ok(())
    }
}

static REGISTER_ENTITIES: Once = Once::new();

fn ensure_entities_registered() {
    REGISTER_ENTITIES.call_once(|| {
        let catalog = EntityCatalog::global();
        InterpreterEntity::register(catalog);
        catalog.register("test-signal", |_config| Ok(Box::new(SignalEntity)));
        catalog.register("test-tool", |config| {
            Ok(Box::new(ToolCapabilityEntity::from_config(config.clone())))
        });
        catalog.register("test-local", |_config| Ok(Box::new(LocalEchoEntity)));
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

#[test]
fn interpreter_tool_invocation_roundtrip() {
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

    let capability_id = Uuid::new_v4();
    let tool_actor = ActorId::new();
    let tool_facet = FacetId::new();

    control
        .register_entity(
            tool_actor.clone(),
            tool_facet.clone(),
            "test-tool".to_string(),
            IOValue::new(capability_id.to_string()),
        )
        .unwrap();

    let grant_payload = IOValue::record(
        IOValue::symbol("grant-capability"),
        vec![
            IOValue::new(interpreter_actor.0.to_string()),
            IOValue::new(interpreter_facet.0.to_string()),
        ],
    );

    control
        .send_message(tool_actor.clone(), tool_facet.clone(), grant_payload)
        .unwrap();

    let program = format!(
        "(workflow tool-demo)\n (roles (workspace :capability \"{cap}\"))\n (state start\n  (action (invoke-tool :role workspace :capability \"capability\" :payload (record request \"payload\") :tag tool-req))\n  (await (tool-result :tag \"tool-req\"))\n  (terminal))",
        cap = capability_id
    );

    control
        .send_message(
            interpreter_actor.clone(),
            interpreter_facet.clone(),
            IOValue::record(
                IOValue::symbol(RUN_MESSAGE_LABEL),
                vec![IOValue::new(program)],
            ),
        )
        .unwrap();

    // Process capability invocation and result propagation.
    control.step(5).unwrap();

    let assertions = control.list_assertions_for_actor(&interpreter_actor);
    let (_, result_value) = assertions
        .iter()
        .find(|(_, value)| {
            value
                .label()
                .as_symbol()
                .map(|sym| sym.as_ref() == TOOL_RESULT_RECORD_LABEL)
                .unwrap_or(false)
        })
        .expect("tool result assertion");

    let result_view = record_with_label(result_value, TOOL_RESULT_RECORD_LABEL).unwrap();
    assert_eq!(result_view.field_string(1).as_deref(), Some("tool-req"));
    assert_eq!(result_view.field_string(2).as_deref(), Some("workspace"));
    assert_eq!(result_view.field_string(3).as_deref(), Some("capability"));
    assert_eq!(
        result_view.field_string(4).as_deref(),
        Some(capability_id.to_string().as_str())
    );

    let output_value = result_view.field(5);
    let output_view = record_with_label(&output_value, "tool-output").unwrap();
    assert_eq!(output_view.field_string(0).as_deref(), Some("ok"));

    // Allow the interpreter to observe the tool result and finish.
    control.step(5).unwrap();

    // Ensure the interpreter instance completed successfully.
    let final_assertions = control
        .runtime()
        .assertions_for_actor(&interpreter_actor)
        .expect("interpreter assertions after completion");

    let completed = final_assertions
        .iter()
        .filter_map(|(_, value)| InstanceRecord::parse(value))
        .find(|record| matches!(record.status, InstanceStatus::Completed));

    assert!(completed.is_some(), "interpreter instance should complete");
}

#[test]
fn interpreter_attach_entity_observes_local_assertions() {
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

    let program = r#"(workflow attach-local)
(roles (helper :label "Helper"))
(defn send-ping (actor facet)
  (action (send :actor actor
                :facet facet
                :value (record ping "hello"))))
(state start
  (attach-entity :role helper :entity-type "test-local")
  (send-ping (role-property helper "actor")
             (role-property helper "facet"))
  (await (record attached-handled :field 0 :equals (record ping "hello")))
  (terminal))"#;

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

    control.step(10).unwrap();

    let assertions = control
        .runtime()
        .assertions_for_actor(&interpreter_actor)
        .expect("interpreter assertions");

    let handled = assertions.iter().any(|(_, value)| {
        value
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref() == "attached-handled")
            .unwrap_or(false)
    });

    assert!(handled, "attached entity should observe local assertions");
}

#[test]
fn assert_record_resolves_role_property() {
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

    let program = r#"(workflow role-prop-assert)
(roles (helper :entity-type "test-local"))
(state start
  (attach-entity :role helper :entity-type "test-local")
  (action (assert (record mock-request
                          (role-property helper "entity")
                          "req-1")))
  (await (record mock-response :field 1 :equals "req-1"))
  (terminal))"#;

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
        runtime.step().unwrap();
    }

    control.drain_pending().unwrap();

    let assertions = control
        .runtime()
        .assertions_for_actor(&interpreter_actor)
        .expect("interpreter assertions available");

    let request = assertions
        .iter()
        .find_map(|(_, value)| record_with_label(value, "mock-request"))
        .expect("mock-request asserted");

    let entity_id = request
        .field_string(0)
        .expect("first field should be resolved entity id");
    assert!(
        Uuid::parse_str(&entity_id).is_ok(),
        "entity id should be a UUID, got {}",
        entity_id
    );
}
