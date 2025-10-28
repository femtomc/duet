//! Common agent abstractions.
use serde::{Deserialize, Serialize};

use crate::runtime::actor::Entity;

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
    if !value.is_record() {
        return Vec::new();
    }

    if value
        .label()
        .as_symbol()
        .map(|sym| sym.as_ref() == "history")
        != Some(true)
    {
        return Vec::new();
    }

    let mut exchanges = Vec::new();
    for i in 0..value.len() {
        let entry = value.index(i);
        if !entry.is_record() {
            continue;
        }
        if entry
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref() == "exchange")
            != Some(true)
        {
            continue;
        }
        if entry.len() < 3 {
            continue;
        }
        let request_id = entry.index(0).as_string().map(|s| s.to_string());
        let prompt = entry.index(1).as_string().map(|s| s.to_string());
        let response = entry.index(2).as_string().map(|s| s.to_string());
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
