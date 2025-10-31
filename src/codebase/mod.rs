//! Built-in entities and behaviours shipped with the Duet runtime.
//!
//! The `codebase` module provides foundational entities that power
//! the `codebased` daemon.  It currently includes:
//!   * `workspace` – publishes a causal view of the filesystem and
//!     issues capabilities for reading/modifying files.
//!   * `echo` / `counter` – small reference implementations used by
//!     tests/examples until richer catalogues arrive.

use std::convert::TryFrom;
use std::path::Path;
use std::sync::{Mutex, Once};

use chrono::{DateTime, Utc};
use preserves::ValueImpl;
use serde::Serialize;

use crate::interpreter::entity::InterpreterEntity;
use crate::runtime::actor::{Activation, Entity, HydratableEntity};
use crate::runtime::control::Control;
use crate::runtime::error::{ActorError, ActorResult, Result as RuntimeResult, RuntimeError};
use crate::runtime::pattern::Pattern;
use crate::runtime::registry::EntityCatalog;
use crate::runtime::turn::{ActorId, BranchId, FacetId, Handle, TurnId};
use crate::util::io_value::record_with_label;

pub mod agent;
pub mod transcript;
pub mod workspace;

static INIT: Once = Once::new();

const WORKSPACE_READ_KIND: &str = "workspace/read";
const WORKSPACE_WRITE_KIND: &str = "workspace/write";
const WORKSPACE_READ_MSG: &str = "workspace-read";
const WORKSPACE_WRITE_MSG: &str = "workspace-write";

/// Register all built-in entities provided by this crate.
///
/// The call is idempotent; it is safe to invoke multiple times.
pub fn register_codebase_entities() {
    INIT.call_once(|| {
        let catalog = EntityCatalog::global();

        workspace::register(catalog);
        InterpreterEntity::register(catalog);
        agent::claude::register(catalog);
        agent::codex::register(catalog);
        agent::harness::register(catalog);

        catalog.register("echo", |config| {
            let topic = config
                .as_string()
                .map(|s| s.to_string())
                .unwrap_or_else(|| "echo".to_string());
            Ok(Box::new(EchoEntity { topic }))
        });

        catalog.register_hydratable("counter", |config| {
            let initial = config
                .as_signed_integer()
                .and_then(|value| i64::try_from(value.as_ref()).ok())
                .unwrap_or(0);
            Ok(CounterEntity::new(initial))
        });
    });
}

struct EchoEntity {
    topic: String,
}

impl Entity for EchoEntity {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        let fact = preserves::IOValue::record(
            preserves::IOValue::symbol("echo"),
            vec![preserves::IOValue::new(self.topic.clone()), payload.clone()],
        );
        activation.assert(Handle::new(), fact);
        Ok(())
    }
}

struct CounterEntity {
    value: Mutex<i64>,
}

impl CounterEntity {
    fn new(initial: i64) -> Self {
        Self {
            value: Mutex::new(initial),
        }
    }
}

/// Handle to the workspace entity registered in this runtime.
#[derive(Debug, Clone)]
pub struct WorkspaceHandle {
    /// Unique identifier of the workspace entity instance.
    pub entity_id: uuid::Uuid,
    /// Actor hosting the workspace entity.
    pub actor: ActorId,
    /// Facet the workspace entity is attached to.
    pub facet: FacetId,
}

/// Materialised view of a workspace entry published in the dataspace.
#[derive(Debug, Clone, Serialize)]
pub struct WorkspaceEntry {
    /// Normalised workspace-relative path.
    pub path: String,
    /// Entry kind (`file`, `dir`, `symlink`, ...).
    pub kind: String,
    /// Logical size of the entry (bytes for files, 0 otherwise).
    pub size: i64,
    /// Optional last-modified timestamp (RFC3339 string).
    pub modified: Option<String>,
    /// Optional digest associated with the entry.
    pub digest: Option<String>,
}

/// Handle to a registered agent entity.
#[derive(Debug, Clone)]
pub struct AgentHandle {
    /// Unique identifier of the agent entity instance.
    pub entity_id: uuid::Uuid,
    /// Actor hosting the agent entity.
    pub actor: ActorId,
    /// Facet the agent entity is attached to.
    pub facet: FacetId,
    /// Agent kind identifier (e.g., "claude-code").
    pub kind: String,
}

/// Structured representation of an agent response.
#[derive(Debug, Clone, Serialize)]
pub struct AgentResponse {
    /// Agent entity identifier that produced the response.
    pub agent_id: String,
    /// Request identifier that triggered the response.
    pub request_id: String,
    /// Prompt supplied with the request.
    pub prompt: String,
    /// Response payload emitted by the agent.
    pub response: String,
    /// Agent kind identifier.
    pub agent: String,
    /// Role associated with the response (assistant, tool, etc.).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// Tool identifier associated with the response, if any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool: Option<String>,
    /// Timestamp when the response was recorded (if present).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<DateTime<Utc>>,
}

