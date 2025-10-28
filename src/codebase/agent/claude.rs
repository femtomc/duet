//! Stub implementation of a Claude Code agent entity.

use super::{AgentEntity, AgentExchange, exchanges_from_preserves, exchanges_to_preserves};
use crate::runtime::actor::{Activation, Entity, HydratableEntity};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityRegistry;
use crate::runtime::turn::Handle;
use preserves::ValueImpl;
use std::sync::Mutex;

/// Entity type name registered in the global registry.
pub const ENTITY_TYPE: &str = "agent-claude-code";
/// Agent kind identifier exposed in dataspace assertions.
pub const CLAUDE_KIND: &str = "claude-code";

/// Record label emitted for responses.
pub const RESPONSE_LABEL: &str = "agent-response";

/// Record label consumed for requests.
pub const REQUEST_LABEL: &str = "agent-request";

/// Minimal Claude Code agent entity.
///
/// This stub implements deterministic behaviour suitable for testing and for
/// demonstrating the persistence/time-travel pipeline. Real integrations can
/// swap in their own implementation while reusing the surrounding helpers.
pub struct ClaudeCodeAgent {
    exchanges: Mutex<Vec<AgentExchange>>,
}

impl ClaudeCodeAgent {
    /// Create a new agent with empty history.
    pub fn new() -> Self {
        Self {
            exchanges: Mutex::new(Vec::new()),
        }
    }

    fn handle_prompt(prompt: &str) -> String {
        format!("Claude Code (stub) suggestion: {}", prompt.trim())
    }

    fn parse_request(value: &preserves::IOValue) -> ActorResult<(String, String)> {
        if !value.is_record() {
            return Err(ActorError::InvalidActivation(
                "agent request must be a record".into(),
            ));
        }

        if value
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref() == REQUEST_LABEL)
            != Some(true)
        {
            return Err(ActorError::InvalidActivation(
                "agent request must use agent-request label".into(),
            ));
        }

        if value.len() < 2 {
            return Err(ActorError::InvalidActivation(
                "agent request requires id and prompt".into(),
            ));
        }

        let request_id = value
            .index(0)
            .as_string()
            .ok_or_else(|| ActorError::InvalidActivation("agent request id must be string".into()))?
            .to_string();

        let prompt = value
            .index(1)
            .as_string()
            .ok_or_else(|| ActorError::InvalidActivation("agent prompt must be string".into()))?
            .to_string();

        Ok((request_id, prompt))
    }

    fn process_request(
        &self,
        activation: &mut Activation,
        request_id: String,
        prompt: String,
    ) -> ActorResult<()> {
        let mut exchanges = self.exchanges.lock().unwrap();
        if exchanges
            .iter()
            .any(|exchange| exchange.request_id == request_id)
        {
            return Ok(());
        }

        let response = Self::handle_prompt(&prompt);
        exchanges.push(AgentExchange {
            request_id: request_id.clone(),
            prompt: prompt.clone(),
            response: response.clone(),
        });
        drop(exchanges);

        let response_record = preserves::IOValue::record(
            preserves::IOValue::symbol(RESPONSE_LABEL),
            vec![
                preserves::IOValue::new(request_id),
                preserves::IOValue::new(prompt),
                preserves::IOValue::new(response),
                preserves::IOValue::symbol(self.agent_kind()),
            ],
        );

        activation.assert(Handle::new(), response_record);
        Ok(())
    }
}

impl AgentEntity for ClaudeCodeAgent {
    fn agent_kind(&self) -> &'static str {
        CLAUDE_KIND
    }
}

impl Entity for ClaudeCodeAgent {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        match Self::parse_request(payload) {
            Ok((request_id, prompt)) => self.process_request(activation, request_id, prompt),
            Err(_) => Ok(()),
        }
    }

    fn on_assert(
        &self,
        activation: &mut Activation,
        _handle: &Handle,
        value: &preserves::IOValue,
    ) -> ActorResult<()> {
        if let Ok((request_id, prompt)) = Self::parse_request(value) {
            self.process_request(activation, request_id, prompt)?;
        }
        Ok(())
    }
}

/// Register the Claude Code agent in the entity registry.
pub fn register(registry: &EntityRegistry) {
    registry.register_hydratable(ENTITY_TYPE, |_config| Ok(ClaudeCodeAgent::new()));
}

impl HydratableEntity for ClaudeCodeAgent {
    fn snapshot_state(&self) -> preserves::IOValue {
        let exchanges = self.exchanges.lock().unwrap();
        exchanges_to_preserves(&exchanges)
    }

    fn restore_state(&mut self, state: &preserves::IOValue) -> ActorResult<()> {
        let exchanges = exchanges_from_preserves(state);
        let mut guard = self.exchanges.lock().unwrap();
        *guard = exchanges;
        Ok(())
    }
}
