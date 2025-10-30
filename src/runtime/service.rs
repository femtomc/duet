//! NDJSON control-plane service for the Duet runtime.
//!
//! This module exposes a small dispatcher that translates newline-delimited
//! JSON commands into calls on the high-level [`Control`] facade. It backs the
//! `codebased` command-line daemon and is intentionally conservative: commands are
//! processed sequentially, and unsupported operations return structured errors.

use super::control::{AssertionEventAction, AssertionEventFilter, Control};
use super::error::{CapabilityError, RuntimeError};
use super::turn::{ActorId, BranchId, FacetId, TurnId};
use crate::PROTOCOL_VERSION;
use crate::codebase::{self, transcript};
use crate::interpreter::protocol::{
    DefinitionRecord, InstanceProgress, InstanceRecord, InstanceStatus, ProgramRef,
    RUN_MESSAGE_LABEL, WaitStatus,
};
use crate::runtime::pattern::Pattern;
use crate::runtime::reaction::{ReactionDefinition, ReactionEffect, ReactionValue};
use crate::util::io_value::{as_record, io_value_summary, io_value_to_json};
use preserves::IOValue;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::io::{self, BufRead, Write};
use std::time::Duration;
use uuid::Uuid;

/// Service entry point: wraps a [`Control`] instance and writes responses to a writer.
pub struct Service {
    control: Control,
    pending_requests: HashMap<String, transcript::TranscriptCursor>,
}

impl Service {
    /// Create a new service wrapper around the provided control interface.
    pub fn new(control: Control) -> Self {
        Self {
            control,
            pending_requests: HashMap::new(),
        }
    }

    /// Process a single connection by consuming requests from the reader and writing responses.
    pub fn handle<R: BufRead, W: Write>(&mut self, reader: R, writer: W) -> io::Result<()> {
        let mut session = Session::new(&mut self.control, &mut self.pending_requests, writer);
        session.run(reader)
    }
}

struct Session<'a, W: Write> {
    control: &'a mut Control,
    pending_requests: &'a mut HashMap<String, transcript::TranscriptCursor>,
    writer: W,
    handshake_completed: bool,
    interpreter: Option<InterpreterBinding>,
}

#[derive(Clone)]
struct InterpreterBinding {
    actor: ActorId,
    facet: FacetId,
}

impl<'a, W: Write> Session<'a, W> {
    fn new(
        control: &'a mut Control,
        pending_requests: &'a mut HashMap<String, transcript::TranscriptCursor>,
        writer: W,
    ) -> Self {
        Self {
            control,
            pending_requests,
            writer,
            handshake_completed: false,
            interpreter: None,
        }
    }

    fn describe_actor(
        &mut self,
        actor: &ActorId,
        cache: &mut HashMap<ActorId, Value>,
    ) -> Option<Value> {
        if let Some(existing) = cache.get(actor) {
            return Some(existing.clone());
        }

        let entities = self.control.list_entities_for_actor(actor);
        let mut entity_types: Vec<String> =
            entities.iter().map(|e| e.entity_type.clone()).collect();
        entity_types.sort();
        entity_types.dedup();

        let actor_str = actor.to_string();
        let summary = json!({
            "id": actor_str,
            "short_id": short_id(&actor_str),
            "entity_types": entity_types,
            "entity_count": entities.len(),
        });

        cache.insert(actor.clone(), summary.clone());
        Some(summary)
    }

    fn run<R: BufRead>(&mut self, reader: R) -> io::Result<()> {
        for line in reader.lines() {
            let line = line?;
            if line.trim().is_empty() {
                continue;
            }

            let envelope: Result<RequestEnvelope, _> = serde_json::from_str(&line);
            match envelope {
                Ok(request) => {
                    let response = self.handle_request(request);
                    self.write_response(response)?;
                }
                Err(err) => {
                    let response = ResponseEnvelope::from_error(
                        Value::Null,
                        ServiceError::Parse(err.to_string()),
                    );
                    self.write_response(response)?;
                }
            }
        }

        Ok(())
    }

    fn write_response(&mut self, envelope: ResponseEnvelope) -> io::Result<()> {
        serde_json::to_writer(&mut self.writer, &envelope)?;
        self.writer.write_all(b"\n")?;
        self.writer.flush()
    }

    fn handle_request(&mut self, request: RequestEnvelope) -> ResponseEnvelope {
        let result = match self.dispatch(&request.command, &request.params) {
            Ok(value) => Ok(value),
            Err(err) => Err(err),
        };

        match result {
            Ok(value) => ResponseEnvelope::success(request.id, value),
            Err(err) => ResponseEnvelope::from_error(request.id, err),
        }
    }

    fn dispatch(&mut self, command: &str, params: &Value) -> Result<Value, ServiceError> {
        match command {
            "handshake" => self.cmd_handshake(params),
            "status" => self.cmd_status(params),
            "list_branches" => self.cmd_list_branches(),
            "history" => self.cmd_history(params),
            "step" => self.cmd_step(params),
            "goto" => self.cmd_goto(params),
            "back" => self.cmd_back(params),
            "send_message" => self.cmd_send_message(params),
            "fork" => self.cmd_fork(params),
            "merge" => self.cmd_merge(params),
            "register_entity" => self.cmd_register_entity(params),
            "list_entities" => self.cmd_list_entities(params),
            "list_capabilities" => self.cmd_list_capabilities(params),
            "workspace_entries" => self.cmd_workspace_entries(),
            "workspace_rescan" => self.cmd_workspace_rescan(),
            "workspace_read" => self.cmd_workspace_read(params),
            "workspace_write" => self.cmd_workspace_write(params),
            "agent_invoke" => self.cmd_agent_invoke(params),
            "agent_responses" => self.cmd_agent_responses(params),
            "transcript_show" => self.cmd_transcript_show(params),
            "transcript_tail" => self.cmd_transcript_tail(params),
            "workflow_list" => self.cmd_workflow_list(params),
            "workflow_start" => self.cmd_workflow_start(params),
            "reaction_register" => self.cmd_reaction_register(params),
            "reaction_unregister" => self.cmd_reaction_unregister(params),
            "reaction_list" => self.cmd_reaction_list(),
            "invoke_capability" => self.cmd_invoke_capability(params),
            "dataspace_assertions" => self.cmd_dataspace_assertions(params),
            "dataspace_events" => self.cmd_dataspace_events(params),
            other => Err(ServiceError::Unsupported(other.to_string())),
        }
    }

