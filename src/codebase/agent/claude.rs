//! Stub implementation of a Claude Code agent entity.

use super::{AgentEntity, AgentExchange, exchanges_from_preserves, exchanges_to_preserves};
use crate::runtime::AsyncMessage;
use crate::runtime::actor::{Activation, Entity, HydratableEntity};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityCatalog;
use crate::runtime::turn::{Handle, TurnOutput};
use chrono::Utc;
use once_cell::sync::Lazy;
use preserves::ValueImpl;
use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use uuid::Uuid;

/// Global default configuration used when instantiating Claude agents.
#[derive(Debug, Clone)]
struct AgentSettings {
    command: Option<String>,
    args: Vec<String>,
}

impl Default for AgentSettings {
    fn default() -> Self {
        Self {
            command: std::env::var("DUET_CLAUDE_COMMAND")
                .ok()
                .filter(|s| !s.is_empty()),
            args: std::env::var("DUET_CLAUDE_ARGS")
                .ok()
                .map(|value| value.split_whitespace().map(str::to_string).collect())
                .unwrap_or_default(),
        }
    }
}

static DEFAULT_SETTINGS: Lazy<Mutex<AgentSettings>> =
    Lazy::new(|| Mutex::new(AgentSettings::default()));

/// Configure the external command used to invoke Claude Code.
pub fn set_external_command(command: Option<String>, args: Vec<String>) {
    let mut settings = DEFAULT_SETTINGS.lock().unwrap();
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
    settings: AgentSettings,
    exchanges: Mutex<Vec<AgentExchange>>,
}

impl ClaudeCodeAgent {
    /// Create a new agent with empty history.
    pub fn new() -> Self {
        let settings = {
            let guard = DEFAULT_SETTINGS.lock().unwrap();
            guard.clone()
        };
        Self::with_settings(settings)
    }

    fn with_settings(settings: AgentSettings) -> Self {
        Self {
            settings,
            exchanges: Mutex::new(Vec::new()),
        }
    }

