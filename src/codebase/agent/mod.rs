//! Common agent abstractions.
use serde::{Deserialize, Serialize};

use crate::runtime::actor::Entity;
use crate::util::io_value::record_with_label;

pub mod claude;

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