    fn cmd_handshake(&mut self, params: &Value) -> Result<Value, ServiceError> {
        let client = params
            .get("client")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("client"))?;

        let requested = params
            .get("protocol_version")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("protocol_version"))?;

        if requested != PROTOCOL_VERSION {
            return Err(ServiceError::Protocol(format!(
                "unsupported protocol version: expected {}, got {}",
                PROTOCOL_VERSION, requested
            )));
        }

        self.handshake_completed = true;

        Ok(json!({
            "protocol_version": PROTOCOL_VERSION,
            "runtime": {
                "version": crate::VERSION,
                "client": client,
                "features": [
                    "status",
                    "history",
                    "time_travel",
                    "branching",
                    "entity_persistence",
                    "capability_inspection",
                    "dataspace_inspection",
                    "dataspace_events",
                    "reactions",
                    "workflow_interpreter"
                ]
            }
        }))
    }

    fn ensure_handshake(&self) -> Result<(), ServiceError> {
        if self.handshake_completed {
            Ok(())
        } else {
            Err(ServiceError::Protocol(
                "handshake required before issuing commands".into(),
            ))
        }
    }

    fn ensure_interpreter_binding(&mut self) -> Result<InterpreterBinding, ServiceError> {
        if let Some(binding) = &self.interpreter {
            return Ok(binding.clone());
        }

        let binding = if let Some(info) = self
            .control
            .list_entities()
            .into_iter()
            .find(|info| info.entity_type == "interpreter")
        {
            InterpreterBinding {
                actor: info.actor,
                facet: info.facet,
            }
        } else {
            let actor = ActorId::new();
            let facet = FacetId::new();
            self.control
                .register_entity(
                    actor.clone(),
                    facet.clone(),
                    "interpreter".to_string(),
                    IOValue::symbol("default"),
                )
                .map_err(ServiceError::from)?;

            InterpreterBinding { actor, facet }
        };

        self.interpreter = Some(binding.clone());
        Ok(binding)
    }

    fn collect_interpreter_state(
        &mut self,
        binding: &InterpreterBinding,
    ) -> (Vec<DefinitionRecord>, Vec<InstanceRecord>) {
        let assertions = self.control.list_assertions_for_actor(&binding.actor);

        let mut definitions: HashMap<String, DefinitionRecord> = HashMap::new();
        let mut instances: HashMap<String, InstanceRecord> = HashMap::new();

        for (_, value) in assertions {
            if let Some(definition) = DefinitionRecord::parse(&value) {
                definitions.insert(definition.definition_id.clone(), definition);
                continue;
            }

            if let Some(instance) = InstanceRecord::parse(&value) {
                instances.insert(instance.instance_id.clone(), instance);
            }
        }

        let mut definition_list: Vec<_> = definitions.into_values().collect();
        definition_list.sort_by(|a, b| a.program_name.cmp(&b.program_name));

        let mut instance_list: Vec<_> = instances.into_values().collect();
        instance_list.sort_by(|a, b| a.instance_id.cmp(&b.instance_id));

        (definition_list, instance_list)
    }

    fn interpreter_instance_ids(&mut self, binding: &InterpreterBinding) -> HashSet<String> {
        self.collect_interpreter_state(binding)
            .1
            .into_iter()
            .map(|record| record.instance_id)
            .collect()
    }

    fn cmd_status(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        if let Some(branch_name) = params.get("branch").and_then(Value::as_str) {
            self.switch_branch(branch_name)?;
        }

        let status = self.control.status().map_err(ServiceError::from)?;
        Ok(serde_json::to_value(status).unwrap_or_default())
    }

    fn cmd_list_branches(&mut self) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let branches = self.control.list_branches().map_err(ServiceError::from)?;
        Ok(json!({ "branches": branches }))
    }

    fn cmd_history(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let branch_name = params
            .get("branch")
            .and_then(Value::as_str)
            .unwrap_or("main");
        let start = params.get("start").and_then(Value::as_u64).unwrap_or(0) as usize;
        let limit = params.get("limit").and_then(Value::as_u64).unwrap_or(20) as usize;

        let branch = BranchId::new(branch_name);
        let history = self
            .control
            .history(&branch, start, limit)
            .map_err(ServiceError::from)?;

        Ok(json!({ "turns": history }))
    }

    fn cmd_step(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        if let Some(branch_name) = params.get("branch").and_then(Value::as_str) {
            self.switch_branch(branch_name)?;
        }

        let count = params.get("count").and_then(Value::as_u64).unwrap_or(1) as usize;
        let turns = self.control.step(count).map_err(ServiceError::from)?;
        Ok(json!({ "executed": turns }))
    }

    fn cmd_goto(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        if let Some(branch_name) = params.get("branch").and_then(Value::as_str) {
            self.switch_branch(branch_name)?;
        }

        let turn_id_str = params
            .get("turn_id")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("turn_id"))?;

        let turn_id = TurnId::new(turn_id_str.to_string());
        self.control
            .goto(turn_id.clone())
            .map_err(ServiceError::from)?;
        Ok(json!({ "head": turn_id }))
    }

    fn cmd_back(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        if let Some(branch_name) = params.get("branch").and_then(Value::as_str) {
            self.switch_branch(branch_name)?;
        }

        let count = params.get("count").and_then(Value::as_u64).unwrap_or(1) as usize;
        let turn_id = self.control.back(count).map_err(ServiceError::from)?;
        Ok(json!({ "head": turn_id }))
    }

    fn cmd_send_message(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        if let Some(branch_name) = params.get("branch").and_then(Value::as_str) {
            self.switch_branch(branch_name)?;
        }

        let target = params
            .get("target")
            .ok_or_else(|| ServiceError::invalid_param("target"))?;

        let actor = target
            .get("actor")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("target.actor"))?;
        let facet = target
            .get("facet")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("target.facet"))?;

        let payload_value = params
            .get("payload")
            .ok_or_else(|| ServiceError::invalid_param("payload"))?;

        let actor_id = ActorId::from_uuid(parse_uuid(actor)?);
        let facet_id = FacetId::from_uuid(parse_uuid(facet)?);
        let payload = parse_preserves(payload_value)?;

        let turn_id = self
            .control
            .send_message(actor_id, facet_id, payload)
            .map_err(ServiceError::from)?;

        Ok(json!({ "queued_turn": turn_id }))
    }

    fn cmd_fork(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        let source = params
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("main");
        let new_branch = params
            .get("new_branch")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("new_branch"))?;

        let base_turn = params
            .get("from_turn")
            .and_then(Value::as_str)
            .map(|s| TurnId::new(s.to_string()));

        let branch = self
            .control
            .fork(BranchId::new(source), BranchId::new(new_branch), base_turn)
            .map_err(ServiceError::from)?;

        Ok(json!({ "branch": branch }))
    }

    fn cmd_merge(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        let source = params
            .get("source")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("source"))?;
        let target = params
            .get("target")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("target"))?;

        let report = self
            .control
            .merge(BranchId::new(source), BranchId::new(target))
            .map_err(ServiceError::from)?;

        Ok(serde_json::to_value(report).unwrap_or_default())
    }

    fn cmd_register_entity(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        let actor = params
            .get("actor")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("actor"))?;
        let facet = params
            .get("facet")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("facet"))?;
        let entity_type = params
            .get("entity_type")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("entity_type"))?;
        let config_value = params
            .get("config")
            .ok_or_else(|| ServiceError::invalid_param("config"))?;

        let actor_id = ActorId::from_uuid(parse_uuid(actor)?);
        let facet_id = FacetId::from_uuid(parse_uuid(facet)?);
        let config = parse_preserves(config_value)?;

        let id = self
            .control
            .register_entity(actor_id, facet_id, entity_type.to_string(), config)
            .map_err(ServiceError::from)?;

        Ok(json!({ "entity_id": id }))
    }

    fn cmd_list_entities(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        if let Some(actor_str) = params.get("actor").and_then(Value::as_str) {
            let actor = ActorId::from_uuid(parse_uuid(actor_str)?);
            let entities = self.control.list_entities_for_actor(&actor);
            Ok(json!({ "entities": entities }))
        } else {
            let entities = self.control.list_entities();
            Ok(json!({ "entities": entities }))
        }
    }

    fn cmd_list_capabilities(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        if let Some(actor_str) = params.get("actor").and_then(Value::as_str) {
            let actor = ActorId::from_uuid(parse_uuid(actor_str)?);
            let capabilities = self.control.list_capabilities_for_actor(&actor);
            Ok(json!({ "capabilities": capabilities }))
        } else {
            let capabilities = self.control.list_capabilities();
            Ok(json!({ "capabilities": capabilities }))
        }
    }

    fn cmd_workspace_entries(&mut self) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let handle = self
            .workspace_handle()
            .ok_or_else(|| ServiceError::Protocol("workspace entity not registered".into()))?;

        let entries = codebase::list_workspace_entries(&self.control, &handle);
        Ok(json!({ "entries": entries }))
    }

    fn cmd_workspace_rescan(&mut self) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let handle = self
            .workspace_handle()
            .ok_or_else(|| ServiceError::Protocol("workspace entity not registered".into()))?;

        codebase::workspace_rescan(&mut self.control, &handle).map_err(ServiceError::from)?;
        Ok(json!({ "status": "ok" }))
    }

    fn cmd_workspace_read(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let path = params
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("path"))?;

        let handle = self
            .workspace_handle()
            .ok_or_else(|| ServiceError::Protocol("workspace entity not registered".into()))?;

        let content =
            codebase::read_file(&mut self.control, &handle, path).map_err(ServiceError::from)?;
        Ok(json!({ "path": path, "content": content }))
    }

    fn cmd_workspace_write(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let path = params
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("path"))?;
        let content = params
            .get("content")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("content"))?;

        let handle = self
            .workspace_handle()
            .ok_or_else(|| ServiceError::Protocol("workspace entity not registered".into()))?;

        codebase::write_file(&mut self.control, &handle, path, content)
            .map_err(ServiceError::from)?;
        Ok(json!({ "status": "ok" }))
    }

    fn cmd_agent_invoke(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let prompt = params
            .get("prompt")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("prompt"))?;

        let handle = self.ensure_claude_handle()?;
        let invocation = codebase::invoke_claude_agent(&mut self.control, &handle, prompt)
            .map_err(ServiceError::from)?;

        let (queued_turn_value, last_turn_for_cursor) = match invocation.queued_turn.clone() {
            Some(turn) => (Value::from(turn.to_string()), turn),
            None => {
                let status = self.control.status().map_err(ServiceError::from)?;
                (Value::Null, status.head_turn)
            }
        };

        let branch_string = invocation.branch.to_string();

        self.pending_requests.insert(
            invocation.request_id.clone(),
            transcript::TranscriptCursor {
                branch: invocation.branch.clone(),
                last_turn: last_turn_for_cursor,
                actor: Some(invocation.actor.clone()),
            },
        );

        Ok(json!({
            "agent": invocation.agent,
            "request_id": invocation.request_id,
            "prompt": invocation.prompt,
            "actor": invocation.actor.to_string(),
            "branch": branch_string,
            "queued_turn": queued_turn_value,
        }))
    }

    fn cmd_agent_responses(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let total_wait_ms = params
            .get("wait_ms")
            .and_then(Value::as_u64)
            .unwrap_or(1000);

        let request_filter = params
            .get("request_id")
            .and_then(Value::as_str)
            .map(|s| s.to_string());

        let limit = params
            .get("limit")
            .and_then(Value::as_u64)
            .map(|v| v as usize);

        let handle = self.ensure_claude_handle()?;
        let status = self.control.status().map_err(ServiceError::from)?;

        if total_wait_ms > 0 {
            let timeout = Duration::from_millis(total_wait_ms);

            if let Some(req_id) = request_filter.clone() {
                let (branch, since) = if let Some(cursor) = self.pending_requests.get(&req_id) {
                    (cursor.branch.clone(), Some(cursor.last_turn.clone()))
                } else {
                    (status.active_branch.clone(), Some(status.head_turn.clone()))
                };

                let _ = self
                    .control
                    .wait_for_turn_after(&branch, since.as_ref(), timeout)
                    .map_err(ServiceError::from)?;
            } else {
                let _ = self
                    .control
                    .wait_for_turn_after(&status.active_branch, Some(&status.head_turn), timeout)
                    .map_err(ServiceError::from)?;
            }
        }

        self.control.drain_pending().map_err(ServiceError::from)?;
        let mut responses = codebase::list_agent_responses(&self.control, &handle);

        if let Some(request_id) = request_filter.as_deref() {
            responses.retain(|resp| resp.request_id == request_id);

            if let Some(limit) = limit {
                if responses.len() > limit {
                    responses.truncate(limit);
                }
            }

            if !responses.is_empty() {
                let entry = self
                    .pending_requests
                    .entry(request_id.to_string())
                    .or_insert(transcript::TranscriptCursor {
                        branch: status.active_branch.clone(),
                        last_turn: status.head_turn.clone(),
                        actor: Some(handle.actor.clone()),
                    });
                entry.branch = status.active_branch.clone();
                entry.last_turn = status.head_turn.clone();
                entry.actor.get_or_insert_with(|| handle.actor.clone());
            }
        } else if let Some(limit) = limit {
            if responses.len() > limit {
                responses.truncate(limit);
            }
        }

        Ok(json!({ "responses": responses }))
    }

    fn cmd_transcript_show(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let request_id = params
            .get("request_id")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("request_id"))?;

        if let Some(branch_name) = params.get("branch").and_then(Value::as_str) {
            self.switch_branch(branch_name)?;
        }

        let provided_branch = params
            .get("branch")
            .and_then(Value::as_str)
            .map(|s| BranchId::new(s.to_string()));
        let branch = if let Some(branch_id) = provided_branch {
            branch_id
        } else if let Some(cursor) = self.pending_requests.get(request_id) {
            cursor.branch.clone()
        } else {
            BranchId::main()
        };
        self.switch_branch(&branch.0)?;

        let limit = params.get("limit").and_then(Value::as_u64).unwrap_or(20) as usize;

        self.control.drain_pending().map_err(ServiceError::from)?;

        let existing_cursor = self.pending_requests.get(request_id);
        let (entries, mut cursor) = transcript::transcript_entries(
            &self.control,
            request_id,
            existing_cursor,
            Some(&branch),
            limit,
        )
        .map_err(ServiceError::from)?;

        cursor.branch = branch.clone();
        if let Some(existing) = self.pending_requests.get(request_id) {
            if cursor.actor.is_none() {
                cursor.actor = existing.actor.clone();
            }
            cursor.last_turn = existing.last_turn.clone();
        }

        if let Some(existing) = self.pending_requests.get_mut(request_id) {
            existing.branch = cursor.branch.clone();
            if cursor.actor.is_some() {
                existing.actor = cursor.actor.clone();
            }
        } else {
            self.pending_requests
                .insert(request_id.to_string(), cursor.clone());
        }

        let entries: Vec<Value> = entries
            .into_iter()
            .map(|entry| {
                let transcript::TranscriptEntry {
                    actor,
                    handle,
                    agent,
                    prompt,
                    response,
                    role,
                    tool,
                    response_timestamp,
                } = entry;

                let mut value = json!({
                    "actor": actor.to_string(),
                    "handle": handle.to_string(),
                    "agent": agent,
                    "prompt": prompt,
                    "response": response,
                });

                if let Some(map) = value.as_object_mut() {
                    if let Some(timestamp) = response_timestamp {
                        map.insert("timestamp".to_string(), Value::from(timestamp.to_rfc3339()));
                    }
                    if let Some(role) = role {
                        map.insert("role".to_string(), Value::from(role));
                    }
                    if let Some(tool) = tool {
                        map.insert("tool".to_string(), Value::from(tool));
                    }
                }

                value
            })
            .collect();

        Ok(json!({
            "request_id": request_id,
            "branch": cursor.branch.to_string(),
            "entries": entries,
        }))
    }

    fn cmd_transcript_tail(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let request_id = params
            .get("request_id")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("request_id"))?;

        let provided_branch = params
            .get("branch")
            .and_then(Value::as_str)
            .map(|s| BranchId::new(s.to_string()));
        let branch = if let Some(branch_id) = provided_branch {
            branch_id
        } else if let Some(cursor) = self.pending_requests.get(request_id) {
            cursor.branch.clone()
        } else {
            BranchId::main()
        };
        self.switch_branch(&branch.0)?;

        let since_turn = if let Some(s) = params.get("since").and_then(Value::as_str) {
            Some(TurnId::new(s.to_string()))
        } else {
            self.pending_requests
                .get(request_id)
                .map(|cursor| cursor.last_turn.clone())
        };

        let limit = params.get("limit").and_then(Value::as_u64).unwrap_or(20) as usize;

        let wait_duration = params
            .get("wait_ms")
            .and_then(Value::as_u64)
            .map(Duration::from_millis);

        self.control.drain_pending().map_err(ServiceError::from)?;

        let existing_cursor = self.pending_requests.get(request_id);
        let (mut cursor, chunk) = transcript::transcript_events(
            &mut self.control,
            request_id,
            existing_cursor,
            Some(&branch),
            since_turn.as_ref(),
            limit,
            wait_duration,
        )
        .map_err(ServiceError::from)?;

        cursor.branch = branch.clone();
        if let Some(existing) = self.pending_requests.get_mut(request_id) {
            *existing = cursor.clone();
        } else {
            self.pending_requests
                .insert(request_id.to_string(), cursor.clone());
        }

        let events: Vec<Value> = transcript::event_batches_payload(&chunk);

        Ok(json!({
            "request_id": request_id,
            "branch": cursor.branch.to_string(),
            "events": events,
            "next_cursor": chunk.next_cursor.map(|t| t.to_string()),
            "head": chunk.head.map(|t| t.to_string()),
            "has_more": chunk.has_more,
        }))
    }

    fn cmd_workflow_list(&mut self, _params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let binding = self.ensure_interpreter_binding()?;
        let (definitions, instances) = self.collect_interpreter_state(&binding);

        let definitions_json: Vec<Value> = definitions.iter().map(serialize_definition).collect();
        let instances_json: Vec<Value> = instances.iter().map(serialize_instance).collect();

        Ok(json!({
            "definitions": definitions_json,
            "instances": instances_json,
        }))
    }

    fn cmd_workflow_start(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let definition = params
            .get("definition")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("definition"))?;
        let label = params.get("label").and_then(Value::as_str);
        let binding = self.ensure_interpreter_binding()?;
        let before_ids = self.interpreter_instance_ids(&binding);

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(definition.to_string())],
        );

        let turn = self
            .control
            .send_message(binding.actor.clone(), binding.facet.clone(), payload)
            .map_err(ServiceError::from)?;

        let (_, instances_after) = self.collect_interpreter_state(&binding);
        let new_instance = instances_after
            .into_iter()
            .find(|instance| !before_ids.contains(&instance.instance_id));

        let mut response = json!({
            "status": "started",
            "turn": turn.to_string(),
        });

        if let Some(instance) = new_instance {
            response
                .as_object_mut()
                .unwrap()
                .insert("instance".into(), serialize_instance(&instance));
        }

        if let Some(path) = params.get("definition_path").and_then(Value::as_str) {
            response
                .as_object_mut()
                .unwrap()
                .insert("definition_path".into(), json!(path));
        }

        if let Some(label_value) = label {
            response
                .as_object_mut()
                .unwrap()
                .insert("label".into(), json!(label_value));
        }

        Ok(response)
    }

    fn cmd_reaction_register(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        let actor_str = params
            .get("actor")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("actor"))?;
        let actor_uuid = parse_uuid(actor_str)?;
        let actor = ActorId::from_uuid(actor_uuid);

        let facet_str = params
            .get("facet")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("facet"))?;
        let facet_uuid = parse_uuid(facet_str)?;
        let facet = FacetId::from_uuid(facet_uuid);

        let pattern_text = params
            .get("pattern")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("pattern"))?;
        let pattern_value: IOValue = pattern_text
            .parse()
            .map_err(|err| ServiceError::InvalidParams(format!("invalid pattern: {err}")))?;

        let effect_value = params
            .get("effect")
            .ok_or_else(|| ServiceError::invalid_param("effect"))?;
        let effect = self.parse_reaction_effect(effect_value)?;

        let pattern = Pattern {
            id: Uuid::new_v4(),
            pattern: pattern_value,
            facet,
        };

        let definition = ReactionDefinition::new(pattern, effect);
        let reaction_id = self
            .control
            .register_reaction(actor, definition)
            .map_err(ServiceError::from)?;

        Ok(json!({ "reaction_id": reaction_id.to_string() }))
    }

    fn cmd_reaction_unregister(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        let reaction_str = params
            .get("reaction_id")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("reaction_id"))?;
        let reaction_uuid = parse_uuid(reaction_str)?;

        let removed = self
            .control
            .unregister_reaction(reaction_uuid)
            .map_err(ServiceError::from)?;

        Ok(json!({ "removed": removed }))
    }

    fn cmd_reaction_list(&mut self) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let reactions = self.control.list_reactions();
        let serialized = serde_json::to_value(&reactions)
            .map_err(|err| ServiceError::Protocol(err.to_string()))?;
        Ok(json!({ "reactions": serialized }))
    }

    fn cmd_invoke_capability(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        let cap_id = params
            .get("capability")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("capability"))?;

        let payload = params
            .get("payload")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("payload"))?;

        let capability = parse_uuid(cap_id)?;
        let payload_value: IOValue = payload
            .parse()
            .map_err(|err| ServiceError::InvalidParams(format!("invalid payload: {err}")))?;

        let response = self
            .control
            .invoke_capability(capability, payload_value)
            .map_err(ServiceError::from)?;

        let rendered = format!("{:?}", response);
        Ok(json!({ "result": rendered }))
    }

    fn parse_reaction_effect(&self, value: &Value) -> Result<ReactionEffect, ServiceError> {
        let effect_obj = value
            .as_object()
            .ok_or_else(|| ServiceError::invalid_param("effect"))?;

        let effect_type = effect_obj
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("effect.type"))?;

        match effect_type {
            "assert" => {
                let value_field = effect_obj
                    .get("value")
                    .ok_or_else(|| ServiceError::invalid_param("effect.value"))?;
                let reaction_value = self.parse_reaction_value(value_field)?;

                let target_facet = if let Some(facet_text) = effect_obj.get("target_facet") {
                    let facet_str = facet_text
                        .as_str()
                        .ok_or_else(|| ServiceError::invalid_param("effect.target_facet"))?;
                    let uuid = parse_uuid(facet_str)?;
                    Some(FacetId::from_uuid(uuid))
                } else {
                    None
                };

                Ok(ReactionEffect::Assert {
                    value: reaction_value,
                    target_facet,
                })
            }
            "send-message" => {
                let actor_str = effect_obj
                    .get("actor")
                    .and_then(Value::as_str)
                    .ok_or_else(|| ServiceError::invalid_param("effect.actor"))?;
                let facet_str = effect_obj
                    .get("facet")
                    .and_then(Value::as_str)
                    .ok_or_else(|| ServiceError::invalid_param("effect.facet"))?;
                let payload_field = effect_obj
                    .get("payload")
                    .ok_or_else(|| ServiceError::invalid_param("effect.payload"))?;

                let target_actor = ActorId::from_uuid(parse_uuid(actor_str)?);
                let target_facet = FacetId::from_uuid(parse_uuid(facet_str)?);
                let payload = self.parse_reaction_value(payload_field)?;

                Ok(ReactionEffect::SendMessage {
                    actor: target_actor,
                    facet: target_facet,
                    payload,
                })
            }
            other => Err(ServiceError::InvalidParams(format!(
                "unsupported effect type: {}",
                other
            ))),
        }
    }

    fn parse_reaction_value(&self, value: &Value) -> Result<ReactionValue, ServiceError> {
        if let Some(text) = value.as_str() {
            let literal: IOValue = text.parse().map_err(|err| {
                ServiceError::InvalidParams(format!("invalid effect value: {err}"))
            })?;
            return Ok(ReactionValue::Literal { value: literal });
        }

        if let Some(obj) = value.as_object() {
            let value_type = obj
                .get("type")
                .and_then(Value::as_str)
                .ok_or_else(|| ServiceError::invalid_param("effect.value.type"))?;
            match value_type {
                "literal" => {
                    let literal_text = obj
                        .get("value")
                        .and_then(Value::as_str)
                        .ok_or_else(|| ServiceError::invalid_param("effect.value.value"))?;
                    let literal: IOValue = literal_text.parse().map_err(|err| {
                        ServiceError::InvalidParams(format!("invalid effect value: {err}"))
                    })?;
                    Ok(ReactionValue::Literal { value: literal })
                }
                "match" => Ok(ReactionValue::Match),
                "match-index" => {
                    let index_val = obj
                        .get("index")
                        .and_then(Value::as_u64)
                        .ok_or_else(|| ServiceError::invalid_param("effect.value.index"))?;
                    Ok(ReactionValue::MatchIndex {
                        index: index_val as usize,
                    })
                }
                other => Err(ServiceError::InvalidParams(format!(
                    "unsupported value type: {}",
                    other
                ))),
            }
        } else {
            Err(ServiceError::invalid_param("effect.value"))
        }
    }

    fn cmd_dataspace_assertions(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        let actor_filter = if let Some(actor) = params.get("actor").and_then(Value::as_str) {
            let uuid = parse_uuid(actor)?;
            Some(ActorId::from_uuid(uuid))
        } else {
            None
        };

        let label_filter = params
            .get("label")
            .and_then(Value::as_str)
            .map(|s| s.to_string());
        let request_filter = params
            .get("request_id")
            .and_then(Value::as_str)
            .map(|s| s.to_string());
        let limit = params
            .get("limit")
            .and_then(Value::as_u64)
            .map(|n| n as usize);

        self.control.drain_pending().map_err(ServiceError::from)?;

        let mut assertions = self.control.list_assertions(actor_filter.as_ref());

        if let Some(label) = &label_filter {
            assertions.retain(|info| assertion_matches_label(&info.value, label));
        }

        if let Some(request_id) = &request_filter {
            assertions.retain(|info| assertion_matches_request_id(&info.value, request_id));
        }

        if let Some(limit) = limit {
            assertions.truncate(limit);
        }

        let mut actor_cache: HashMap<ActorId, Value> = HashMap::new();
        let mut assertions_payload = Vec::new();
        for assertion in assertions {
            let actor = assertion.actor.clone();
            let actor_info = self
                .describe_actor(&actor, &mut actor_cache)
                .unwrap_or_else(|| json!({ "id": actor.to_string() }));

            let mut entry = serde_json::Map::new();
            let actor_str = actor.to_string();
            entry.insert("actor".to_string(), Value::String(actor_str));
            entry.insert("actor_info".to_string(), actor_info);
            entry.insert(
                "handle".to_string(),
                Value::String(assertion.handle.to_string()),
            );
            entry.insert(
                "summary".to_string(),
                Value::String(io_value_summary(&assertion.value, 80)),
            );
            entry.insert(
                "value".to_string(),
                Value::String(format!("{:?}", assertion.value)),
            );
            entry.insert(
                "value_structured".to_string(),
                io_value_to_json(&assertion.value),
            );

            assertions_payload.push(Value::Object(entry));
        }

        Ok(json!({ "assertions": assertions_payload }))
    }

    fn cmd_dataspace_events(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;

        let branch_name = params
            .get("branch")
            .and_then(Value::as_str)
            .unwrap_or("main");
        let branch = BranchId::new(branch_name);

        let since_turn = params
            .get("since")
            .and_then(Value::as_str)
            .map(|s| TurnId::new(s.to_string()));

        let limit = params
            .get("limit")
            .and_then(Value::as_u64)
            .map(|v| v as usize)
            .unwrap_or(20);

        let mut filter = AssertionEventFilter::inclusive();

        if let Some(actor) = params.get("actor").and_then(Value::as_str) {
            let uuid = parse_uuid(actor)?;
            filter.actor = Some(ActorId::from_uuid(uuid));
        }

        if let Some(label) = params.get("label").and_then(Value::as_str) {
            filter.label = Some(label.to_string());
        }

        if let Some(request_id) = params.get("request_id").and_then(Value::as_str) {
            filter.request_id = Some(request_id.to_string());
        }

        if let Some(types) = params.get("event_types").and_then(Value::as_array) {
            filter.include_asserts = false;
            filter.include_retracts = false;
            for ty in types {
                if let Some(name) = ty.as_str() {
                    match name {
                        "assert" | "asserts" => filter.include_asserts = true,
                        "retract" | "retracts" => filter.include_retracts = true,
                        _ => {}
                    }
                }
            }
        }

        let wait_duration = params
            .get("wait_ms")
            .and_then(Value::as_u64)
            .map(Duration::from_millis);

        self.control.drain_pending().map_err(ServiceError::from)?;

        let chunk = self
            .control
            .assertion_events_since(&branch, since_turn.as_ref(), limit, filter, wait_duration)
            .map_err(ServiceError::from)?;

        let mut actor_cache: HashMap<ActorId, Value> = HashMap::new();
        let mut events_payload = Vec::new();
        for batch in &chunk.events {
            let actor_info = self
                .describe_actor(&batch.actor, &mut actor_cache)
                .unwrap_or_else(|| json!({ "id": batch.actor.to_string() }));

            let mut batch_obj = serde_json::Map::new();
            batch_obj.insert("turn".to_string(), Value::String(batch.turn_id.to_string()));
            batch_obj.insert("actor".to_string(), Value::String(batch.actor.to_string()));
            batch_obj.insert("actor_info".to_string(), actor_info);
            batch_obj.insert("clock".to_string(), Value::Number(batch.clock.into()));
            batch_obj.insert(
                "timestamp".to_string(),
                Value::String(batch.timestamp.to_rfc3339()),
            );

            let mut event_values = Vec::new();
            for event in &batch.events {
                let mut event_obj = serde_json::Map::new();
                let action = match event.action {
                    AssertionEventAction::Assert => "assert",
                    AssertionEventAction::Retract => "retract",
                };
                event_obj.insert("action".to_string(), Value::String(action.to_string()));
                event_obj.insert(
                    "handle".to_string(),
                    Value::String(event.handle.to_string()),
                );

                if let Some(value) = event.value.as_ref() {
                    event_obj.insert("value".to_string(), Value::String(format!("{:?}", value)));
                    event_obj.insert("value_structured".to_string(), io_value_to_json(value));
                    event_obj.insert(
                        "summary".to_string(),
                        Value::String(io_value_summary(value, 80)),
                    );

                    if let Some(agent_response) = codebase::parse_agent_response(value) {
                        let codebase::AgentResponse {
                            request_id,
                            prompt,
                            response,
                            agent,
                            role,
                            tool,
                            timestamp,
                        } = agent_response;

                        let mut transcript = serde_json::Map::new();
                        transcript.insert("request_id".to_string(), Value::String(request_id));
                        transcript.insert("prompt".to_string(), Value::String(prompt));
                        transcript.insert("response".to_string(), Value::String(response));
                        transcript.insert("agent".to_string(), Value::String(agent));
                        if let Some(ts) = timestamp {
                            transcript.insert(
                                "response_timestamp".to_string(),
                                Value::String(ts.to_rfc3339()),
                            );
                        }
                        if let Some(role) = role {
                            transcript.insert("role".to_string(), Value::String(role));
                        }
                        if let Some(tool) = tool {
                            transcript.insert("tool".to_string(), Value::String(tool));
                        }

                        event_obj.insert("transcript".to_string(), Value::Object(transcript));
                    }
                } else {
                    event_obj.insert("summary".to_string(), Value::String("null".to_string()));
                }

                event_values.push(Value::Object(event_obj));
            }

            batch_obj.insert("events".to_string(), Value::Array(event_values));
            events_payload.push(Value::Object(batch_obj));
        }

        let mut result_obj = serde_json::Map::new();
        result_obj.insert("events".to_string(), Value::Array(events_payload));
        if let Some(cursor) = &chunk.next_cursor {
            result_obj.insert("next_cursor".to_string(), Value::String(cursor.to_string()));
        }
        if let Some(head) = &chunk.head {
            result_obj.insert("head".to_string(), Value::String(head.to_string()));
        }
        result_obj.insert("has_more".to_string(), Value::Bool(chunk.has_more));

        Ok(Value::Object(result_obj))
    }

    fn switch_branch(&mut self, branch: &str) -> Result<(), ServiceError> {
        let branch_id = BranchId::new(branch);
        self.control
            .switch_branch(branch_id)
            .map_err(ServiceError::from)
    }

    fn workspace_handle(&self) -> Option<codebase::WorkspaceHandle> {
        codebase::workspace_handle(&self.control)
    }

    fn ensure_claude_handle(&mut self) -> Result<codebase::AgentHandle, ServiceError> {
        match codebase::ensure_claude_agent(&mut self.control) {
            Ok(handle) => Ok(handle),
            Err(err) => Err(ServiceError::from(err)),
        }
    }
}

