//! Helpers for managing the interpreter instance that acts as the runtime control plane.
//!
//! The control interpreter is a regular `interpreter` entity that advertises its presence
//! via a `control-interpreter` dataspace assertion.  Clients discover it after handshake
//! and route mutation requests through the programs it exposes.

use crate::runtime::control::Control;
use crate::runtime::error::RuntimeError;
use crate::runtime::turn::{ActorId, FacetId};
use crate::util::io_value::{RecordView, record_with_label};
use preserves::IOValue;
use uuid::Uuid;

/// Dataspace label used to advertise the control interpreter.
pub const CONTROL_INTERPRETER_LABEL: &str = "control-interpreter";

/// Handle describing the interpreter entity that brokers control-plane requests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlInterpreterHandle {
    /// Actor hosting the interpreter entity.
    pub actor: ActorId,
    /// Facet the interpreter entity is attached to.
    pub facet: FacetId,
}

impl ControlInterpreterHandle {
    fn from_record_view(record: RecordView<'_>) -> Option<Self> {
        if record.len() < 2 {
            return None;
        }

        let actor = record.field_string(0)?;
        let facet = record.field_string(1)?;
        let actor_id = ActorId::from_uuid(Uuid::parse_str(&actor).ok()?);
        let facet_id = FacetId::from_uuid(Uuid::parse_str(&facet).ok()?);

        Some(ControlInterpreterHandle {
            actor: actor_id,
            facet: facet_id,
        })
    }
}

/// Attempt to locate an existing control interpreter without mutating runtime state.
pub fn discover(control: &Control) -> Option<ControlInterpreterHandle> {
    for assertion in control.list_assertions(None) {
        if let Some(record) = record_with_label(&assertion.value, CONTROL_INTERPRETER_LABEL) {
            if let Some(handle) = ControlInterpreterHandle::from_record_view(record) {
                if interpreter_entity_exists(control, &handle.actor) {
                    return Some(handle);
                }
            }
        }
    }
    None
}

/// Ensure the control interpreter exists, spawning it if necessary.
pub fn ensure(control: &mut Control) -> Result<ControlInterpreterHandle, RuntimeError> {
    if let Some(existing) = discover(control) {
        return Ok(existing);
    }

    let actor = ActorId::new();
    let facet = FacetId::new();

    control.register_entity(
        actor.clone(),
        facet.clone(),
        "interpreter".to_string(),
        IOValue::symbol("control"),
    )?;

    let announcement = IOValue::record(
        IOValue::symbol(CONTROL_INTERPRETER_LABEL),
        vec![
            IOValue::new(actor.to_string()),
            IOValue::new(facet.0.to_string()),
        ],
    );
    control.assert_value(actor.clone(), announcement)?;

    Ok(ControlInterpreterHandle { actor, facet })
}

fn interpreter_entity_exists(control: &Control, actor: &ActorId) -> bool {
    control
        .list_entities_for_actor(actor)
        .into_iter()
        .any(|info| info.entity_type == "interpreter")
}
