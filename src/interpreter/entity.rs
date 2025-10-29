use crate::interpreter::{build_ir, parse_program, Action, Condition, InterpreterHost, InterpreterRuntime, ProgramIr, RuntimeError, RuntimeEvent, WaitCondition};
use crate::runtime::actor::{Activation, Entity};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityCatalog;
use crate::runtime::turn::Handle;
use crate::util::io_value::record_with_label;
use preserves::IOValue;

/// Label expected for interpreter run requests.
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
        let record = match record_with_label(payload, RUN_LABEL) {
            Some(record) => record,
            None => return Ok(()),
        };

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
}

fn run_program(activation: &mut Activation, program: ProgramIr) -> ActorResult<()> {
    let host = ActivationHost { activation };
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
            Action::Assert(_)
            | Action::Retract(_)
            | Action::InvokeTool { .. }
            | Action::SendPrompt { .. } => Err(ActorError::InvalidActivation(
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