fn short_id(text: &str) -> String {
    if text.len() <= 8 {
        text.to_string()
    } else {
        format!("{}...", &text[..8])
    }
}

fn assertion_matches_label(value: &IOValue, label: &str) -> bool {
    if let Some(record) = as_record(value) {
        record.has_label(label)
    } else {
        value
            .as_symbol()
            .map(|sym| sym.as_ref() == label)
            .unwrap_or(false)
    }
}

fn assertion_matches_request_id(value: &IOValue, request_id: &str) -> bool {
    if let Some(record) = as_record(value) {
        if record.len() == 0 {
            return false;
        }
        record
            .field_string(0)
            .map(|s| s == request_id)
            .unwrap_or(false)
    } else {
        false
    }
}

#[derive(Debug)]
enum ServiceError {
    Parse(String),
    InvalidParams(String),
    Unsupported(String),
    Protocol(String),
    Runtime(RuntimeError),
}

impl ServiceError {
    fn invalid_param(name: &str) -> Self {
        ServiceError::InvalidParams(format!("missing or invalid parameter: {}", name))
    }
}

impl From<RuntimeError> for ServiceError {
    fn from(err: RuntimeError) -> Self {
        ServiceError::Runtime(err)
    }
}

#[derive(Deserialize)]
struct RequestEnvelope {
    id: Value,
    command: String,
    #[serde(default)]
    params: Value,
}

