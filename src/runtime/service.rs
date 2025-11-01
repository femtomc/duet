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
use crate::interpreter::ensure_control_interpreter;
use crate::interpreter::protocol::{
    DefinitionRecord, InputRequestRecord, InstanceProgress, InstanceRecord, InstanceStatus,
    ProgramRef, WaitStatus, input_request_from_value,
};
use crate::util::io_value::{as_record, io_value_summary, io_value_to_json};
use preserves::IOValue;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::fs;
use std::io::{self, BufRead, Write};
use std::path::Path;
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
    _facet: FacetId,
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
            "fork" => self.cmd_fork(params),
            "merge" => self.cmd_merge(params),
            "list_entities" => self.cmd_list_entities(params),
            "list_capabilities" => self.cmd_list_capabilities(params),
            "workspace_entries" => self.cmd_workspace_entries(),
            "transcript_show" => self.cmd_transcript_show(params),
            "transcript_tail" => self.cmd_transcript_tail(params),
            "workflow_list" => self.cmd_workflow_list(params),
            "workflow_follow" => self.cmd_workflow_follow(params),
            "reaction_list" => self.cmd_reaction_list(),
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

        let handle = ensure_control_interpreter(self.control).map_err(ServiceError::from)?;
        let actor_str = handle.actor.to_string();
        let facet_str = handle.facet.0.to_string();

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
                    "entity_inspection",
                    "branch_listing",
                    "dataspace_inspection",
                    "dataspace_events",
                    "transcript_inspection",
                    "program_inspection",
                    "workflow_inspection",
                    "reaction_inspection"
                ],
                "control_interpreter": {
                    "actor": actor_str,
                    "facet": facet_str,
                    "short_actor": short_id(&actor_str),
                }
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

        let handle = ensure_control_interpreter(self.control).map_err(ServiceError::from)?;
        let binding = InterpreterBinding {
            actor: handle.actor,
            _facet: handle.facet,
        };

        self.interpreter = Some(binding.clone());
        Ok(binding)
    }

    fn collect_interpreter_state(
        &mut self,
        binding: &InterpreterBinding,
    ) -> (
        Vec<DefinitionRecord>,
        Vec<InstanceRecord>,
        Vec<InputRequestRecord>,
    ) {
        let assertions = self.control.list_assertions_for_actor(&binding.actor);

        let mut definitions: HashMap<String, DefinitionRecord> = HashMap::new();
        let mut instances: HashMap<String, InstanceRecord> = HashMap::new();
        let mut prompts: Vec<InputRequestRecord> = Vec::new();

        for (_, value) in assertions {
            if let Some(definition) = DefinitionRecord::parse(&value) {
                definitions.insert(definition.definition_id.clone(), definition);
                continue;
            }

            if let Some(instance) = InstanceRecord::parse(&value) {
                instances.insert(instance.instance_id.clone(), instance);
                continue;
            }

            if let Some(prompt) = input_request_from_value(&value) {
                prompts.push(prompt);
            }
        }

        let mut definition_list: Vec<_> = definitions.into_values().collect();
        definition_list.sort_by(|a, b| a.program_name.cmp(&b.program_name));

        let mut instance_list: Vec<_> = instances.into_values().collect();
        instance_list.sort_by(|a, b| a.instance_id.cmp(&b.instance_id));

        prompts.sort_by(|a, b| a.request_id.cmp(&b.request_id));

        (definition_list, instance_list, prompts)
    }

    fn discover_example_programs(&self) -> Vec<Value> {
        let mut examples = Vec::new();
        let root = self.control.runtime().config().root.clone();
        let programs_dir = root.join("programs");

        if programs_dir.exists() {
            self.collect_program_files(&programs_dir, &programs_dir, &mut examples);
            examples.sort_by(|a, b| {
                let a_path = a.get("path").and_then(Value::as_str).unwrap_or("");
                let b_path = b.get("path").and_then(Value::as_str).unwrap_or("");
                a_path.cmp(b_path)
            });
        }

        examples
    }

    fn collect_program_files(&self, base: &Path, dir: &Path, out: &mut Vec<Value>) {
        let entries = match fs::read_dir(dir) {
            Ok(entries) => entries,
            Err(_) => return,
        };

        for entry in entries {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => continue,
            };
            let path = entry.path();
            if path.is_dir() {
                self.collect_program_files(base, &path, out);
            } else if path
                .extension()
                .and_then(|ext| ext.to_str())
                .map(|ext| ext.eq_ignore_ascii_case("duet"))
                .unwrap_or(false)
            {
                let relative = path
                    .strip_prefix(base)
                    .unwrap_or_else(|_| path.as_path())
                    .to_path_buf();
                let relative_str = relative.to_string_lossy().replace('\\', "/");
                let mut item = json!({
                    "path": relative_str,
                    "absolute_path": path.to_string_lossy(),
                });
                if let Some(desc) = Self::read_program_description(&path) {
                    item.as_object_mut()
                        .unwrap()
                        .insert("description".into(), json!(desc));
                }
                out.push(item);
            }
        }
    }

    fn read_program_description(_path: &Path) -> Option<String> {
        None
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
        self.control.drain_pending().map_err(ServiceError::from)?;
        let (definitions, instances, _prompts) = self.collect_interpreter_state(&binding);

        let definitions_json: Vec<Value> = definitions.iter().map(serialize_definition).collect();
        let instances_json: Vec<Value> = instances.iter().map(serialize_instance).collect();
        let examples_json = self.discover_example_programs();

        Ok(json!({
            "definitions": definitions_json,
            "instances": instances_json,
            "examples": examples_json,
        }))
    }

    fn cmd_workflow_follow(&mut self, params: &Value) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let instance_id = params
            .get("instance_id")
            .and_then(Value::as_str)
            .ok_or_else(|| ServiceError::invalid_param("instance_id"))?;

        let binding = self.ensure_interpreter_binding()?;
        self.control.drain_pending().map_err(ServiceError::from)?;
        let (_, instances, prompts) = self.collect_interpreter_state(&binding);

        let instance = instances
            .into_iter()
            .find(|entry| entry.instance_id == instance_id)
            .ok_or_else(|| ServiceError::invalid_param("instance_id"))?;

        let prompt_values: Vec<Value> = prompts
            .iter()
            .filter(|prompt| prompt.instance_id == instance.instance_id)
            .map(serialize_prompt)
            .collect();

        Ok(json!({
            "instance": serialize_instance(&instance),
            "prompts": prompt_values,
        }))
    }

    fn cmd_reaction_list(&mut self) -> Result<Value, ServiceError> {
        self.ensure_handshake()?;
        let reactions = self.control.list_reactions();
        let serialized = serde_json::to_value(&reactions)
            .map_err(|err| ServiceError::Protocol(err.to_string()))?;
        Ok(json!({ "reactions": serialized }))
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
                    event_obj.insert("value_structured".to_string(), io_value_to_json(value));
                    event_obj.insert(
                        "summary".to_string(),
                        Value::String(io_value_summary(value, 80)),
                    );

                    if let Some(agent_response) = codebase::parse_agent_response(value) {
                        let codebase::AgentResponse {
                            agent_id,
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
                        transcript.insert("agent_id".to_string(), Value::String(agent_id));
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
            .field_string(1)
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

    if !instance.roles.is_empty() {
        let mut roles_json = Vec::with_capacity(instance.roles.len());
        let mut entities_json = Vec::new();

        for role in &instance.roles {
            let mut role_obj = serde_json::Map::new();
            role_obj.insert("name".into(), Value::String(role.name.clone()));

            let mut props = serde_json::Map::new();
            for (key, value_str) in &role.properties {
                props.insert(key.clone(), Value::String(value_str.clone()));
            }
            role_obj.insert("properties".into(), Value::Object(props.clone()));
            roles_json.push(Value::Object(role_obj));

            if let (Some(actor), Some(facet)) =
                (role.properties.get("actor"), role.properties.get("facet"))
            {
                let mut entity_obj = serde_json::Map::new();
                entity_obj.insert("role".into(), Value::String(role.name.clone()));
                entity_obj.insert("actor".into(), Value::String(actor.clone()));
                entity_obj.insert("facet".into(), Value::String(facet.clone()));
                if let Some(entity_id) = role.properties.get("entity") {
                    entity_obj.insert("entity".into(), Value::String(entity_id.clone()));
                }
                if let Some(entity_type) = role.properties.get("entity-type") {
                    entity_obj.insert("entity_type".into(), Value::String(entity_type.clone()));
                }
                if let Some(agent_kind) = role.properties.get("agent-kind") {
                    entity_obj.insert("agent_kind".into(), Value::String(agent_kind.clone()));
                }
                entities_json.push(Value::Object(entity_obj));
            }
        }

        let object = value.as_object_mut().unwrap();
        object.insert("roles".into(), Value::Array(roles_json));
        if !entities_json.is_empty() {
            object.insert("entities".into(), Value::Array(entities_json));
        }
    }

    value
}

fn serialize_prompt(prompt: &InputRequestRecord) -> Value {
    let prompt_value = prompt.prompt.to_io_value();
    json!({
        "instance_id": prompt.instance_id,
        "request_id": prompt.request_id,
        "tag": prompt.tag,
        "prompt": io_value_to_json(&prompt_value),
        "summary": io_value_summary(&prompt_value, 80),
    })
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
        WaitStatus::UserInput {
            request_id,
            tag,
            prompt,
        } => {
            let prompt_value = prompt.to_io_value();
            json!({
                "type": "user-input",
                "request_id": request_id,
                "tag": tag,
                "prompt": io_value_to_json(&prompt_value),
                "summary": io_value_summary(&prompt_value, 80),
            })
        }
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