    fn execute_prompt(settings: &AgentSettings, prompt: &str) -> Result<String, String> {
        let command = settings
            .command
            .clone()
            .unwrap_or_else(|| "claude".to_string());
        Self::invoke_external(&command, &settings.args, prompt)
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
            stdin
                .write_all(b"\n")
                .map_err(|err| format!("failed to terminate prompt for '{cmd}': {err}"))?;
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

    fn parse_response(value: &preserves::IOValue) -> ActorResult<(String, String, String, String)> {
        parse_response_fields(value).ok_or_else(|| {
            ActorError::InvalidActivation("agent response must use agent-response label and include request, prompt, response, and agent kind".into())
        })
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

    fn schedule_request(
        &self,
        activation: &mut Activation,
        request_id: String,
        prompt: String,
    ) -> ActorResult<()> {
        let actor = activation.actor_id.clone();
        let facet = activation.current_facet.clone();
        let request_uuid = Uuid::new_v4();
        let settings = self.settings.clone();
        let async_sender = activation.async_sender();
        activation.outputs.push(TurnOutput::ExternalRequest {
            request_id: request_uuid,
            service: "claude-code".to_string(),
            request: preserves::IOValue::record(
                preserves::IOValue::symbol(REQUEST_LABEL),
                vec![
                    preserves::IOValue::new(request_id.clone()),
                    preserves::IOValue::new(prompt.clone()),
                ],
            ),
        });

        let agent_kind = self.agent_kind().to_string();

        let settings_clone = settings.clone();

        if let Some(async_sender) = async_sender {
            std::thread::spawn(move || {
                let response = match Self::execute_prompt(&settings_clone, &prompt) {
                    Ok(value) => value,
                    Err(err) => format!("Claude Code error: {err}"),
                };

                let timestamp = Utc::now().to_rfc3339();
                let response_record = preserves::IOValue::record(
                    preserves::IOValue::symbol(RESPONSE_LABEL),
                    vec![
                        preserves::IOValue::new(request_id),
                        preserves::IOValue::new(prompt),
                        preserves::IOValue::new(response),
                        preserves::IOValue::symbol(agent_kind),
                        preserves::IOValue::new(timestamp),
                    ],
                );

                let message = AsyncMessage {
                    actor,
                    facet,
                    payload: response_record,
                };

                let _ = async_sender.send(message);
            });
        }

        Ok(())
    }

    fn record_response(
        &self,
        activation: &mut Activation,
        request_id: String,
        prompt: String,
        response: String,
        agent_kind: String,
    ) -> ActorResult<()> {
        let mut exchanges = self.exchanges.lock().unwrap();
        if exchanges
            .iter()
            .any(|exchange| exchange.request_id == request_id)
        {
            return Ok(());
        }

        exchanges.push(AgentExchange {
            request_id: request_id.clone(),
            prompt: prompt.clone(),
            response: response.clone(),
        });
        drop(exchanges);

        let timestamp = Utc::now().to_rfc3339();

        let response_record = preserves::IOValue::record(
            preserves::IOValue::symbol(RESPONSE_LABEL),
            vec![
                preserves::IOValue::new(request_id),
                preserves::IOValue::new(prompt),
                preserves::IOValue::new(response),
                preserves::IOValue::symbol(agent_kind),
                preserves::IOValue::new(timestamp),
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
        match payload
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref() == REQUEST_LABEL)
        {
            Some(true) => match Self::parse_request(payload) {
                Ok((request_id, prompt)) => self.schedule_request(activation, request_id, prompt),
                Err(_) => Ok(()),
            },
            _ => match Self::parse_response(payload) {
                Ok((request_id, prompt, response, agent)) => {
                    self.record_response(activation, request_id, prompt, response, agent)
                }
                Err(_) => Ok(()),
            },
        }
    }

    fn on_assert(
        &self,
        activation: &mut Activation,
        _handle: &Handle,
        value: &preserves::IOValue,
    ) -> ActorResult<()> {
        if let Ok((request_id, prompt)) = Self::parse_request(value) {
            self.schedule_request(activation, request_id, prompt)?;
        }
        Ok(())
    }
}

/// Register the Claude Code agent in the entity catalog.
pub fn register(catalog: &EntityCatalog) {
    catalog.register_hydratable(ENTITY_TYPE, |config| {
        let defaults = {
            let guard = DEFAULT_SETTINGS.lock().unwrap();
            guard.clone()
        };
        let settings = settings_from_config(config).unwrap_or(defaults);
        Ok(ClaudeCodeAgent::with_settings(settings))
    });
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

fn parse_response_fields(value: &preserves::IOValue) -> Option<(String, String, String, String)> {
    if !value.is_record() {
        return None;
    }

    if value
        .label()
        .as_symbol()
        .map(|sym| sym.as_ref() == RESPONSE_LABEL)
        != Some(true)
    {
        return None;
    }

    if value.len() < 4 {
        return None;
    }

    let request_id = value.index(0).as_string()?.to_string();
    let prompt = value.index(1).as_string()?.to_string();
    let response = value.index(2).as_string()?.to_string();
    let agent_kind = value
        .index(3)
        .as_symbol()
        .map(|sym| sym.as_ref().to_string())
        .unwrap_or_default();

    Some((request_id, prompt, response, agent_kind))
}

fn settings_from_config(value: &preserves::IOValue) -> Option<AgentSettings> {
    if !value.is_record() {
        return None;
    }

    let label_value = value.label();
    let label_symbol = label_value.as_symbol()?;
    if label_symbol.as_ref() != "claude-config" {
        return None;
    }

    let mut settings = AgentSettings::default();

    if value.len() > 0 {
        if let Some(command) = value.index(0).as_string() {
            let trimmed = command.trim();
            if !trimmed.is_empty() {
                settings.command = Some(trimmed.to_string());
            }
        }
    }

    if value.len() > 1 {
        if let Some(args_text) = value.index(1).as_string() {
            let args = args_text
                .split_whitespace()
                .filter(|arg| !arg.is_empty())
                .map(str::to_string)
                .collect();
            settings.args = args;
        }
    }

    Some(settings)
}