#[derive(Serialize)]
struct ResponseEnvelope {
    id: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<ErrorEnvelope>,
}

impl ResponseEnvelope {
    fn success(id: Value, result: Value) -> Self {
        Self {
            id,
            result: Some(result),
            error: None,
        }
    }

    fn from_error(id: Value, error: ServiceError) -> Self {
        Self {
            id,
            result: None,
            error: Some(ErrorEnvelope::from(error)),
        }
    }
}

fn serialize_definition(definition: &DefinitionRecord) -> Value {
    json!({
        "id": definition.definition_id,
        "name": definition.program_name,
        "source": definition.source,
    })
}

fn serialize_instance(instance: &InstanceRecord) -> Value {
    let mut value = json!({
        "id": instance.instance_id,
        "program": serialize_program_ref(&instance.program),
        "program_name": instance.program_name,
        "status": serialize_instance_status(&instance.status),
    });

    if let Some(state) = &instance.state {
        value
            .as_object_mut()
            .unwrap()
            .insert("state".into(), json!(state));
    }

    if let Some(progress) = &instance.progress {
        value
            .as_object_mut()
            .unwrap()
            .insert("progress".into(), serialize_progress(progress));
    }

    value
}

fn serialize_program_ref(program: &ProgramRef) -> Value {
    match program {
        ProgramRef::Definition(id) => json!({
            "type": "definition",
            "id": id,
        }),
        ProgramRef::Inline(source) => json!({
            "type": "inline",
            "source": source,
        }),
    }
}

