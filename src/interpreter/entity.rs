use std::collections::HashMap;

use crate::interpreter::{
    build_ir, parse_program, Action, Condition, InterpreterHost, InterpreterRuntime, ProgramIr,
    RoleBinding, RuntimeError, RuntimeEvent, WaitCondition,
};
use crate::runtime::actor::{Activation, Entity};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityCatalog;
use crate::runtime::turn::{ActorId, FacetId, Handle};
use crate::util::io_value::record_with_label;
use preserves::IOValue;
use uuid::Uuid;

const DEFINE_LABEL: &str = "interpreter-define";
const RUN_LABEL: &str = "interpreter-run";

/// Entity that executes interpreter programs inside the Syndicated Actor runtime.
pub struct InterpreterEntity;

impl InterpreterEntity {
    /// Register the interpreter entity with the provided catalog.
    pub fn register(catalog: &EntityCatalog) {
        catalog.register("interpreter", |_config| Ok(Box::new(InterpreterEntity)));
    }
}

impl Entity for InterpreterEntity {
    fn on_message(&self, activation: &mut Activation, payload: &IOValue) -> ActorResult<()> {
        if let Some(record) = record_with_label(payload, DEFINE_LABEL) {
            handle_define(activation, &record)
        } else if let Some(record) = record_with_label(payload, RUN_LABEL) {
            handle_run(activation, &record)
        } else {
            Ok(())
        }
    }
}

fn handle_define(activation: &mut Activation, record: &crate::util::io_value::RecordView<'_>) -> ActorResult<()> {
    if record.len() == 0 {
        return Err(ActorError::InvalidActivation(
            "interpreter-define requires program source".into(),
        ));
    }

    let source = record
        .field_string(0)
        .ok_or_else(|| ActorError::InvalidActivation("program must be a string".into()))?;

    let program = parse_program(&source)
        .map_err(|err| ActorError::InvalidActivation(format!("parse error: {err}")))?;
    let _ = build_ir(&program)
        .map_err(|err| ActorError::InvalidActivation(format!("validation error: {err}")))?;

    // TODO: persist definitions in the dataspace; for now just acknowledge.
    let acknowledgement = IOValue::record(
        IOValue::symbol("interpreter-defined"),
        vec![IOValue::new(program.name.clone())],
    );
    activation.assert(Handle::new(), acknowledgement);
    Ok(())
}

fn handle_run(activation: &mut Activation, record: &crate::util::io_value::RecordView<'_>) -> ActorResult<()> {
    if record.len() == 0 {
        return Err(ActorError::InvalidActivation(
            "interpreter-run requires a program string".into(),
        ));
    }

    let source = record
        .field_string(0)
        .ok_or_else(|| ActorError::InvalidActivation("program must be a string".into()))?;

    let program = parse_program(&source)
        .map_err(|err| ActorError::InvalidActivation(format!("parse error: {err}")))?;
    let ir = build_ir(&program)
        .map_err(|err| ActorError::InvalidActivation(format!("validation error: {err}")))?;

    run_program(activation, ir)
}

fn run_program(activation: &mut Activation, program: ProgramIr) -> ActorResult<()> {
    let roles: HashMap<String, RoleBinding> = program
        .roles
        .iter()
        .map(|binding| (binding.name.clone(), binding.clone()))
        .collect();
    let host = ActivationHost::new(activation, roles);
    let mut runtime = InterpreterRuntime::new(host, program);

    loop {
        match runtime.tick() {
            Ok(RuntimeEvent::Progress) => continue,
            Ok(RuntimeEvent::Transition { .. }) => continue,
            Ok(RuntimeEvent::Waiting(wait)) => {
                return Err(ActorError::InvalidActivation(format!(
                    "interpreter waiting is not yet supported: {:?}",
                    wait
                )));
            }
            Ok(RuntimeEvent::Completed) => break,
            Err(RuntimeError::Host(err)) => return Err(err),
            Err(RuntimeError::UnknownState(state)) => {
                return Err(ActorError::InvalidActivation(format!(
                    "unknown state: {}",
                    state
                )))
            }
            Err(RuntimeError::NoStates) => {
                return Err(ActorError::InvalidActivation(
                    "program must define at least one state".into(),
                ))
            }
        }
    }

    Ok(())
}