/// Metadata returned when an agent invocation is enqueued.
#[derive(Debug, Clone, Serialize)]
pub struct AgentInvocation {
    /// Prompt supplied with the request.
    pub prompt: String,
    /// Agent kind identifier.
    pub agent: String,
    /// Request identifier generated for correlation.
    pub request_id: String,
    /// Actor hosting the agent entity.
    pub actor: ActorId,
    /// Branch where the request turn executed.
    pub branch: BranchId,
    /// Turn that executed the request (if immediately processed).
    pub queued_turn: Option<TurnId>,
}

/// Ensure a workspace entity exists for the given root directory.
pub fn ensure_workspace_entity(
    control: &mut Control,
    root: &Path,
) -> RuntimeResult<WorkspaceHandle> {
    if let Some(handle) = workspace_handle(control) {
        return Ok(handle);
    }

    let actor = ActorId::new();
    let facet = FacetId::new();
    let config = preserves::IOValue::new(root.to_string_lossy().to_string());

    let entity_id = control.register_entity(
        actor.clone(),
        facet.clone(),
        "workspace".to_string(),
        config,
    )?;

    // Kick off initial scan so the dataspace is populated.
    control.send_message(
        actor.clone(),
        facet.clone(),
        preserves::IOValue::symbol("workspace-rescan"),
    )?;

    Ok(WorkspaceHandle {
        entity_id,
        actor,
        facet,
    })
}

/// Return the handle for the first registered workspace entity, if any.
pub fn workspace_handle(control: &Control) -> Option<WorkspaceHandle> {
    control.list_entities().into_iter().find_map(|entity| {
        if entity.entity_type == "workspace" {
            Some(WorkspaceHandle {
                entity_id: entity.id,
                actor: entity.actor,
                facet: entity.facet,
            })
        } else {
            None
        }
    })
}

/// Trigger a rescan of the workspace dataspace.
pub fn workspace_rescan(control: &mut Control, handle: &WorkspaceHandle) -> RuntimeResult<()> {
    control.send_message(
        handle.actor.clone(),
        handle.facet.clone(),
        preserves::IOValue::symbol("workspace-rescan"),
    )?;
    Ok(())
}

/// List workspace entries currently asserted in the dataspace.
pub fn list_workspace_entries(control: &Control, handle: &WorkspaceHandle) -> Vec<WorkspaceEntry> {
    control
        .list_assertions_for_actor(&handle.actor)
        .into_iter()
        .filter_map(|(_handle, value)| parse_workspace_entry(&value))
        .collect()
}

/// Read a workspace file via capability invocation.
pub fn read_file(
    control: &mut Control,
    handle: &WorkspaceHandle,
    rel_path: &str,
) -> RuntimeResult<String> {
    let cap = request_read_capability(control, handle, rel_path)?;
    let payload = preserves::IOValue::record(
        preserves::IOValue::symbol("workspace-read"),
        vec![preserves::IOValue::new(rel_path.to_string())],
    );
    let response = control.invoke_capability(cap, payload)?;
    response.as_string().map(|s| s.to_string()).ok_or_else(|| {
        RuntimeError::Actor(ActorError::InvalidActivation(
            "workspace read returned non-string".into(),
        ))
    })
}

/// Ensure a Claude Code agent entity exists for this runtime.
pub fn ensure_claude_agent(control: &mut Control) -> RuntimeResult<AgentHandle> {
    ensure_agent(
        control,
        agent::claude::ENTITY_TYPE,
        agent::claude::CLAUDE_KIND,
    )
}

/// Ensure a Codex agent entity exists for this runtime.
pub fn ensure_codex_agent(control: &mut Control) -> RuntimeResult<AgentHandle> {
    ensure_agent(control, agent::codex::ENTITY_TYPE, agent::codex::CODEX_KIND)
}

/// Ensure an OpenAI-harness agent entity exists for this runtime.
pub fn ensure_harness_agent(control: &mut Control) -> RuntimeResult<AgentHandle> {
    ensure_agent(
        control,
        agent::harness::ENTITY_TYPE,
        agent::harness::HARNESS_KIND,
    )
}

/// Enqueue a prompt for the Claude Code agent to process.
pub fn invoke_claude_agent(
    control: &mut Control,
    handle: &AgentHandle,
    prompt: &str,
) -> RuntimeResult<AgentInvocation> {
    invoke_agent(control, handle, prompt)
}

/// Enqueue a prompt for the Codex agent to process.
pub fn invoke_codex_agent(
    control: &mut Control,
    handle: &AgentHandle,
    prompt: &str,
) -> RuntimeResult<AgentInvocation> {
    invoke_agent(control, handle, prompt)
}

