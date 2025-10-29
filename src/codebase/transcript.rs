//! Helpers for working with agent transcripts.
//!
//! These functions derive conversation history from runtime assertions without
//! modifying core runtime state. They translate raw dataspace assertions into
//! higher-level transcript entries suitable for user interfaces.

use super::agent;
use crate::codebase;
use crate::runtime::control::{AssertionEventChunk, AssertionEventFilter, Control};
use crate::runtime::error::Result as RuntimeResult;
use crate::runtime::turn::{ActorId, BranchId, Handle, TurnId};
use chrono::{DateTime, Utc};
use codebase::AgentResponse;
use preserves::IOValue;
use std::time::Duration;

/// Snapshot describing the most recent transcript state we observed.
#[derive(Debug, Clone)]
pub struct TranscriptCursor {
    /// Branch where the conversation is taking place.
    pub branch: BranchId,
    /// Last turn we have processed on this branch.
    pub last_turn: TurnId,
    /// Actor hosting the agent entity (if known).
    pub actor: Option<ActorId>,
}

/// Materialised transcript entry for display.
#[derive(Debug, Clone)]
pub struct TranscriptEntry {
    /// Actor hosting the agent entity when the response was recorded.
    pub actor: ActorId,
    /// Dataspace handle associated with the response assertion.
    pub handle: Handle,
    /// Agent kind identifier (e.g., "claude-code").
    pub agent: String,
    /// Prompt supplied to the agent.
    pub prompt: String,
    /// Response emitted by the agent.
    pub response: String,
    /// Timestamp recorded for the response, if provided by the agent.
    pub response_timestamp: Option<DateTime<Utc>>,
}

/// Resolve transcript entries for a request, deriving an updated cursor.
pub fn transcript_entries(
    control: &Control,
    request_id: &str,
    cursor: Option<&TranscriptCursor>,
    branch_hint: Option<&BranchId>,
    limit: usize,
) -> RuntimeResult<(Vec<TranscriptEntry>, TranscriptCursor)> {
    let status = control.status()?;
    let branch = branch_hint
        .cloned()
        .or_else(|| cursor.map(|c| c.branch.clone()))
        .unwrap_or_else(|| status.active_branch.clone());

    let mut resolved_cursor = cursor.cloned().unwrap_or(TranscriptCursor {
        branch: branch.clone(),
        last_turn: status.head_turn.clone(),
        actor: None,
    });
    resolved_cursor.branch = branch.clone();
    resolved_cursor.last_turn = status.head_turn.clone();

    let mut entries = Vec::new();

    if let Some(actor_id) = resolved_cursor.actor.clone() {
        for (handle, value) in control.list_assertions_for_actor(&actor_id) {
            if !matches_request(&value, request_id) {
                continue;
            }
            if let Some(agent_resp) = parse_agent_response(&value) {
                entries.push(TranscriptEntry {
                    actor: actor_id.clone(),
                    handle,
                    agent: agent_resp.agent,
                    prompt: agent_resp.prompt,
                    response: agent_resp.response,
                    response_timestamp: agent_resp.timestamp,
                });
                if entries.len() >= limit {
                    break;
                }
            }
        }
    } else {
        for assertion in control.list_assertions(None) {
            if !matches_label(&assertion.value) {
                continue;
            }
            if !matches_request(&assertion.value, request_id) {
                continue;
            }

            if let Some(agent_resp) = parse_agent_response(&assertion.value) {
                resolved_cursor.actor.get_or_insert(assertion.actor.clone());
                entries.push(TranscriptEntry {
                    actor: assertion.actor.clone(),
                    handle: assertion.handle.clone(),
                    agent: agent_resp.agent,
                    prompt: agent_resp.prompt,
                    response: agent_resp.response,
                    response_timestamp: agent_resp.timestamp,
                });
                if entries.len() >= limit {
                    break;
                }
            }
        }
    }

    entries.sort_by(|a, b| {
        let time_order = a.response_timestamp.cmp(&b.response_timestamp);
        if time_order == std::cmp::Ordering::Equal {
            a.handle.0.cmp(&b.handle.0)
        } else {
            time_order
        }
    });

    if entries.len() > limit {
        entries.truncate(limit);
    }

    Ok((entries, resolved_cursor))
}

/// Tail transcript events using the runtime's journal iterator, returning an
/// updated cursor and the raw event chunk.
pub fn transcript_events(
    control: &mut Control,
    request_id: &str,
    cursor: Option<&TranscriptCursor>,
    branch_hint: Option<&BranchId>,
    since: Option<&TurnId>,
    limit: usize,
    wait: Option<Duration>,
) -> RuntimeResult<(TranscriptCursor, AssertionEventChunk)> {
    let status = control.status()?;
    let branch = branch_hint
        .cloned()
        .or_else(|| cursor.map(|c| c.branch.clone()))
        .unwrap_or_else(|| status.active_branch.clone());

    let since_turn = since
        .cloned()
        .or_else(|| cursor.map(|c| c.last_turn.clone()));

    let mut filter = AssertionEventFilter::inclusive();
    filter.label = Some(agent::claude::RESPONSE_LABEL.to_string());
    filter.request_id = Some(request_id.to_string());

    let chunk =
        control.assertion_events_since(&branch, since_turn.as_ref(), limit, filter, wait)?;

    let mut updated_cursor = cursor.cloned().unwrap_or(TranscriptCursor {
        branch: branch.clone(),
        last_turn: status.head_turn.clone(),
        actor: None,
    });
    updated_cursor.branch = branch;
    if let Some(next) = chunk.next_cursor.clone().or_else(|| chunk.head.clone()) {
        updated_cursor.last_turn = next;
    } else {
        updated_cursor.last_turn = status.head_turn.clone();
    }

    Ok((updated_cursor, chunk))
}

fn matches_label(value: &IOValue) -> bool {
    if !value.is_record() {
        return false;
    }

    value
        .label()
        .as_symbol()
        .map(|sym| sym.as_ref() == agent::claude::RESPONSE_LABEL)
        .unwrap_or(false)
}

fn matches_request(value: &IOValue, request_id: &str) -> bool {
    if !value.is_record() || value.len() == 0 {
        return false;
    }

    value
        .index(0)
        .as_string()
        .map(|s| s.as_ref() == request_id)
        .unwrap_or(false)
}

fn parse_agent_response(value: &IOValue) -> Option<AgentResponse> {
    codebase::parse_agent_response(value)
}
