//! Common agent abstractions.
use preserves::{IOValue, ValueImpl};
use serde::{Deserialize, Serialize};

use crate::runtime::actor::Entity;
use crate::util::io_value::record_with_label;

pub mod claude;
pub mod codex;
pub mod harness;

/// Label used for agent request records.
pub const REQUEST_LABEL: &str = "agent-request";
/// Label used for agent response records.
pub const RESPONSE_LABEL: &str = "agent-response";

/// Represents a single exchange between the runtime and an agent.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentExchange {
    /// Request identifier.
    pub request_id: String,
    /// Prompt delivered to the agent.
    pub prompt: String,
    /// Response synthesized by the agent.
    pub response: String,
}

/// Trait implemented by agent entities to expose metadata.
pub trait AgentEntity: Entity {
    /// Returns the agent kind identifier (e.g., "claude-code").
    fn agent_kind(&self) -> &'static str;
}

/// Resolve an entity type identifier for a given agent kind.
pub fn entity_type_for_kind(kind: &str) -> Option<&'static str> {
    match kind {
        claude::CLAUDE_KIND => Some(claude::ENTITY_TYPE),
        codex::CODEX_KIND => Some(codex::ENTITY_TYPE),
        harness::HARNESS_KIND => Some(harness::ENTITY_TYPE),
        _ => None,
    }
}

/// Shared helper to convert response data into a preserves record payload.
pub fn response_fields(
    request_id: String,
    prompt: String,
    response: String,
    agent_kind: String,
    timestamp: String,
    role: Option<&str>,
    tool: Option<&str>,
) -> Vec<IOValue> {
    let mut fields = vec![
        IOValue::new(request_id),
        IOValue::new(prompt),
        IOValue::new(response),
        IOValue::symbol(agent_kind),
        IOValue::new(timestamp),
    ];

    if let Some(role) = role {
        fields.push(IOValue::symbol(role.to_string()));
    }

    if let Some(tool) = tool {
        fields.push(IOValue::new(tool.to_string()));
    }

    fields
}

/// Attempt to parse response fields from a preserves value.
pub fn parse_response_fields(value: &IOValue) -> Option<(String, String, String, String)> {
    let record = record_with_label(value, RESPONSE_LABEL)?;
    if record.len() < 4 {
        return None;
    }

    let request_id = record.field_string(0)?;
    let prompt = record.field_string(1)?;
    let response = record.field_string(2)?;
    let agent_kind = record.field_symbol(3).unwrap_or_default();

    Some((request_id, prompt, response, agent_kind))
}

/// Convenience helper for serializing a vector of exchanges.
pub fn exchanges_to_preserves(exchanges: &[AgentExchange]) -> preserves::IOValue {
    preserves::IOValue::record(
        preserves::IOValue::symbol("history"),
        exchanges
            .iter()
            .map(|exchange| {
                preserves::IOValue::record(
                    preserves::IOValue::symbol("exchange"),
                    vec![
                        preserves::IOValue::new(exchange.request_id.clone()),
                        preserves::IOValue::new(exchange.prompt.clone()),
                        preserves::IOValue::new(exchange.response.clone()),
                    ],
                )
            })
            .collect(),
    )
}

/// Convenience helper for deserializing exchanges.
pub fn exchanges_from_preserves(value: &preserves::IOValue) -> Vec<AgentExchange> {
    let history = match record_with_label(value, "history") {
        Some(view) => view,
        None => return Vec::new(),
    };

    let mut exchanges = Vec::new();
    for index in 0..history.len() {
        let entry = history.field(index);
        let exchange = match record_with_label(&entry, "exchange") {
            Some(view) if view.len() >= 3 => view,
            _ => continue,
        };

        let request_id = exchange.field_string(0);
        let prompt = exchange.field_string(1);
        let response = exchange.field_string(2);

        if let (Some(request_id), Some(prompt), Some(response)) = (request_id, prompt, response) {
            exchanges.push(AgentExchange {
                request_id,
                prompt,
                response,
            });
        }
    }
    exchanges
}