/// Enqueue a prompt for the OpenAI harness agent to process.
pub fn invoke_harness_agent(
    control: &mut Control,
    handle: &AgentHandle,
    prompt: &str,
) -> RuntimeResult<AgentInvocation> {
    invoke_agent(control, handle, prompt)
}

fn ensure_agent(
    control: &mut Control,
    entity_type: &str,
    kind: &str,
) -> RuntimeResult<AgentHandle> {
    if let Some(handle) = agent_handle(control, kind) {
        return Ok(handle);
    }

    let actor = ActorId::new();
    let facet = FacetId::new();
    let entity_id = control.register_entity(
        actor.clone(),
        facet.clone(),
        entity_type.to_string(),
        preserves::IOValue::symbol("default"),
    )?;

    let pattern = Pattern {
        id: uuid::Uuid::new_v4(),
        pattern: preserves::IOValue::record(
            preserves::IOValue::symbol(agent::REQUEST_LABEL),
            vec![
                preserves::IOValue::new(entity_id.to_string()),
                preserves::IOValue::symbol("<_>"),
                preserves::IOValue::symbol("<_>"),
            ],
        ),
        facet: facet.clone(),
    };
    control
        .register_pattern_for_entity(entity_id, pattern)
        .map_err(RuntimeError::from)?;

    Ok(AgentHandle {
        entity_id,
        actor,
        facet,
        kind: kind.to_string(),
    })
}

fn invoke_agent(
    control: &mut Control,
    handle: &AgentHandle,
    prompt: &str,
) -> RuntimeResult<AgentInvocation> {
    let request_id = uuid::Uuid::new_v4().to_string();
    let message = preserves::IOValue::record(
        preserves::IOValue::symbol(agent::REQUEST_LABEL),
        vec![
            preserves::IOValue::new(handle.entity_id.to_string()),
            preserves::IOValue::new(request_id.clone()),
            preserves::IOValue::new(prompt.to_string()),
        ],
    );

    let branch = control.runtime().current_branch().clone();
    let turn_id = control.send_message(handle.actor.clone(), handle.facet.clone(), message)?;

    Ok(AgentInvocation {
        prompt: prompt.to_string(),
        agent: handle.kind.clone(),
        request_id,
        actor: handle.actor.clone(),
        branch,
        queued_turn: Some(turn_id),
    })
}

/// List all agent responses currently asserted for a given handle.
pub fn list_agent_responses(control: &Control, handle: &AgentHandle) -> Vec<AgentResponse> {
    control
        .list_assertions_for_actor(&handle.actor)
        .into_iter()
        .filter_map(|(_handle, value)| parse_agent_response(&value))
        .filter(|resp| resp.agent == handle.kind && resp.agent_id == handle.entity_id.to_string())
        .collect()
}

/// Write content to a workspace file via capability invocation.
pub fn write_file(
    control: &mut Control,
    handle: &WorkspaceHandle,
    rel_path: &str,
    content: &str,
) -> RuntimeResult<()> {
    let cap = request_write_capability(control, handle, rel_path)?;
    let payload = preserves::IOValue::record(
        preserves::IOValue::symbol("workspace-write"),
        vec![
            preserves::IOValue::new(rel_path.to_string()),
            preserves::IOValue::new(content.to_string()),
        ],
    );
    let response = control.invoke_capability(cap, payload)?;
    if response
        .as_symbol()
        .map(|sym| sym.as_ref() == "ok")
        .unwrap_or(false)
    {
        Ok(())
    } else {
        Err(RuntimeError::Actor(ActorError::InvalidActivation(
            "workspace write did not return ok".into(),
        )))
    }
}

fn parse_workspace_entry(value: &preserves::IOValue) -> Option<WorkspaceEntry> {
    let record = record_with_label(value, "workspace-entry")?;
    if record.len() < 3 {
        return None;
    }

    let path = record.field_string(0)?;
    let kind = record.field_symbol(1)?;
    let size = record
        .field(2)
        .as_signed_integer()
        .and_then(|s| i64::try_from(s.as_ref()).ok())
        .unwrap_or(0);

    let modified = if record.len() > 3 {
        let modified_value = record.field(3);
        if modified_value.as_symbol().is_some() {
            None
        } else {
            modified_value.as_string().map(|s| s.to_string())
        }
    } else {
        None
    };

    let digest = if record.len() > 4 {
        record.field_string(4)
    } else {
        None
    };

    Some(WorkspaceEntry {
        path,
        kind,
        size,
        modified,
        digest,
    })
}