fn serialize_instance_status(status: &InstanceStatus) -> Value {
    match status {
        InstanceStatus::Running => json!({ "state": "running" }),
        InstanceStatus::Waiting(wait) => json!({
            "state": "waiting",
            "wait": serialize_wait_status(wait),
        }),
        InstanceStatus::Completed => json!({ "state": "completed" }),
        InstanceStatus::Failed(message) => json!({
            "state": "failed",
            "message": message,
        }),
    }
}

fn serialize_wait_status(wait: &WaitStatus) -> Value {
    match wait {
        WaitStatus::RecordFieldEq {
            label,
            field,
            value,
        } => json!({
            "type": "record-field-eq",
            "label": label,
            "field": field,
            "value": format!("{:?}", value.to_io_value()),
        }),
        WaitStatus::Signal { label } => json!({
            "type": "signal",
            "label": label,
        }),
        WaitStatus::ToolResult { tag } => json!({
            "type": "tool-result",
            "tag": tag,
        }),
    }
}

fn serialize_progress(progress: &InstanceProgress) -> Value {
    let mut value = json!({
        "entry_pending": progress.entry_pending,
        "frame_depth": progress.frame_depth,
    });

    if let Some(state) = &progress.state {
        value
            .as_object_mut()
            .unwrap()
            .insert("state".into(), json!(state));
    }

    if let Some(wait) = &progress.waiting {
        value
            .as_object_mut()
            .unwrap()
            .insert("waiting".into(), serialize_wait_status(wait));
    }

    value
}

