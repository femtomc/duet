//! Stub implementation of a Claude Code agent entity.

use super::{AgentEntity, AgentExchange, exchanges_from_preserves, exchanges_to_preserves};
use crate::runtime::actor::{Activation, Entity, HydratableEntity};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityRegistry;
use crate::runtime::turn::Handle;
use once_cell::sync::Lazy;
use preserves::ValueImpl;
use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::Mutex;

/// Global agent configuration shared across runtime instances.
#[derive(Debug, Clone)]
struct AgentSettings {
    mode: AgentMode,
    command: Option<String>,
    args: Vec<String>,
}

impl Default for AgentSettings {
    fn default() -> Self {
        Self {
            mode: AgentMode::Auto,
            command: None,
            args: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AgentMode {
    Auto,
    Stub,
}

static SETTINGS: Lazy<Mutex<AgentSettings>> = Lazy::new(|| Mutex::new(AgentSettings::default()));

/// Force the Claude agent into stub mode (useful for deterministic tests).
pub fn set_stub_mode(enabled: bool) {
    let mut settings = SETTINGS.lock().unwrap();
    settings.mode = if enabled {
        AgentMode::Stub
    } else {
        AgentMode::Auto
    };
}

/// Configure the external command used to invoke Claude Code.
pub fn set_external_command(command: Option<String>, args: Vec<String>) {
    let mut settings = SETTINGS.lock().unwrap();
    settings.command = command;
    settings.args = args;
}

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

    fn handle_prompt(prompt: &str) -> ActorResult<String> {
        let (mode, command, args) = {
            let settings = SETTINGS.lock().unwrap();
            (
                settings.mode,
                settings
                    .command
                    .clone()
                    .unwrap_or_else(|| "claude".to_string()),
                settings.args.clone(),
            )
        };

        match mode {
            AgentMode::Stub => Ok(format!("Claude Code (stub) suggestion: {}", prompt.trim())),
            AgentMode::Auto => Self::invoke_external(&command, &args, prompt).map_err(|err| {
                ActorError::InvalidActivation(format!("Claude CLI invocation failed: {err}"))
            }),
        }
    }

    fn invoke_external(cmd: &str, args: &[String], prompt: &str) -> Result<String, String> {
        let mut command = Command::new(cmd);
        if !args.is_empty() {
            command.args(args);
        }

        command.stdin(Stdio::piped()).stdout(Stdio::piped());

        let mut child = command
            .spawn()
            .map_err(|err| format!("failed to spawn '{cmd}': {err}"))?;
        if let Some(mut stdin) = child.stdin.take() {
            stdin
                .write_all(prompt.as_bytes())
                .map_err(|err| format!("failed to write prompt to '{cmd}': {err}"))?;
        }

        let output = child
            .wait_with_output()
            .map_err(|err| format!("failed to read output from '{cmd}': {err}"))?;
        if !output.status.success() {
            return Err(format!(
                "'{cmd}' exited with status {}",
                output.status.code().unwrap_or(-1)
            ));
        }

        let response = String::from_utf8(output.stdout)
            .map_err(|err| format!("non-UTF8 output from '{cmd}': {err}"))?;
        Ok(response.trim().to_string())
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

        let response = Self::handle_prompt(&prompt)?;
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
