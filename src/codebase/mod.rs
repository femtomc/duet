//! Built-in entities and behaviours shipped with the Duet runtime.
//!
//! The `codebase` module provides foundational entities that power
//! the `codebased` daemon.  It currently includes:
//!   * `workspace` – publishes a causal view of the filesystem and
//!     issues capabilities for reading/modifying files.
//!   * `echo` / `counter` – small reference implementations used by
//!     tests/examples until richer catalogues arrive.

use std::convert::TryFrom;
use std::sync::{Mutex, Once};

use preserves::ValueImpl;

use crate::runtime::actor::{Activation, Entity, HydratableEntity};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityRegistry;
use crate::runtime::turn::Handle;

pub mod workspace;

static INIT: Once = Once::new();

/// Register all built-in entities provided by this crate.
///
/// The call is idempotent; it is safe to invoke multiple times.
pub fn register_codebase_entities() {
    INIT.call_once(|| {
        let registry = EntityRegistry::global();

        workspace::register(registry);

        registry.register("echo", |config| {
            let topic = config
                .as_string()
                .map(|s| s.to_string())
                .unwrap_or_else(|| "echo".to_string());
            Ok(Box::new(EchoEntity { topic }))
        });

        registry.register_hydratable("counter", |config| {
            let initial = config
                .as_signed_integer()
                .and_then(|value| i64::try_from(value.as_ref()).ok())
                .unwrap_or(0);
            Ok(CounterEntity::new(initial))
        });
    });
}

struct EchoEntity {
    topic: String,
}

impl Entity for EchoEntity {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        let fact = preserves::IOValue::record(
            preserves::IOValue::symbol("echo"),
            vec![preserves::IOValue::new(self.topic.clone()), payload.clone()],
        );
        activation.assert(Handle::new(), fact);
        Ok(())
    }
}

struct CounterEntity {
    value: Mutex<i64>,
}

impl CounterEntity {
    fn new(initial: i64) -> Self {
        Self {
            value: Mutex::new(initial),
        }
    }
}

impl Entity for CounterEntity {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        let delta = payload
            .as_signed_integer()
            .and_then(|value| i64::try_from(value.as_ref()).ok())
            .unwrap_or(1);

        let mut guard = self.value.lock().unwrap();
        *guard += delta;

        let fact = preserves::IOValue::record(
            preserves::IOValue::symbol("counter"),
            vec![preserves::IOValue::new(*guard)],
        );
        activation.assert(Handle::new(), fact);
        Ok(())
    }
}

impl HydratableEntity for CounterEntity {
    fn snapshot_state(&self) -> preserves::IOValue {
        let value = *self.value.lock().unwrap();
        preserves::IOValue::new(value)
    }

    fn restore_state(&mut self, state: &preserves::IOValue) -> ActorResult<()> {
        let value = state
            .as_signed_integer()
            .and_then(|v| i64::try_from(v.as_ref()).ok())
            .ok_or_else(|| {
                ActorError::InvalidActivation("counter state must be an integer".into())
            })?;
        *self.value.lock().unwrap() = value;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registers_entities_once() {
        register_codebase_entities();
        register_codebase_entities();

        let registry = EntityRegistry::global();
        assert!(registry.has_type("echo"));
        assert!(registry.has_type("counter"));
        assert!(registry.has_type("workspace"));
    }
}