#[derive(Serialize)]
struct ErrorEnvelope {
    code: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    details: Option<Value>,
}

impl From<ServiceError> for ErrorEnvelope {
    fn from(error: ServiceError) -> Self {
        match error {
            ServiceError::Parse(message) => ErrorEnvelope {
                code: "parse_error".into(),
                message,
                details: None,
            },
            ServiceError::InvalidParams(message) => ErrorEnvelope {
                code: "invalid_params".into(),
                message,
                details: None,
            },
            ServiceError::Unsupported(command) => ErrorEnvelope {
                code: "unsupported_command".into(),
                message: format!("Command '{command}' is not supported yet"),
                details: None,
            },
            ServiceError::Protocol(message) => ErrorEnvelope {
                code: "protocol_error".into(),
                message,
                details: None,
            },
            ServiceError::Runtime(err) => {
                let message = err.to_string();
                let details = match &err {
                    RuntimeError::Capability(cap_err) => {
                        let (variant, cap_id, reason) = match cap_err {
                            CapabilityError::NotFound(id) => ("NotFound", Some(id), None),
                            CapabilityError::Revoked(id) => ("Revoked", Some(id), None),
                            CapabilityError::Denied(id, detail) => {
                                ("Denied", Some(id), Some(detail.as_str()))
                            }
                        };
                        Some(json!({
                            "category": "capability",
                            "variant": variant,
                            "capability": cap_id.map(|id| id.to_string()),
                            "reason": reason,
                        }))
                    }
                    _ => None,
                };

                ErrorEnvelope {
                    code: "runtime_error".into(),
                    message,
                    details,
                }
            }
        }
    }
}

fn parse_uuid(value: &str) -> Result<Uuid, ServiceError> {
    Uuid::parse_str(value)
        .map_err(|err| ServiceError::InvalidParams(format!("invalid UUID '{}': {}", value, err)))
}

fn parse_preserves(value: &Value) -> Result<IOValue, ServiceError> {
    match value {
        Value::String(s) => s.parse().map_err(|err| {
            ServiceError::InvalidParams(format!("invalid preserves value: {}", err))
        }),
        other => Err(ServiceError::InvalidParams(format!(
            "payload must be a string containing preserves text, found {}",
            other
        ))),
    }
}
