//! Generic OpenAI-compatible harness for base LLM endpoints.

use super::{
    AgentEntity, AgentExchange, DUET_AGENT_SYSTEM_PROMPT, REQUEST_LABEL, RESPONSE_LABEL,
    exchanges_from_preserves, exchanges_to_preserves, parse_response_fields, response_fields,
};
use crate::runtime::AsyncMessage;
use crate::runtime::actor::{Activation, Entity, HydratableEntity};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityCatalog;
use crate::runtime::turn::{Handle, TurnOutput};
use crate::util::io_value::record_with_label;
use chrono::Utc;
use once_cell::sync::Lazy;
use reqwest::blocking::Client;
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderValue};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::Mutex;
use uuid::Uuid;

/// Entity type name registered in the global registry.
pub const ENTITY_TYPE: &str = "agent-noface";
/// Default agent kind identifier exposed in dataspace assertions.
pub const HARNESS_KIND: &str = "noface";

const DEFAULT_ENDPOINT: &str = "https://api.openai.com/v1/chat/completions";
const DEFAULT_MODEL: &str = "gpt-4o-mini";
const DEFAULT_ROLE: &str = "assistant";

/// Global default configuration used when instantiating harness agents.
#[derive(Debug, Clone)]
struct AgentSettings {
    endpoint: String,
    api_key: Option<String>,
    model: String,
    system_prompt: Option<String>,
    temperature: Option<f32>,
    max_tokens: Option<u32>,
    request_timeout_secs: Option<u64>,
}

impl Default for AgentSettings {
    fn default() -> Self {
        Self {
            endpoint: std::env::var("DUET_HARNESS_ENDPOINT")
                .ok()
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| DEFAULT_ENDPOINT.to_string()),
            api_key: std::env::var("DUET_HARNESS_API_KEY")
                .ok()
                .filter(|value| !value.trim().is_empty()),
            model: std::env::var("DUET_HARNESS_MODEL")
                .ok()
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| DEFAULT_MODEL.to_string()),
            system_prompt: std::env::var("DUET_HARNESS_SYSTEM_PROMPT")
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .or_else(|| Some(DUET_AGENT_SYSTEM_PROMPT.to_string())),
            temperature: std::env::var("DUET_HARNESS_TEMPERATURE")
                .ok()
                .and_then(|value| value.parse::<f32>().ok()),
            max_tokens: std::env::var("DUET_HARNESS_MAX_TOKENS")
                .ok()
                .and_then(|value| value.parse::<u32>().ok()),
            request_timeout_secs: std::env::var("DUET_HARNESS_TIMEOUT_SECS")
                .ok()
                .and_then(|value| value.parse::<u64>().ok()),
        }
    }
}

static DEFAULT_SETTINGS: Lazy<Mutex<AgentSettings>> =
    Lazy::new(|| Mutex::new(AgentSettings::default()));

/// Minimal OpenAI-compatible harness entity.
pub struct HarnessAgent {
    settings: AgentSettings,
    exchanges: Mutex<Vec<AgentExchange>>,
}

impl HarnessAgent {
    /// Create a new agent using shared defaults.
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
        let client = build_client(settings.request_timeout_secs)
            .map_err(|err| format!("failed to construct HTTP client: {err}"))?;
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        if let Some(key) = settings.api_key.as_ref() {
            let value = format!("Bearer {key}");
            let header_value = HeaderValue::from_str(&value)
                .map_err(|err| format!("invalid API key header: {err}"))?;
            headers.insert(AUTHORIZATION, header_value);
        }

        let messages = build_messages(settings.system_prompt.as_deref(), prompt);
        let mut body = json!({
            "model": settings.model,
            "messages": messages,
        });

        if let Some(temp) = settings.temperature {
            body["temperature"] = json!(temp);
        }
        if let Some(max_tokens) = settings.max_tokens {
            body["max_tokens"] = json!(max_tokens);
        }

        let response = client
            .post(&settings.endpoint)
            .headers(headers)
            .json(&body)
            .send()
            .map_err(|err| format!("request to {} failed: {err}", settings.endpoint))?;

        if !response.status().is_success() {
            let status = response.status();
            let text = response
                .text()
                .unwrap_or_else(|_| "<failed to read error body>".to_string());
            return Err(format!(
                "endpoint {} returned {}: {}",
                settings.endpoint, status, text
            ));
        }

