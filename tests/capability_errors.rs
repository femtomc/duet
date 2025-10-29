//! Capability error handling tests
//!
//! Verifies that the runtime maps capability failures into the
//! documented `CapabilityError` variants rather than leaking lower
//! level actor errors.

use duet::runtime::actor::{Activation, CapabilitySpec, Entity};
use duet::runtime::error::{CapabilityError, RuntimeError};
use duet::runtime::registry::EntityCatalog;
use duet::runtime::state::CapabilityTarget;
use duet::runtime::turn::{ActorId, FacetId};
use duet::runtime::{Control, RuntimeConfig};
use once_cell::sync::Lazy;
use preserves::IOValue;
use std::sync::Mutex;
use tempfile::TempDir;
use uuid::Uuid;

/// Register the test entity exactly once for all tests.
static REGISTER_ENTITY: Lazy<()> = Lazy::new(|| {
    EntityCatalog::global().register("cap-error-harness", |_config| {
        Ok(Box::new(CapabilityHarness::default()))
    });
});

#[derive(Default)]
struct CapabilityHarness {
    last_capability: Mutex<Option<Uuid>>,
}

impl Entity for CapabilityHarness {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &IOValue,
    ) -> duet::runtime::error::ActorResult<()> {
        if let Some(symbol) = payload.as_symbol() {
            match symbol.as_ref() {
                "grant" => {
                    let cap_id = activation.grant_capability(CapabilitySpec {
                        holder: activation.actor_id.clone(),
                        holder_facet: activation.current_facet.clone(),
                        target: Some(CapabilityTarget {
                            actor: activation.actor_id.clone(),
                            facet: Some(activation.current_facet.clone()),
                        }),
                        kind: "test/capability".to_string(),
                        attenuation: Vec::new(),
                    });

                    *self
                        .last_capability
                        .lock()
                        .expect("capability mutex poisoned") = Some(cap_id);
                }
                "revoke" => {
                    if let Some(cap_id) = *self
                        .last_capability
                        .lock()
                        .expect("capability mutex poisoned")
                    {
                        activation.revoke_capability(cap_id);
                    }
                }
                _ => {}
            }
        }

        Ok(())
    }

    fn on_capability_invoke(
        &self,
        _activation: &mut Activation,
        _capability: &duet::runtime::state::CapabilityMetadata,
        payload: &IOValue,
    ) -> duet::runtime::error::ActorResult<IOValue> {
        if payload
            .as_symbol()
            .map(|sym| sym.as_ref() == "deny")
            .unwrap_or(false)
        {
            return Err(duet::runtime::error::ActorError::InvalidActivation(
                "invocation denied".into(),
            ));
        }

        Ok(IOValue::symbol("ok"))
    }
}

fn new_control() -> (Control, TempDir) {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    let control = Control::init(config).expect("control init failed");
    (control, temp)
}

#[test]
fn capability_not_found_returns_error() {
    let (mut control, _temp) = new_control();
    let bogus = Uuid::new_v4();

    let err = control
        .invoke_capability(bogus, IOValue::symbol("payload"))
        .expect_err("invoking missing capability should fail");

    match err {
        RuntimeError::Capability(CapabilityError::NotFound(id)) => {
            assert_eq!(id, bogus, "not-found should echo requested id");
        }
        other => panic!("expected CapabilityError::NotFound, got {other:?}"),
    }
}

#[test]
fn revoked_capability_produces_revoked_error() {
    Lazy::force(&REGISTER_ENTITY);

    let (mut control, _temp) = new_control();
    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    control
        .register_entity(
            actor_id.clone(),
            facet_id.clone(),
            "cap-error-harness".into(),
            IOValue::symbol("config"),
        )
        .expect("entity registration");

    control
        .send_message(actor_id.clone(), facet_id.clone(), IOValue::symbol("grant"))
        .expect("grant message should execute");

    let capability = control
        .list_capabilities()
        .into_iter()
        .find(|cap| cap.kind == "test/capability")
        .expect("capability to be granted");

    control
        .send_message(
            actor_id.clone(),
            facet_id.clone(),
            IOValue::symbol("revoke"),
        )
        .expect("revoke message should execute");

    let err = control
        .invoke_capability(capability.id, IOValue::symbol("payload"))
        .expect_err("invoking revoked capability should fail");

    match err {
        RuntimeError::Capability(CapabilityError::Revoked(id)) => {
            assert_eq!(id, capability.id);
        }
        other => panic!("expected CapabilityError::Revoked, got {other:?}"),
    }
}

#[test]
fn entity_denial_maps_to_capability_denied() {
    Lazy::force(&REGISTER_ENTITY);

    let (mut control, _temp) = new_control();
    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    control
        .register_entity(
            actor_id.clone(),
            facet_id.clone(),
            "cap-error-harness".into(),
            IOValue::symbol("config"),
        )
        .expect("entity registration");

    control
        .send_message(actor_id.clone(), facet_id.clone(), IOValue::symbol("grant"))
        .expect("grant message should execute");

    let capability = control
        .list_capabilities()
        .into_iter()
        .find(|cap| cap.kind == "test/capability")
        .expect("capability to be granted");

    let err = control
        .invoke_capability(capability.id, IOValue::symbol("deny"))
        .expect_err("denied invocation should fail");

    match err {
        RuntimeError::Capability(CapabilityError::Denied(id, _detail)) => {
            assert_eq!(id, capability.id);
        }
        other => panic!("expected CapabilityError::Denied, got {other:?}"),
    }
}