fn agent_handle(control: &Control, kind: &str) -> Option<AgentHandle> {
    let target_type = agent::entity_type_for_kind(kind)?;

    control.list_entities().into_iter().find_map(|entity| {
        if entity.entity_type == target_type {
            Some(AgentHandle {
                entity_id: entity.id,
                actor: entity.actor,
                facet: entity.facet,
                kind: kind.to_string(),
            })
        } else {
            None
        }
    })
}

/// Attempt to interpret a preserves value as an agent response record.
/// Attempt to interpret a preserves payload as an agent response.
pub fn parse_agent_response(value: &preserves::IOValue) -> Option<AgentResponse> {
    let record = record_with_label(value, agent::RESPONSE_LABEL)?;
    if record.len() < 5 {
        return None;
    }

    let agent_id = record.field_string(0)?;
    let request_id = record.field_string(1)?;
    let prompt = record.field_string(2)?;
    let response = record.field_string(3)?;
    let agent_kind = record.field_symbol(4).unwrap_or_default();

    let timestamp = if record.len() > 5 {
        record.field_timestamp(5)
    } else {
        None
    };

    let role = if record.len() > 6 {
        record.field_symbol(6).or_else(|| record.field_string(6))
    } else {
        None
    };

    let tool = if record.len() > 7 {
        record.field_string(7)
    } else {
        None
    };

    Some(AgentResponse {
        agent_id,
        request_id,
        prompt,
        response,
        agent: agent_kind,
        role,
        tool,
        timestamp,
    })
}

fn request_read_capability(
    control: &mut Control,
    handle: &WorkspaceHandle,
    rel_path: &str,
) -> RuntimeResult<uuid::Uuid> {
    request_capability(
        control,
        handle,
        rel_path,
        WORKSPACE_READ_KIND,
        WORKSPACE_READ_MSG,
    )
}

fn request_write_capability(
    control: &mut Control,
    handle: &WorkspaceHandle,
    rel_path: &str,
) -> RuntimeResult<uuid::Uuid> {
    request_capability(
        control,
        handle,
        rel_path,
        WORKSPACE_WRITE_KIND,
        WORKSPACE_WRITE_MSG,
    )
}

fn request_capability(
    control: &mut Control,
    handle: &WorkspaceHandle,
    rel_path: &str,
    kind: &str,
    label: &str,
) -> RuntimeResult<uuid::Uuid> {
    let request = preserves::IOValue::record(
        preserves::IOValue::symbol(label.to_string()),
        vec![preserves::IOValue::new(rel_path.to_string())],
    );
    control.send_message(handle.actor.clone(), handle.facet.clone(), request)?;

    find_capability(control, &handle.actor, kind, rel_path).ok_or_else(|| {
        RuntimeError::Actor(ActorError::InvalidActivation(format!(
            "workspace capability {kind} for {rel_path} not granted",
        )))
    })
}

fn find_capability(
    control: &Control,
    actor: &ActorId,
    kind: &str,
    rel_path: &str,
) -> Option<uuid::Uuid> {
    control
        .list_capabilities_for_actor(actor)
        .into_iter()
        .filter(|cap| cap.kind == kind)
        .find(|cap| attenuation_matches(&cap.attenuation, rel_path))
        .map(|cap| cap.id)
}

fn attenuation_matches(attenuation: &[preserves::IOValue], rel_path: &str) -> bool {
    attenuation
        .first()
        .and_then(|value| value.as_string())
        .map(|s| s.as_ref() == rel_path)
        .unwrap_or(false)
}

impl Entity for CounterEntity {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        let delta = payload
            .as_signed_integer()
            .and_then(|value| i64::try_from(value.as_ref()).ok())
            .unwrap_or(1);

        let mut guard = self.value.lock().unwrap();
        *guard += delta;

        let fact = preserves::IOValue::record(
            preserves::IOValue::symbol("counter"),
            vec![preserves::IOValue::new(*guard)],
        );
        activation.assert(Handle::new(), fact);
        Ok(())
    }
}

impl HydratableEntity for CounterEntity {
    fn snapshot_state(&self) -> preserves::IOValue {
        let value = *self.value.lock().unwrap();
        preserves::IOValue::new(value)
    }

    fn restore_state(&mut self, state: &preserves::IOValue) -> ActorResult<()> {
        let value = state
            .as_signed_integer()
            .and_then(|v| i64::try_from(v.as_ref()).ok())
            .ok_or_else(|| {
                ActorError::InvalidActivation("counter state must be an integer".into())
            })?;
        *self.value.lock().unwrap() = value;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registers_entities_once() {
        register_codebase_entities();
        register_codebase_entities();

        let snapshot = EntityCatalog::global().snapshot();
        assert!(snapshot.has_type("echo"));
        assert!(snapshot.has_type("counter"));
        assert!(snapshot.has_type("workspace"));
    }
}