        let completion: ChatCompletion = response
            .json()
            .map_err(|err| format!("failed to parse completion payload: {err}"))?;

        extract_completion_text(&completion)
            .map(|text| text.trim().to_string())
            .ok_or_else(|| "completion payload missing response text".to_string())
    }

    fn parse_response(
        activation: &Activation,
        value: &preserves::IOValue,
    ) -> ActorResult<Option<(String, String, String, String)>> {
        let Some((agent_id, request_id, prompt, response, agent_kind)) =
            parse_response_fields(value)
        else {
            return Err(ActorError::InvalidActivation(
                "agent response must use agent-response label and include agent id, request, prompt, response, and agent kind"
                    .into(),
            ));
        };

        if let Some(current) = activation.current_entity_id() {
            if agent_id != current.to_string() {
                return Ok(None);
            }
        }

        Ok(Some((request_id, prompt, response, agent_kind)))
    }

    fn parse_request(
        activation: &Activation,
        value: &preserves::IOValue,
    ) -> ActorResult<Option<(String, String)>> {
        let record = record_with_label(value, REQUEST_LABEL).ok_or_else(|| {
            ActorError::InvalidActivation("agent request must use agent-request label".into())
        })?;

        if record.len() < 3 {
            return Err(ActorError::InvalidActivation(
                "agent request requires agent id, request id, and prompt".into(),
            ));
        }

        let agent_id = record.field_string(0).ok_or_else(|| {
            ActorError::InvalidActivation("agent request agent id must be string".into())
        })?;
        let request_id = record.field_string(1).ok_or_else(|| {
            ActorError::InvalidActivation("agent request id must be string".into())
        })?;

        let prompt = record
            .field_string(2)
            .ok_or_else(|| ActorError::InvalidActivation("agent prompt must be string".into()))?;

        if let Some(current) = activation.current_entity_id() {
            if agent_id != current.to_string() {
                return Ok(None);
            }
        }

        Ok(Some((request_id, prompt)))
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
        let agent_entity_id = activation
            .current_entity_id()
            .map(|id| id.to_string())
            .unwrap_or_default();
        activation.outputs.push(TurnOutput::ExternalRequest {
            request_id: request_uuid,
            service: HARNESS_KIND.to_string(),
            request: preserves::IOValue::record(
                preserves::IOValue::symbol(REQUEST_LABEL),
                vec![
                    preserves::IOValue::new(agent_entity_id.clone()),
                    preserves::IOValue::new(request_id.clone()),
                    preserves::IOValue::new(prompt.clone()),
                ],
            ),
        });

        let agent_kind = self.agent_kind().to_string();
        let settings_clone = settings.clone();

        if let Some(async_sender) = async_sender {
            let agent_id_for_response = agent_entity_id.clone();
            std::thread::spawn(move || {
                let response = match Self::execute_prompt(&settings_clone, &prompt) {
                    Ok(value) => value,
                    Err(err) => format!("Harness error: {err}"),
                };

                let timestamp = Utc::now().to_rfc3339();
                let response_record = preserves::IOValue::record(
                    preserves::IOValue::symbol(RESPONSE_LABEL),
                    response_fields(
                        agent_id_for_response,
                        request_id,
                        prompt,
                        response,
                        agent_kind,
                        timestamp,
                        Some(DEFAULT_ROLE),
                        None,
                    ),
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

        let agent_id = activation
            .current_entity_id()
            .map(|id| id.to_string())
            .unwrap_or_default();

        exchanges.push(AgentExchange {
            agent_id: agent_id.clone(),
            request_id: request_id.clone(),
            prompt: prompt.clone(),
            response: response.clone(),
        });
        drop(exchanges);

        let timestamp = Utc::now().to_rfc3339();

        let response_record = preserves::IOValue::record(
            preserves::IOValue::symbol(RESPONSE_LABEL),
            response_fields(
                agent_id,
                request_id,
                prompt,
                response,
                agent_kind,
                timestamp,
                Some(DEFAULT_ROLE),
                None,
            ),
        );

        activation.assert(Handle::new(), response_record);
        Ok(())
    }
}

impl AgentEntity for HarnessAgent {
    fn agent_kind(&self) -> &'static str {
        HARNESS_KIND
    }
}

impl Entity for HarnessAgent {
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
            Some(true) => match Self::parse_request(activation, payload) {
                Ok(Some((request_id, prompt))) => {
                    self.schedule_request(activation, request_id, prompt)
                }
                Ok(None) => Ok(()),
                Err(_) => Ok(()),
            },
            _ => match Self::parse_response(activation, payload) {
                Ok(Some((request_id, prompt, response, agent))) => {
                    self.record_response(activation, request_id, prompt, response, agent)
                }
                Ok(None) => Ok(()),
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
        if let Ok(Some((request_id, prompt))) = Self::parse_request(activation, value) {
            self.schedule_request(activation, request_id, prompt)?;
        }
        Ok(())
    }
}

/// Register the harness agent in the entity catalog.
pub fn register(catalog: &EntityCatalog) {
    catalog.register_hydratable(ENTITY_TYPE, |config| {
        let defaults = {
            let guard = DEFAULT_SETTINGS.lock().unwrap();
            guard.clone()
        };
        let settings = settings_from_config(config).unwrap_or(defaults);
        Ok(HarnessAgent::with_settings(settings))
    });
}

impl HydratableEntity for HarnessAgent {
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

fn settings_from_config(value: &preserves::IOValue) -> Option<AgentSettings> {
    let record = record_with_label(value, "noface-config")?;

    let mut settings = AgentSettings::default();

    if record.len() > 0 {
        if let Some(endpoint) = record.field_string(0) {
            let trimmed = endpoint.trim();
            if !trimmed.is_empty() {
                settings.endpoint = trimmed.to_string();
            }
        }
    }

    if record.len() > 1 {
        if let Some(model) = record.field_string(1) {
            let trimmed = model.trim();
            if !trimmed.is_empty() {
                settings.model = trimmed.to_string();
            }
        }
    }

    if record.len() > 2 {
        if let Some(system_prompt) = record.field_string(2) {
            let trimmed = system_prompt.trim();
            if !trimmed.is_empty() {
                settings.system_prompt = Some(trimmed.to_string());
            } else {
                settings.system_prompt = None;
            }
        }
    }

    if record.len() > 3 {
        if let Some(api_key) = record.field_string(3) {
            let trimmed = api_key.trim();
            if !trimmed.is_empty() {
                settings.api_key = Some(trimmed.to_string());
            } else {
                settings.api_key = None;
            }
        }
    }

    if record.len() > 4 {
        if let Some(temperature) = record.field_string(4) {
            settings.temperature = temperature.parse::<f32>().ok();
        }
    }

    if record.len() > 5 {
        if let Some(max_tokens) = record.field_string(5) {
            settings.max_tokens = max_tokens.parse::<u32>().ok();
        }
    }

    Some(settings)
}

fn build_client(timeout_secs: Option<u64>) -> Result<Client, reqwest::Error> {
    let mut builder = Client::builder();
    if let Some(secs) = timeout_secs {
        builder = builder.timeout(std::time::Duration::from_secs(secs));
    }
    builder.build()
}

fn build_messages(system_prompt: Option<&str>, user_prompt: &str) -> Vec<serde_json::Value> {
    let mut messages = Vec::new();
    if let Some(prompt) = system_prompt {
        messages.push(json!({ "role": "system", "content": prompt }));
    }
    messages.push(json!({ "role": "user", "content": user_prompt }));
    messages
}

fn extract_completion_text(completion: &ChatCompletion) -> Option<String> {
    completion.choices.iter().find_map(|choice| {
        if let Some(message) = choice.message.as_ref() {
            if let Some(content) = message.content.as_ref() {
                return Some(content.clone());
            }
        }
        if let Some(text) = choice.text.as_ref() {
            return Some(text.clone());
        }
        None
    })
}

#[derive(Debug, Deserialize)]
struct ChatCompletion {
    choices: Vec<Choice>,
}

#[derive(Debug, Deserialize)]
struct Choice {
    message: Option<Message>,
    #[serde(default)]
    text: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
struct Message {
    #[serde(default)]
    role: Option<String>,
    #[serde(default)]
    content: Option<String>,
}
