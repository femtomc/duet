//! Common agent abstractions.
use preserves::IOValue;
use serde::{Deserialize, Serialize};

use crate::runtime::actor::Entity;
use crate::util::io_value::record_with_label;

pub mod claude;
pub mod codex;
pub mod harness;

/// System instructions shared by all Duet-managed agents.
pub const DUET_AGENT_SYSTEM_PROMPT: &str = r#"
You are the coding agent embedded in the Duet runtime.

Environment constraints:
- The workspace is mediated by Duet's capabilities; you never run shell commands or edit files yourself.
- Ask explicitly for information you need (for example: "Please show me READ path/to/file.rs" or "List directory src/").
- When proposing changes, describe precise edits or provide unified diffs so the runtime can apply them deterministically.
- Make small, reviewable steps and confirm assumptions before large refactors.
- Highlight tests or checks the user should run after your changes.

Response style:
- Work incrementally, thinking aloud when the plan is non-trivial.
- Use Markdown with clear sections. Finish with a brief summary and next steps for the user.
- If you are missing context, ask for it instead of guessing.
"#;

/// Label used for agent request records.
pub const REQUEST_LABEL: &str = "agent-request";
/// Label used for agent response records.
pub const RESPONSE_LABEL: &str = "agent-response";

/// Represents a single exchange between the runtime and an agent.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentExchange {
    /// Agent entity identifier that produced the response.
    pub agent_id: String,
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
    agent_id: String,
    request_id: String,
    prompt: String,
    response: String,
    agent_kind: String,
    timestamp: String,
    role: Option<&str>,
    tool: Option<&str>,
) -> Vec<IOValue> {
    let mut fields = vec![
        IOValue::new(agent_id),
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
pub fn parse_response_fields(value: &IOValue) -> Option<(String, String, String, String, String)> {
    let record = record_with_label(value, RESPONSE_LABEL)?;
    if record.len() < 5 {
        return None;
    }

    let agent_id = record.field_string(0)?;
    let request_id = record.field_string(1)?;
    let prompt = record.field_string(2)?;
    let response = record.field_string(3)?;
    let agent_kind = record.field_symbol(4).unwrap_or_default();

    Some((agent_id, request_id, prompt, response, agent_kind))
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
                        preserves::IOValue::new(exchange.agent_id.clone()),
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
            Some(view) if view.len() >= 4 => view,
            _ => continue,
        };

        let agent_id = exchange.field_string(0);
        let request_id = exchange.field_string(1);
        let prompt = exchange.field_string(2);
        let response = exchange.field_string(3);

        if let (Some(agent_id), Some(request_id), Some(prompt), Some(response)) =
            (agent_id, request_id, prompt, response)
        {
            exchanges.push(AgentExchange {
                agent_id,
                request_id,
                prompt,
                response,
            });
        }
    }
    exchanges
}