struct ActivationHost<'a> {
    activation: &'a mut Activation,
    roles: HashMap<String, RoleBinding>,
}

impl<'a> ActivationHost<'a> {
    fn new(activation: &'a mut Activation, roles: HashMap<String, RoleBinding>) -> Self {
        Self { activation, roles }
    }

    fn resolve_role(&self, name: &str) -> Result<&RoleBinding, ActorError> {
        self.roles
            .get(name)
            .ok_or_else(|| ActorError::InvalidActivation(format!("unknown role: {name}")))
    }

    fn property<'b>(&self, binding: &'b RoleBinding, key: &str) -> Result<&'b String, ActorError> {
        binding
            .properties
            .get(key)
            .ok_or_else(|| ActorError::InvalidActivation(format!("role '{}' missing property '{}'", binding.name, key)))
    }
}

impl<'a> InterpreterHost for ActivationHost<'a> {
    type Error = ActorError;

    fn execute_action(&mut self, action: &Action) -> std::result::Result<(), Self::Error> {
        match action {
            Action::EmitLog(message) => {
                let record = IOValue::record(
                    IOValue::symbol("interpreter-log"),
                    vec![IOValue::new(message.clone())],
                );
                self.activation.assert(Handle::new(), record);
                Ok(())
            }
            Action::Assert(value_text) => {
                let value: IOValue = value_text
                    .parse()
                    .map_err(|err| ActorError::InvalidActivation(format!("invalid assert value: {err}")))?;
                self.activation.assert(Handle::new(), value);
                Ok(())
            }
            Action::SendPrompt {
                agent_role,
                template,
                tag,
            } => {
                let binding = self.resolve_role(agent_role)?;
                let actor_text = self.property(binding, "actor")?;
                let facet_text = self.property(binding, "facet")?;
                let actor_uuid = Uuid::parse_str(actor_text).map_err(|_| {
                    ActorError::InvalidActivation(format!("invalid actor UUID: {actor_text}"))
                })?;
                let facet_uuid = Uuid::parse_str(facet_text).map_err(|_| {
                    ActorError::InvalidActivation(format!("invalid facet UUID: {facet_text}"))
                })?;

                let mut fields = vec![IOValue::new(template.clone())];
                if let Some(tag) = tag {
                    fields.push(IOValue::new(tag.clone()));
                }
                let payload = IOValue::record(IOValue::symbol("interpreter-prompt"), fields);
                self.activation
                    .send_message(ActorId::from_uuid(actor_uuid), FacetId::from_uuid(facet_uuid), payload);
                Ok(())
            }
            Action::Retract(_) | Action::InvokeTool { .. } => Err(ActorError::InvalidActivation(
                "action not supported yet".into(),
            )),
        }
    }

    fn check_condition(&mut self, _condition: &Condition) -> std::result::Result<bool, Self::Error> {
        Ok(false)
    }

    fn poll_wait(&mut self, _wait: &WaitCondition) -> std::result::Result<bool, Self::Error> {
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime::actor::Actor;
    use crate::runtime::turn::ActorId;

    #[test]
    fn interpreter_entity_emits_log() {
        let entity = InterpreterEntity;
        let actor = Actor::new(ActorId::new());
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let program = "(workflow demo) (state start (emit (log \"hello\")))";
        let payload = IOValue::record(
            IOValue::symbol(RUN_LABEL),
            vec![IOValue::new(program.to_string())],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        assert!(activation
            .assertions_added
            .iter()
            .any(|(_, value)| matches!(value.label().as_symbol(), Some(sym) if sym.as_ref() == "interpreter-log")));
    }
}
