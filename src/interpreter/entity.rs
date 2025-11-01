use std::collections::HashMap;
use std::sync::Mutex;

use crate::codebase::agent;
use crate::interpreter::protocol::{
    ENTITY_RECORD_LABEL, InputRequestRecord, OBSERVER_RECORD_LABEL, TOOL_REQUEST_RECORD_LABEL,
    TOOL_RESULT_RECORD_LABEL, WaitRecord, input_request_to_value, input_response_from_value,
    runtime_snapshot_from_value, runtime_snapshot_to_value, wait_record_to_value,
};
use crate::interpreter::value::ValueContext;
use crate::interpreter::{
    Action, DEFINE_MESSAGE_LABEL, DefinitionRecord, InstanceProgress, InstanceRecord,
    InstanceStatus, InterpreterHost, InterpreterRuntime, LOG_RECORD_LABEL, NOTIFY_MESSAGE_LABEL,
    ProgramIr, ProgramRef, RESUME_MESSAGE_LABEL, RUN_MESSAGE_LABEL, RoleBinding, RuntimeError,
    RuntimeEvent, RuntimeSnapshot, Value, WaitCondition, WaitStatus, build_ir, parse_program,
};
use crate::runtime::actor::{
    Activation, CapabilitySpec, ENTITY_SPAWN_CAPABILITY_KIND, Entity, HydratableEntity,
};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityCatalog;
use crate::runtime::turn::{ActorId, CapabilityCompletion, FacetId, Handle};
use crate::util::io_value::{as_record, record_with_label};
use preserves::IOValue;
use std::convert::TryFrom;
use uuid::Uuid;

/// Entity that executes interpreter programs inside the Syndicated Actor runtime.
pub struct InterpreterEntity {
    definitions: Mutex<HashMap<String, StoredDefinition>>,
    waiting: Mutex<HashMap<String, WaitingInstance>>,
    actor_id: Mutex<Option<ActorId>>,
    observers: Mutex<HashMap<String, ObserverEntry>>,
    spawn_capability_granted: Mutex<bool>,
}

#[derive(Clone)]
struct StoredDefinition {
    program: ProgramIr,
    source: String,
}

impl StoredDefinition {
    fn new(program: ProgramIr, source: String) -> Self {
        Self { program, source }
    }
}

#[derive(Clone)]
struct WaitingInstance {
    program_ref: ProgramRef,
    program: ProgramIr,
    snapshot: RuntimeSnapshot,
    wait: WaitStatus,
    handle: Handle,
    wait_handle: Handle,
    facet: FacetId,
    assertions: Vec<RecordedAssertion>,
    facets: Vec<FacetId>,
    prompt_counter: u64,
    prompts: Vec<RecordedPrompt>,
    resume_pending: bool,
    matched_value: Option<IOValue>,
}

#[derive(Clone)]
struct ObserverEntry {
    id: String,
    condition: WaitCondition,
    program_ref: ProgramRef,
    program: ProgramIr,
    facet: FacetId,
    handle: Handle,
}

#[derive(Clone)]
struct RecordedAssertion {
    value: IOValue,
    handle: Handle,
}

impl RecordedAssertion {
    fn new(value: IOValue, handle: Handle) -> Self {
        Self { value, handle }
    }
}

fn assertion_to_value(assertion: &RecordedAssertion) -> IOValue {
    IOValue::record(
        IOValue::symbol("interpreter-assertion"),
        vec![
            assertion.value.clone(),
            IOValue::new(assertion.handle.0.to_string()),
        ],
    )
}

fn assertion_from_value(value: &IOValue) -> Option<RecordedAssertion> {
    let record = record_with_label(value, "interpreter-assertion")?;
    if record.len() < 2 {
        return None;
    }

    let asserted_value = record.field(0);
    let handle_str = record.field(1).as_string()?.to_string();
    let handle_uuid = Uuid::parse_str(&handle_str).ok()?;
    Some(RecordedAssertion::new(asserted_value, Handle(handle_uuid)))
}

fn facet_to_value(facet: &FacetId) -> IOValue {
    IOValue::new(facet.0.to_string())
}

fn facets_from_value(value: &IOValue) -> Vec<FacetId> {
    let mut facets = Vec::new();
    if let Some(record) = record_with_label(value, "facets") {
        for idx in 0..record.len() {
            if let Some(text) = record.field(idx).as_string() {
                if let Ok(uuid) = Uuid::parse_str(text.as_ref()) {
                    facets.push(FacetId::from_uuid(uuid));
                }
            }
        }
    }
    facets
}

const PROMPT_SNAPSHOT_RECORD_LABEL: &str = "interpreter-wait-prompt";

#[derive(Clone)]
struct RecordedPrompt {
    wait: WaitCondition,
    request_id: String,
    tag: String,
    handle: Handle,
}

impl RecordedPrompt {
    fn new(wait: WaitCondition, request_id: String, tag: String, handle: Handle) -> Self {
        Self {
            wait,
            request_id,
            tag,
            handle,
        }
    }
}

fn prompt_snapshot_to_value(prompt: &RecordedPrompt) -> IOValue {
    let wait_status = WaitStatus::from_condition(&prompt.wait);
    IOValue::record(
        IOValue::symbol(PROMPT_SNAPSHOT_RECORD_LABEL),
        vec![
            wait_status.as_value(),
            IOValue::new(prompt.handle.0.to_string()),
        ],
    )
}

fn prompt_snapshot_from_value(value: &IOValue) -> Option<RecordedPrompt> {
    let record = record_with_label(value, PROMPT_SNAPSHOT_RECORD_LABEL)?;
    if record.len() < 2 {
        return None;
    }
    let wait_value = record.field(0);
    let wait_status = as_record(&wait_value).and_then(WaitStatus::parse_record)?;
    let handle_id = record.field_string(1)?;
    let handle_uuid = Uuid::parse_str(&handle_id).ok()?;

    let (request_id, tag) = match &wait_status {
        WaitStatus::UserInput {
            request_id, tag, ..
        } => (request_id.clone(), tag.clone()),
        _ => return None,
    };

    let wait = wait_status.into_condition();

    Some(RecordedPrompt::new(
        wait,
        request_id,
        tag,
        Handle(handle_uuid),
    ))
}

#[derive(Clone)]
struct PromptRecord {
    wait: WaitCondition,
    request_id: String,
    tag: String,
    handle: Handle,
}

impl PromptRecord {
    fn new(wait: WaitCondition, request_id: String, tag: String, handle: Handle) -> Self {
        Self {
            wait,
            request_id,
            tag,
            handle,
        }
    }

    fn to_snapshot(&self) -> RecordedPrompt {
        RecordedPrompt::new(
            self.wait.clone(),
            self.request_id.clone(),
            self.tag.clone(),
            self.handle.clone(),
        )
    }
}

impl From<RecordedPrompt> for PromptRecord {
    fn from(prompt: RecordedPrompt) -> Self {
        Self {
            wait: prompt.wait,
            request_id: prompt.request_id,
            tag: prompt.tag,
            handle: prompt.handle,
        }
    }
}

fn program_ref_to_value(program_ref: &ProgramRef) -> IOValue {
    match program_ref {
        ProgramRef::Inline(source) => IOValue::record(
            IOValue::symbol("inline"),
            vec![IOValue::new(source.clone())],
        ),
        ProgramRef::Definition(id) => IOValue::record(
            IOValue::symbol("definition"),
            vec![IOValue::new(id.clone())],
        ),
    }
}

fn program_ref_from_value(value: &IOValue) -> Option<ProgramRef> {
    if let Some(record) = as_record(value) {
        if record.has_label("inline") {
            if record.len() >= 1 {
                return record
                    .field_string(0)
                    .map(|s| ProgramRef::Inline(s.to_string()));
            }
        } else if record.has_label("definition") {
            if record.len() >= 1 {
                return record
                    .field_string(0)
                    .map(|s| ProgramRef::Definition(s.to_string()));
            }
        }
    }
    None
}

fn wait_condition_to_value(condition: &WaitCondition) -> IOValue {
    match condition {
        WaitCondition::Signal { label } => {
            IOValue::record(IOValue::symbol("signal"), vec![IOValue::new(label.clone())])
        }
        WaitCondition::RecordFieldEq {
            label,
            field,
            value,
        } => IOValue::record(
            IOValue::symbol("record"),
            vec![
                IOValue::new(label.clone()),
                IOValue::new(*field as i64),
                value.to_io_value(),
            ],
        ),
        WaitCondition::ToolResult { tag } => IOValue::record(
            IOValue::symbol("tool-result"),
            vec![IOValue::new(tag.clone())],
        ),
        WaitCondition::UserInput {
            prompt,
            tag,
            request_id,
        } => IOValue::record(
            IOValue::symbol("user-input"),
            vec![
                prompt.to_io_value(),
                IOValue::new(tag.clone().unwrap_or_default()),
                IOValue::new(request_id.clone().unwrap_or_default()),
            ],
        ),
    }
}

fn wait_condition_from_value(value: &IOValue) -> Option<WaitCondition> {
    if let Some(signal) = record_with_label(value, "signal") {
        let label = signal.field_string(0)?.to_string();
        Some(WaitCondition::Signal { label })
    } else if let Some(record_cond) = record_with_label(value, "record") {
        if record_cond.len() < 3 {
            return None;
        }
        let label = record_cond.field_string(0)?.to_string();
        let field_value = record_cond.field(1);
        let field_signed = field_value.as_signed_integer()?;
        let field_index = i64::try_from(field_signed.as_ref()).ok()?;
        if field_index < 0 {
            return None;
        }
        let expected = Value::from_io_value(&record_cond.field(2))?;
        Some(WaitCondition::RecordFieldEq {
            label,
            field: field_index as usize,
            value: expected,
        })
    } else if let Some(tool) = record_with_label(value, "tool-result") {
        let tag = tool.field_string(0)?.to_string();
        Some(WaitCondition::ToolResult { tag })
    } else if let Some(input) = record_with_label(value, "user-input") {
        if input.len() < 3 {
            return None;
        }
        let prompt_value = Value::from_io_value(&input.field(0))?;
        let tag = input.field_string(1)?.to_string();
        let request_id = input.field_string(2)?.to_string();
        Some(WaitCondition::UserInput {
            prompt: prompt_value,
            tag: if tag.is_empty() { None } else { Some(tag) },
            request_id: if request_id.is_empty() {
                None
            } else {
                Some(request_id)
            },
        })
    } else {
        None
    }
}

fn observer_assertion_value(
    id: &str,
    condition: &WaitCondition,
    handler: &ProgramRef,
    facet: &FacetId,
) -> IOValue {
    IOValue::record(
        IOValue::symbol(OBSERVER_RECORD_LABEL),
        vec![
            IOValue::new(id.to_string()),
            wait_condition_to_value(condition),
            program_ref_to_value(handler),
            IOValue::new(facet.0.to_string()),
        ],
    )
}

fn observer_snapshot_value(entry: &ObserverEntry) -> IOValue {
    let mut fields = vec![
        IOValue::new(entry.id.clone()),
        wait_condition_to_value(&entry.condition),
        program_ref_to_value(&entry.program_ref),
        IOValue::new(entry.facet.0.to_string()),
    ];
    fields.push(IOValue::new(entry.handle.0.to_string()));
    IOValue::record(IOValue::symbol("observer-entry"), fields)
}

fn observer_entry_from_value(
    value: &IOValue,
) -> Option<(String, WaitCondition, ProgramRef, FacetId, Option<Handle>)> {
    let record = record_with_label(value, "observer-entry")?;
    if record.len() < 4 {
        return None;
    }

    let observer_id = record.field_string(0)?.to_string();
    let condition = wait_condition_from_value(&record.field(1))?;
    let program_ref = program_ref_from_value(&record.field(2))?;
    let facet_uuid = record.field_string(3)?;
    let facet = FacetId::from_uuid(Uuid::parse_str(&facet_uuid).ok()?);

    let handle = if record.len() >= 5 {
        record
            .field_string(4)
            .and_then(|value| Uuid::parse_str(&value).ok())
            .map(Handle)
    } else {
        None
    };

    Some((observer_id, condition, program_ref, facet, handle))
}

impl Default for InterpreterEntity {
    fn default() -> Self {
        Self {
            definitions: Mutex::new(HashMap::new()),
            waiting: Mutex::new(HashMap::new()),
            actor_id: Mutex::new(None),
            observers: Mutex::new(HashMap::new()),
            spawn_capability_granted: Mutex::new(false),
        }
    }
}

impl InterpreterEntity {
    /// Register the interpreter entity with the provided catalog.
    pub fn register(catalog: &EntityCatalog) {
        catalog.register_hydratable("interpreter", |_config| Ok(InterpreterEntity::default()));
    }

    fn remember_actor(&self, actor: &ActorId) {
        let mut stored = self.actor_id.lock().unwrap();
        if stored.is_none() {
            *stored = Some(actor.clone());
        }
    }

    fn ensure_spawn_capability(&self, activation: &mut Activation) {
        let mut granted = self.spawn_capability_granted.lock().unwrap();
        if *granted {
            return;
        }

        let spec = CapabilitySpec {
            holder: activation.actor_id.clone(),
            holder_facet: activation.current_facet.clone(),
            target: None,
            kind: ENTITY_SPAWN_CAPABILITY_KIND.to_string(),
            attenuation: Vec::new(),
        };
        activation.grant_capability(spec);
        *granted = true;
    }

    fn register_observer(
        &self,
        activation: &mut Activation,
        spec: ObserverSpec,
    ) -> ActorResult<String> {
        let program = self.program_for_ref(&spec.handler)?;
        let id = Uuid::new_v4().to_string();
        let record = observer_assertion_value(&id, &spec.condition, &spec.handler, &spec.facet);
        let handle = Handle::new();
        activation.assert(handle.clone(), record);

        let entry = ObserverEntry {
            id: id.clone(),
            condition: spec.condition,
            program_ref: spec.handler,
            program,
            facet: spec.facet,
            handle,
        };

        self.observers.lock().unwrap().insert(id.clone(), entry);

        Ok(id)
    }

    fn notify_observers(&self, activation: &mut Activation, value: &IOValue) -> ActorResult<()> {
        let mut triggered = Vec::new();
        {
            let observers = self.observers.lock().unwrap();
            for entry in observers.values() {
                let status = WaitStatus::from_condition(&entry.condition);
                if wait_matches(&status, None, value) {
                    triggered.push(entry.clone());
                }
            }
        }

        for entry in triggered {
            self.invoke_observer(activation, entry)?;
        }

        Ok(())
    }

    fn invoke_observer(
        &self,
        activation: &mut Activation,
        entry: ObserverEntry,
    ) -> ActorResult<()> {
        let previous = std::mem::replace(&mut activation.current_facet, entry.facet.clone());
        let result = self.run_program(activation, entry.program_ref.clone(), entry.program.clone());
        activation.current_facet = previous;
        result
    }

    fn process_assertion(&self, activation: &mut Activation, value: &IOValue) -> ActorResult<()> {
        let mut matches = Vec::new();
        {
            let mut waiting_guard = self.waiting.lock().unwrap();
            for (instance_id, entry) in waiting_guard.iter_mut() {
                if wait_matches(&entry.wait, Some(instance_id), value) && !entry.resume_pending {
                    entry.resume_pending = true;
                    entry.matched_value = Some(value.clone());
                    matches.push((instance_id.clone(), entry.clone()));
                }
            }
        }

        if !matches.is_empty() {
            let target_actor = match self.actor_id.lock().unwrap().clone() {
                Some(id) => id,
                None => {
                    self.remember_actor(&activation.actor_id);
                    match self.actor_id.lock().unwrap().clone() {
                        Some(id) => id,
                        None => return Ok(()),
                    }
                }
            };

            for (instance_id, entry) in matches {
                let resume_payload = IOValue::record(
                    IOValue::symbol(RESUME_MESSAGE_LABEL),
                    vec![
                        IOValue::new(instance_id.clone()),
                        entry.wait.as_value(),
                        entry
                            .matched_value
                            .clone()
                            .unwrap_or_else(|| IOValue::symbol("none")),
                    ],
                );

                activation.send_message(target_actor.clone(), entry.facet.clone(), resume_payload);
            }
        }

        self.notify_observers(activation, value)?;
        Ok(())
    }
    fn handle_resume(
        &self,
        activation: &mut Activation,
        record: crate::util::io_value::RecordView<'_>,
    ) -> ActorResult<()> {
        self.remember_actor(&activation.actor_id);
        if record.len() < 1 {
            return Err(ActorError::InvalidActivation(
                "interpreter-resume requires instance id".into(),
            ));
        }

        let instance_id = record
            .field_string(0)
            .ok_or_else(|| ActorError::InvalidActivation("instance id must be a string".into()))?;

        let waiting_entry = {
            let mut waiting_guard = self.waiting.lock().unwrap();
            waiting_guard.remove(&instance_id).ok_or_else(|| {
                ActorError::InvalidActivation(format!(
                    "no waiting interpreter instance with id {instance_id}"
                ))
            })?
        };

        let expected_wait = waiting_entry.wait.clone();
        let wait_status = if record.len() > 1 {
            let wait_value = record.field(1);
            as_record(&wait_value)
                .and_then(WaitStatus::parse_record)
                .ok_or_else(|| {
                    ActorError::InvalidActivation("resume payload must describe a wait".into())
                })?
        } else {
            expected_wait.clone()
        };

        if wait_status != waiting_entry.wait {
            return Err(ActorError::InvalidActivation(
                "resume payload does not match recorded wait condition".into(),
            ));
        }

        let resume_value = if record.len() > 2 {
            let value = record.field(2);
            if value
                .as_symbol()
                .map(|sym| sym.as_ref() == "none")
                .unwrap_or(false)
            {
                None
            } else {
                Some(value)
            }
        } else {
            None
        };

        let matched_value = resume_value
            .clone()
            .or_else(|| waiting_entry.matched_value.clone());

        let mut ready_waits = Vec::new();
        let ready_condition = match &wait_status {
            WaitStatus::RecordFieldEq {
                label,
                field,
                value,
            } => WaitCondition::RecordFieldEq {
                label: label.clone(),
                field: *field,
                value: value.clone(),
            },
            WaitStatus::Signal { label } => WaitCondition::Signal {
                label: label.clone(),
            },
            WaitStatus::ToolResult { tag } => WaitCondition::ToolResult { tag: tag.clone() },
            WaitStatus::UserInput {
                prompt,
                tag,
                request_id,
            } => WaitCondition::UserInput {
                prompt: prompt.clone(),
                tag: Some(tag.clone()),
                request_id: Some(request_id.clone()),
            },
        };
        ready_waits.push(ReadyWait {
            condition: ready_condition,
            value: matched_value.clone(),
        });

        let program_ref = waiting_entry.program_ref.clone();
        let program = waiting_entry.program.clone();
        let mut program_clone = program.clone();
        let snapshot = waiting_entry.snapshot.clone();
        let status_handle = waiting_entry.handle.clone();
        let stored_assertions = waiting_entry.assertions.clone();
        let stored_facets = waiting_entry.facets.clone();
        let stored_prompts = waiting_entry.prompts.clone();

        let mut progress = InstanceProgress {
            state: program.states.first().map(|state| state.name.clone()),
            entry_pending: true,
            waiting: None,
            frame_depth: 0,
        };

        #[allow(unused_assignments)]
        let mut status = InstanceStatus::Running;
        let mut next_wait: Option<WaitingInstance> = None;
        let mut result_error: Option<ActorError> = None;
        let mut pending_wait_status: Option<WaitStatus> = None;
        let mut pending_wait_snapshot: Option<RuntimeSnapshot> = None;
        let mut pending_wait_handle: Option<Handle> = None;
        let mut pending_wait_assertions: Option<Vec<RecordedAssertion>> = None;
        let mut pending_wait_facets: Option<Vec<FacetId>> = None;
        let mut pending_wait_prompts: Option<Vec<RecordedPrompt>> = None;
        let mut wait_assertion: Option<(Handle, IOValue)> = None;

        let host = ActivationHost::with_ready(
            activation,
            instance_id.clone(),
            &program.roles,
            ready_waits,
            stored_assertions,
            stored_facets,
            stored_prompts,
            waiting_entry.prompt_counter,
        );
        let mut runtime = InterpreterRuntime::from_snapshot(host, program.clone(), snapshot);

        progress.state = runtime.current_state_name();
        progress.entry_pending = runtime.entry_pending();
        progress.waiting = runtime.waiting_condition().map(WaitStatus::from_condition);
        progress.frame_depth = runtime.frame_depth();

        loop {
            match runtime.tick() {
                Ok(RuntimeEvent::Progress) => {
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = runtime.waiting_condition().map(WaitStatus::from_condition);
                    progress.frame_depth = runtime.frame_depth();
                }
                Ok(RuntimeEvent::Transition { .. }) => {
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = runtime.waiting_condition().map(WaitStatus::from_condition);
                    progress.frame_depth = runtime.frame_depth();
                }
                Ok(RuntimeEvent::Waiting(wait)) => {
                    let wait_status = WaitStatus::from_condition(&wait);
                    let snapshot = runtime.snapshot();
                    let wait_handle = Handle::new();
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = Some(wait_status.clone());
                    progress.frame_depth = runtime.frame_depth();
                    status = InstanceStatus::Waiting(wait_status.clone());
                    pending_wait_status = Some(wait_status);
                    pending_wait_snapshot = Some(snapshot);
                    pending_wait_handle = Some(wait_handle);
                    pending_wait_assertions = Some(runtime.host().snapshot_assertions());
                    pending_wait_facets = Some(runtime.host().snapshot_facets());
                    pending_wait_prompts = Some(runtime.host().snapshot_prompts());
                    break;
                }
                Ok(RuntimeEvent::Completed) => {
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    status = InstanceStatus::Completed;
                    break;
                }
                Err(RuntimeError::Host(err)) => {
                    let message = err.to_string();
                    status = InstanceStatus::Failed(message.clone());
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    result_error = Some(err);
                    break;
                }
                Err(RuntimeError::UnknownState(state)) => {
                    let message = format!("unknown state: {state}");
                    status = InstanceStatus::Failed(message.clone());
                    progress.state = Some(state);
                    progress.entry_pending = false;
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    result_error = Some(ActorError::InvalidActivation(message));
                    break;
                }
                Err(RuntimeError::NoStates) => {
                    let message = "program must define at least one state".to_string();
                    status = InstanceStatus::Failed(message.clone());
                    progress.state = None;
                    progress.entry_pending = false;
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    result_error = Some(ActorError::InvalidActivation(message));
                    break;
                }
            }
        }

        let updated_roles = runtime.host().roles_snapshot();
        runtime.program_mut().roles = updated_roles.clone();
        program_clone.roles = updated_roles.clone();

        let observer_specs = runtime.host_mut().drain_observers();

        if let (Some(wait_status), Some(snapshot), Some(wait_handle)) = (
            pending_wait_status,
            pending_wait_snapshot,
            pending_wait_handle,
        ) {
            let facet = runtime.host().current_facet();
            let wait_record = wait_record_to_value(&WaitRecord {
                instance_id: instance_id.clone(),
                facet: facet.clone(),
                wait_status: wait_status.clone(),
            });
            wait_assertion = Some((wait_handle.clone(), wait_record));
            let assertions = pending_wait_assertions.take().unwrap_or_default();
            let facets = pending_wait_facets.take().unwrap_or_default();
            let prompts = pending_wait_prompts.take().unwrap_or_default();
            next_wait = Some(WaitingInstance {
                program_ref: program_ref.clone(),
                program: program_clone.clone(),
                snapshot,
                wait: wait_status,
                handle: status_handle.clone(),
                wait_handle,
                facet,
                assertions,
                facets,
                prompt_counter: runtime.host().prompt_counter_value(),
                prompts,
                resume_pending: false,
                matched_value: None,
            });
        }

        drop(runtime);

        for spec in observer_specs {
            self.register_observer(activation, spec)?;
        }

        if let Some((handle, value)) = wait_assertion.take() {
            activation.assert(handle, value);
        }

        activation.retract(waiting_entry.wait_handle.clone());
        let final_record = InstanceRecord {
            instance_id: instance_id.clone(),
            program: program_ref.clone(),
            program_name: program_clone.name.clone(),
            state: progress.state.clone(),
            status: status.clone(),
            progress: Some(progress.clone()),
            roles: program_clone.roles.clone(),
        };

        if let Some(entry) = next_wait {
            let mut waiting_guard = self.waiting.lock().unwrap();
            waiting_guard.insert(instance_id.clone(), entry);
        }

        activation.assert(status_handle, final_record.to_value());

        if let Some(err) = result_error {
            Err(err)
        } else {
            Ok(())
        }
    }
}

fn wait_matches(wait: &WaitStatus, instance_id: Option<&str>, value: &IOValue) -> bool {
    match wait {
        WaitStatus::Signal { label } => value
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref() == label)
            .unwrap_or(false),
        WaitStatus::RecordFieldEq {
            label,
            field,
            value: expected,
        } => {
            if value
                .label()
                .as_symbol()
                .map(|sym| sym.as_ref() == label)
                .unwrap_or(false)
                && value.len() > *field
            {
                if let Some(actual) = Value::from_io_value(&IOValue::from(value.index(*field))) {
                    &actual == expected
                } else {
                    false
                }
            } else {
                false
            }
        }
        WaitStatus::ToolResult { tag } => {
            if let Some(record) = record_with_label(value, TOOL_RESULT_RECORD_LABEL) {
                if record.len() < 2 {
                    return false;
                }

                let matches_tag = record
                    .field_string(1)
                    .map(|candidate| candidate == *tag)
                    .unwrap_or(false);

                let matches_instance = match instance_id {
                    Some(id) => record
                        .field_string(0)
                        .map(|candidate| candidate == id)
                        .unwrap_or(false),
                    None => true,
                };

                matches_tag && matches_instance
            } else {
                false
            }
        }
        WaitStatus::UserInput {
            request_id, tag, ..
        } => {
            if let Some(response) = input_response_from_value(value) {
                let matches_instance = instance_id
                    .map(|id| response.instance_id == id)
                    .unwrap_or(true);
                matches_instance && response.request_id == *request_id && response.tag == *tag
            } else {
                false
            }
        }
    }
}

impl Entity for InterpreterEntity {
    fn on_message(&self, activation: &mut Activation, payload: &IOValue) -> ActorResult<()> {
        self.ensure_spawn_capability(activation);
        if let Some(record) = record_with_label(payload, NOTIFY_MESSAGE_LABEL) {
            if record.len() > 0 {
                let assertion = record.field(0);
                self.process_assertion(activation, &assertion)?;
            }
            Ok(())
        } else if let Some(record) = record_with_label(payload, DEFINE_MESSAGE_LABEL) {
            self.handle_define(activation, record)
        } else if let Some(record) = record_with_label(payload, RUN_MESSAGE_LABEL) {
            self.handle_run(activation, record)
        } else if let Some(record) = record_with_label(payload, RESUME_MESSAGE_LABEL) {
            self.handle_resume(activation, record)
        } else {
            Ok(())
        }
    }
}

impl HydratableEntity for InterpreterEntity {
    fn snapshot_state(&self) -> IOValue {
        let definitions_guard = self.definitions.lock().unwrap();
        let definition_records: Vec<_> = definitions_guard
            .iter()
            .map(|(id, stored)| {
                DefinitionRecord {
                    definition_id: id.clone(),
                    program_name: stored.program.name.clone(),
                    source: stored.source.clone(),
                }
                .to_value()
            })
            .collect();

        let definitions_value = IOValue::record(IOValue::symbol("definitions"), definition_records);

        let waiting_guard = self.waiting.lock().unwrap();
        let waiting_records: Vec<_> = waiting_guard
            .iter()
            .map(|(instance_id, entry)| {
                let assertion_values: Vec<_> = entry
                    .assertions
                    .iter()
                    .map(|assertion| assertion_to_value(assertion))
                    .collect();
                let facet_values: Vec<_> = entry
                    .facets
                    .iter()
                    .map(|facet| facet_to_value(facet))
                    .collect();
                let prompt_values: Vec<_> = entry
                    .prompts
                    .iter()
                    .map(|prompt| prompt_snapshot_to_value(prompt))
                    .collect();
                IOValue::record(
                    IOValue::symbol("waiting-instance"),
                    vec![
                        IOValue::new(instance_id.clone()),
                        entry.program_ref.to_value(),
                        runtime_snapshot_to_value(&entry.snapshot),
                        entry.wait.as_value(),
                        IOValue::new(entry.handle.0.to_string()),
                        IOValue::new(entry.wait_handle.0.to_string()),
                        IOValue::new(entry.facet.0.to_string()),
                        IOValue::record(IOValue::symbol("assertions"), assertion_values),
                        IOValue::record(IOValue::symbol("facets"), facet_values),
                        IOValue::record(IOValue::symbol("prompts"), prompt_values),
                        IOValue::new(entry.prompt_counter as i64),
                        IOValue::symbol(if entry.resume_pending {
                            "true"
                        } else {
                            "false"
                        }),
                        entry
                            .matched_value
                            .clone()
                            .unwrap_or_else(|| IOValue::symbol("none")),
                    ],
                )
            })
            .collect();

        let waiting_value = IOValue::record(IOValue::symbol("waiting"), waiting_records);

        let observers_guard = self.observers.lock().unwrap();
        let observer_records: Vec<_> = observers_guard
            .values()
            .map(|entry| observer_snapshot_value(entry))
            .collect();

        let observers_value = IOValue::record(IOValue::symbol("observers"), observer_records);

        let actor_field = self.actor_id.lock().unwrap().as_ref().map(|actor| {
            IOValue::record(
                IOValue::symbol("actor-id"),
                vec![IOValue::new(actor.0.to_string())],
            )
        });

        let mut fields = vec![definitions_value, waiting_value, observers_value];
        if let Some(actor_value) = actor_field {
            fields.push(actor_value);
        }

        IOValue::record(IOValue::symbol("interpreter-state"), fields)
    }

    fn restore_state(&mut self, state: &IOValue) -> ActorResult<()> {
        let mut definitions_guard = self.definitions.lock().unwrap();
        definitions_guard.clear();

        let mut reconstructed_waiting: Vec<(String, WaitingInstance)> = Vec::new();
        let mut restored_actor: Option<ActorId> = None;
        let mut restored_observers: Vec<(
            String,
            WaitCondition,
            ProgramRef,
            FacetId,
            Option<Handle>,
        )> = Vec::new();

        if let Some(record) = as_record(state) {
            if record.has_label("interpreter-state") {
                for index in 0..record.len() {
                    let entry = record.field(index);
                    if let Some(defs) = record_with_label(&entry, "definitions") {
                        for def_index in 0..defs.len() {
                            let value = defs.field(def_index);
                            if let Some(definition) = DefinitionRecord::parse(&value) {
                                let program = parse_program(&definition.source).map_err(|err| {
                                    ActorError::InvalidActivation(format!("parse error: {err}"))
                                })?;
                                let ir = build_ir(&program).map_err(|err| {
                                    ActorError::InvalidActivation(format!(
                                        "validation error: {err}"
                                    ))
                                })?;
                                definitions_guard.insert(
                                    definition.definition_id.clone(),
                                    StoredDefinition::new(ir, definition.source.clone()),
                                );
                            }
                        }
                    } else if let Some(waiting) = record_with_label(&entry, "waiting") {
                        for wait_index in 0..waiting.len() {
                            let wait_entry = waiting.field(wait_index);
                            if let Some(wait_view) =
                                record_with_label(&wait_entry, "waiting-instance")
                            {
                                if wait_view.len() < 7 {
                                    continue;
                                }

                                let instance_id = match wait_view.field_string(0) {
                                    Some(id) => id,
                                    None => continue,
                                };

                                let program_ref = match ProgramRef::parse(&wait_view.field(1)) {
                                    Some(r) => r,
                                    None => continue,
                                };

                                let snapshot =
                                    match runtime_snapshot_from_value(&wait_view.field(2)) {
                                        Some(snapshot) => snapshot,
                                        None => continue,
                                    };

                                let wait_status = match as_record(&wait_view.field(3))
                                    .and_then(WaitStatus::parse_record)
                                {
                                    Some(status) => status,
                                    None => continue,
                                };

                                let handle_id = match wait_view.field_string(4) {
                                    Some(value) => value,
                                    None => continue,
                                };

                                let wait_handle_id = match wait_view.field_string(5) {
                                    Some(value) => value,
                                    None => continue,
                                };

                                let facet_id = match wait_view.field_string(6) {
                                    Some(value) => value,
                                    None => continue,
                                };

                                let handle_uuid = match uuid::Uuid::parse_str(&handle_id) {
                                    Ok(uuid) => uuid,
                                    Err(_) => continue,
                                };

                                let wait_handle_uuid = match uuid::Uuid::parse_str(&wait_handle_id)
                                {
                                    Ok(uuid) => uuid,
                                    Err(_) => continue,
                                };

                                let facet_uuid = match uuid::Uuid::parse_str(&facet_id) {
                                    Ok(uuid) => uuid,
                                    Err(_) => continue,
                                };

                                let program = match self.program_for_ref(&program_ref) {
                                    Ok(program) => program,
                                    Err(_) => continue,
                                };

                                let assertions = if wait_view.len() > 7 {
                                    let assertions_value = wait_view.field(7);
                                    if let Some(assertions_view) =
                                        record_with_label(&assertions_value, "assertions")
                                    {
                                        let mut collected = Vec::new();
                                        for assertion_index in 0..assertions_view.len() {
                                            let assertion_value =
                                                assertions_view.field(assertion_index);
                                            if let Some(parsed) =
                                                assertion_from_value(&assertion_value)
                                            {
                                                collected.push(parsed);
                                            }
                                        }
                                        collected
                                    } else {
                                        Vec::new()
                                    }
                                } else {
                                    Vec::new()
                                };

                                let facets = if wait_view.len() > 8 {
                                    let facets_value = wait_view.field(8);
                                    facets_from_value(&facets_value)
                                } else {
                                    Vec::new()
                                };

                                let prompts = if wait_view.len() > 9 {
                                    let prompts_value = wait_view.field(9);
                                    if let Some(prompts_view) =
                                        record_with_label(&prompts_value, "prompts")
                                    {
                                        let mut collected = Vec::new();
                                        for prompt_index in 0..prompts_view.len() {
                                            let prompt_value = prompts_view.field(prompt_index);
                                            if let Some(parsed) =
                                                prompt_snapshot_from_value(&prompt_value)
                                            {
                                                collected.push(parsed);
                                            }
                                        }
                                        collected
                                    } else {
                                        Vec::new()
                                    }
                                } else {
                                    Vec::new()
                                };

                                let prompt_counter = if wait_view.len() > 10 {
                                    let value = wait_view.field(10);
                                    value
                                        .as_signed_integer()
                                        .and_then(|num| i64::try_from(num.as_ref()).ok())
                                        .map(|n| if n < 0 { 0 } else { n as u64 })
                                        .unwrap_or(0)
                                } else {
                                    0
                                };

                                let resume_pending = if wait_view.len() > 11 {
                                    wait_view
                                        .field(11)
                                        .as_symbol()
                                        .map(|sym| sym.as_ref() == "true")
                                        .unwrap_or(false)
                                } else {
                                    false
                                };

                                let matched_value = if wait_view.len() > 12 {
                                    let value = wait_view.field(12);
                                    if value
                                        .as_symbol()
                                        .map(|sym| sym.as_ref() == "none")
                                        .unwrap_or(false)
                                    {
                                        None
                                    } else {
                                        Some(value)
                                    }
                                } else {
                                    None
                                };

                                reconstructed_waiting.push((
                                    instance_id,
                                    WaitingInstance {
                                        program_ref,
                                        program,
                                        snapshot,
                                        wait: wait_status,
                                        handle: Handle(handle_uuid),
                                        wait_handle: Handle(wait_handle_uuid),
                                        facet: FacetId::from_uuid(facet_uuid),
                                        assertions,
                                        facets,
                                        prompt_counter,
                                        prompts,
                                        resume_pending,
                                        matched_value,
                                    },
                                ));
                            }
                        }
                    } else if let Some(observers) = record_with_label(&entry, "observers") {
                        for observer_index in 0..observers.len() {
                            let observer_entry = observers.field(observer_index);
                            if let Some((observer_id, condition, program_ref, facet, handle)) =
                                observer_entry_from_value(&observer_entry)
                            {
                                restored_observers.push((
                                    observer_id,
                                    condition,
                                    program_ref,
                                    facet,
                                    handle,
                                ));
                            }
                        }
                    } else if let Some(actor_value) = record_with_label(&entry, "actor-id") {
                        if actor_value.len() > 0 {
                            if let Some(actor_str) = actor_value.field_string(0) {
                                if let Ok(uuid) = uuid::Uuid::parse_str(&actor_str) {
                                    restored_actor = Some(ActorId::from_uuid(uuid));
                                }
                            }
                        }
                    }
                }

                let mut waiting_guard = self.waiting.lock().unwrap();
                waiting_guard.clear();
                for (instance_id, entry) in reconstructed_waiting {
                    waiting_guard.insert(instance_id, entry);
                }

                if let Some(actor) = restored_actor {
                    *self.actor_id.lock().unwrap() = Some(actor);
                }

                drop(definitions_guard);

                let mut observers_guard = self.observers.lock().unwrap();
                observers_guard.clear();
                for (observer_id, condition, program_ref, facet, handle) in restored_observers {
                    let program = self.program_for_ref(&program_ref)?;
                    let key = observer_id.clone();
                    observers_guard.insert(
                        key,
                        ObserverEntry {
                            id: observer_id,
                            condition,
                            program_ref,
                            program,
                            facet,
                            handle: handle.unwrap_or_else(Handle::new),
                        },
                    );
                }

                return Ok(());
            }
        }

        if let Some(definition) = DefinitionRecord::parse(state) {
            let program = parse_program(&definition.source)
                .map_err(|err| ActorError::InvalidActivation(format!("parse error: {err}")))?;
            let ir = build_ir(&program)
                .map_err(|err| ActorError::InvalidActivation(format!("validation error: {err}")))?;
            definitions_guard.insert(
                definition.definition_id.clone(),
                StoredDefinition::new(ir, definition.source.clone()),
            );
        }

        Ok(())
    }
}

impl InterpreterEntity {
    fn handle_define(
        &self,
        activation: &mut Activation,
        record: crate::util::io_value::RecordView<'_>,
    ) -> ActorResult<()> {
        self.remember_actor(&activation.actor_id);
        if record.len() == 0 {
            return Err(ActorError::InvalidActivation(
                "interpreter-define requires program source".into(),
            ));
        }

        let source = record
            .field_string(0)
            .ok_or_else(|| ActorError::InvalidActivation("program must be a string".into()))?;

        let program = parse_program(&source)
            .map_err(|err| ActorError::InvalidActivation(format!("parse error: {err}")))?;
        let ir = build_ir(&program)
            .map_err(|err| ActorError::InvalidActivation(format!("validation error: {err}")))?;

        let definition_id = Uuid::new_v4().to_string();
        self.definitions.lock().unwrap().insert(
            definition_id.clone(),
            StoredDefinition::new(ir.clone(), source.clone()),
        );

        let definition_record = DefinitionRecord {
            definition_id: definition_id.clone(),
            program_name: program.name.clone(),
            source: source.clone(),
        };
        activation.assert(Handle::new(), definition_record.to_value());

        let acknowledgement = IOValue::record(
            IOValue::symbol("interpreter-defined"),
            vec![IOValue::new(definition_id)],
        );
        activation.assert(Handle::new(), acknowledgement);
        Ok(())
    }

    fn handle_run(
        &self,
        activation: &mut Activation,
        record: crate::util::io_value::RecordView<'_>,
    ) -> ActorResult<()> {
        self.remember_actor(&activation.actor_id);
        if record.len() == 0 {
            return Err(ActorError::InvalidActivation(
                "interpreter-run requires arguments".into(),
            ));
        }

        let (program_ref, ir) = if let Some(sym) = record.field_symbol(0) {
            if sym == "definition" {
                if record.len() < 2 {
                    return Err(ActorError::InvalidActivation(
                        "interpreter-run :definition requires an id".into(),
                    ));
                }
                let id = record.field_string(1).ok_or_else(|| {
                    ActorError::InvalidActivation("definition id must be string".into())
                })?;
                let stored = self
                    .definitions
                    .lock()
                    .unwrap()
                    .get(&id)
                    .cloned()
                    .ok_or_else(|| {
                        ActorError::InvalidActivation(format!("unknown definition id: {id}"))
                    })?;
                (ProgramRef::Definition(id), stored.program)
            } else {
                return Err(ActorError::InvalidActivation(format!(
                    "unknown interpreter-run option: {sym}"
                )));
            }
        } else {
            let source = record
                .field_string(0)
                .ok_or_else(|| ActorError::InvalidActivation("program must be a string".into()))?;
            let program = parse_program(&source)
                .map_err(|err| ActorError::InvalidActivation(format!("parse error: {err}")))?;
            (
                ProgramRef::Inline(source.clone()),
                build_ir(&program).map_err(|err| {
                    ActorError::InvalidActivation(format!("validation error: {err}"))
                })?,
            )
        };

        self.run_program(activation, program_ref, ir)
    }

    fn program_for_ref(&self, program_ref: &ProgramRef) -> ActorResult<ProgramIr> {
        match program_ref {
            ProgramRef::Definition(id) => {
                let definitions = self.definitions.lock().unwrap();
                let stored = definitions.get(id).ok_or_else(|| {
                    ActorError::InvalidActivation(format!("unknown definition id: {id}"))
                })?;
                Ok(stored.program.clone())
            }
            ProgramRef::Inline(source) => {
                let program = parse_program(source)
                    .map_err(|err| ActorError::InvalidActivation(format!("parse error: {err}")))?;
                build_ir(&program).map_err(|err| {
                    ActorError::InvalidActivation(format!("validation error: {err}"))
                })
            }
        }
    }

    fn run_program(
        &self,
        activation: &mut Activation,
        program_ref: ProgramRef,
        program: ProgramIr,
    ) -> ActorResult<()> {
        let mut program_clone = program.clone();
        let program_ref_clone = program_ref.clone();

        let instance_uuid = Uuid::new_v4();
        let instance_id = instance_uuid.to_string();

        let status_handle = Handle::new();

        let mut progress = InstanceProgress {
            state: program_clone.states.first().map(|state| state.name.clone()),
            entry_pending: true,
            waiting: None,
            frame_depth: 0,
        };

        let mut status = InstanceStatus::Running;
        let mut waiting_entry: Option<WaitingInstance> = None;
        let mut result_error: Option<ActorError> = None;
        let mut pending_wait_status: Option<WaitStatus> = None;
        let mut pending_wait_snapshot: Option<RuntimeSnapshot> = None;
        let mut pending_wait_handle: Option<Handle> = None;
        let mut pending_wait_assertions: Option<Vec<RecordedAssertion>> = None;
        let mut pending_wait_facets: Option<Vec<FacetId>> = None;
        let mut pending_wait_prompts: Option<Vec<RecordedPrompt>> = None;
        let mut wait_assertion: Option<(Handle, IOValue)> = None;

        let running_record = InstanceRecord {
            instance_id: instance_id.clone(),
            program: program_ref_clone.clone(),
            program_name: program_clone.name.clone(),
            state: progress.state.clone(),
            status: status.clone(),
            progress: Some(progress.clone()),
            roles: program_clone.roles.clone(),
        };
        activation.assert(status_handle.clone(), running_record.to_value());

        let host = ActivationHost::new(activation, instance_id.clone(), &program_clone.roles);
        let mut runtime = InterpreterRuntime::new(host, program);

        progress.state = runtime.current_state_name();
        progress.entry_pending = runtime.entry_pending();
        progress.waiting = runtime.waiting_condition().map(WaitStatus::from_condition);
        progress.frame_depth = runtime.frame_depth();

        loop {
            match runtime.tick() {
                Ok(RuntimeEvent::Progress) => {
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = runtime.waiting_condition().map(WaitStatus::from_condition);
                    progress.frame_depth = runtime.frame_depth();
                }
                Ok(RuntimeEvent::Transition { .. }) => {
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = runtime.waiting_condition().map(WaitStatus::from_condition);
                    progress.frame_depth = runtime.frame_depth();
                }
                Ok(RuntimeEvent::Waiting(wait)) => {
                    let wait_status = WaitStatus::from_condition(&wait);
                    let snapshot = runtime.snapshot();
                    let wait_handle = Handle::new();
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = Some(wait_status.clone());
                    progress.frame_depth = runtime.frame_depth();
                    status = InstanceStatus::Waiting(wait_status.clone());
                    pending_wait_status = Some(wait_status);
                    pending_wait_snapshot = Some(snapshot);
                    pending_wait_handle = Some(wait_handle);
                    pending_wait_assertions = Some(runtime.host().snapshot_assertions());
                    pending_wait_facets = Some(runtime.host().snapshot_facets());
                    pending_wait_prompts = Some(runtime.host().snapshot_prompts());
                    break;
                }
                Ok(RuntimeEvent::Completed) => {
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    status = InstanceStatus::Completed;
                    break;
                }
                Err(RuntimeError::Host(err)) => {
                    let message = err.to_string();
                    status = InstanceStatus::Failed(message.clone());
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    result_error = Some(err);
                    break;
                }
                Err(RuntimeError::UnknownState(state)) => {
                    let message = format!("unknown state: {state}");
                    status = InstanceStatus::Failed(message.clone());
                    progress.state = Some(state);
                    progress.entry_pending = false;
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    result_error = Some(ActorError::InvalidActivation(message));
                    break;
                }
                Err(RuntimeError::NoStates) => {
                    let message = "program must define at least one state".to_string();
                    status = InstanceStatus::Failed(message.clone());
                    progress.state = None;
                    progress.entry_pending = false;
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    result_error = Some(ActorError::InvalidActivation(message));
                    break;
                }
            }
        }

        let updated_roles = runtime.host().roles_snapshot();
        runtime.program_mut().roles = updated_roles.clone();
        program_clone.roles = updated_roles.clone();

        let observer_specs = runtime.host_mut().drain_observers();

        if let (Some(wait_status), Some(snapshot), Some(wait_handle)) = (
            pending_wait_status,
            pending_wait_snapshot,
            pending_wait_handle,
        ) {
            let facet = runtime.host().current_facet();
            let wait_record = wait_record_to_value(&WaitRecord {
                instance_id: instance_id.clone(),
                facet: facet.clone(),
                wait_status: wait_status.clone(),
            });
            wait_assertion = Some((wait_handle.clone(), wait_record));
            let assertions = pending_wait_assertions.take().unwrap_or_default();
            let facets = pending_wait_facets.take().unwrap_or_default();
            let prompts = pending_wait_prompts.take().unwrap_or_default();
            waiting_entry = Some(WaitingInstance {
                program_ref: program_ref_clone.clone(),
                program: program_clone.clone(),
                snapshot,
                wait: wait_status,
                handle: status_handle.clone(),
                wait_handle,
                facet,
                assertions,
                facets,
                prompt_counter: runtime.host().prompt_counter_value(),
                prompts,
                resume_pending: false,
                matched_value: None,
            });
        }

        let final_record = InstanceRecord {
            instance_id: instance_id.clone(),
            program: program_ref_clone.clone(),
            program_name: program_clone.name.clone(),
            state: progress.state.clone(),
            status: status.clone(),
            progress: Some(progress.clone()),
            roles: program_clone.roles.clone(),
        };

        let cleanup_facets = if waiting_entry.is_some() {
            Vec::new()
        } else {
            runtime.host_mut().drain_tracked_facets()
        };
        let final_record_value = final_record.to_value();

        drop(runtime);

        for spec in observer_specs {
            self.register_observer(activation, spec)?;
        }

        if let Some((handle, value)) = wait_assertion {
            activation.assert(handle, value);
        }

        let is_waiting = waiting_entry.is_some();
        {
            let mut waiting_guard = self.waiting.lock().unwrap();
            if let Some(entry) = waiting_entry.take() {
                waiting_guard.insert(instance_id.clone(), entry);
            } else {
                waiting_guard.remove(&instance_id);
            }
        }

        if !is_waiting {
            for (facet, handles) in cleanup_facets {
                activation.terminate_facet(facet.clone());
                for handle in handles {
                    activation.retract(handle);
                }
            }
        }

        activation.assert(status_handle, final_record_value);

        if let Some(err) = result_error {
            Err(err)
        } else {
            Ok(())
        }
    }
}

struct ActivationHost<'a> {
    activation: &'a mut Activation,
    satisfied: Vec<ReadyWait>,
    assertions: HashMap<IOValue, Vec<Handle>>,
    facets: Vec<FacetId>,
    observers: Vec<ObserverSpec>,
    prompts: Vec<PromptRecord>,
    prompt_counter: u64,
    instance_id: String,
    roles: HashMap<String, RoleBinding>,
    last_ready_value: Option<IOValue>,
}

#[derive(Clone)]
struct ObserverSpec {
    condition: WaitCondition,
    handler: ProgramRef,
    facet: FacetId,
}

#[derive(Clone)]
struct ReadyWait {
    condition: WaitCondition,
    value: Option<IOValue>,
}

#[derive(Clone)]
struct ToolInvocation {
    role: String,
    capability_alias: String,
    capability_id: Uuid,
    payload: IOValue,
    tag: String,
    role_properties: Option<IOValue>,
}

impl ToolInvocation {
    fn new(
        role: String,
        capability_alias: String,
        capability_id: Uuid,
        payload: IOValue,
        tag: String,
        role_properties: Option<IOValue>,
    ) -> Self {
        Self {
            role,
            capability_alias,
            capability_id,
            payload,
            tag,
            role_properties,
        }
    }

    fn request_record(&self, instance_id: &str) -> IOValue {
        let mut fields = vec![
            IOValue::new(instance_id.to_string()),
            IOValue::new(self.tag.clone()),
            IOValue::new(self.role.clone()),
            IOValue::new(self.capability_alias.clone()),
            IOValue::new(self.capability_id.to_string()),
            self.payload.clone(),
        ];

        if let Some(props) = &self.role_properties {
            fields.push(props.clone());
        }

        IOValue::record(IOValue::symbol(TOOL_REQUEST_RECORD_LABEL), fields)
    }

    fn log_record(&self) -> IOValue {
        IOValue::record(
            IOValue::symbol(LOG_RECORD_LABEL),
            vec![IOValue::new(format!(
                "invoke-tool role={} capability={} tag={}",
                self.role, self.capability_alias, self.tag
            ))],
        )
    }

    fn completion(
        &self,
        actor: &ActorId,
        facet: &FacetId,
        instance_id: &str,
    ) -> CapabilityCompletion {
        CapabilityCompletion {
            origin_actor: actor.clone(),
            origin_facet: facet.clone(),
            instance_id: instance_id.to_string(),
            role: self.role.clone(),
            capability_alias: self.capability_alias.clone(),
            tag: self.tag.clone(),
            role_properties: self.role_properties.clone(),
        }
    }

    fn payload(&self) -> IOValue {
        self.payload.clone()
    }

    fn capability_id(&self) -> Uuid {
        self.capability_id
    }
}

impl<'a> ActivationHost<'a> {
    fn new(activation: &'a mut Activation, instance_id: String, roles: &[RoleBinding]) -> Self {
        let mut role_map = HashMap::new();
        for binding in roles {
            role_map.insert(binding.name.clone(), binding.clone());
        }
        Self {
            activation,
            satisfied: Vec::new(),
            assertions: HashMap::new(),
            facets: Vec::new(),
            observers: Vec::new(),
            prompts: Vec::new(),
            prompt_counter: 0,
            instance_id,
            roles: role_map,
            last_ready_value: None,
        }
    }

    fn with_ready(
        activation: &'a mut Activation,
        instance_id: String,
        roles: &[RoleBinding],
        satisfied: Vec<ReadyWait>,
        assertions: Vec<RecordedAssertion>,
        facets: Vec<FacetId>,
        prompts: Vec<RecordedPrompt>,
        prompt_counter: u64,
    ) -> Self {
        let mut host = Self::new(activation, instance_id, roles);
        host.satisfied = satisfied;
        host.restore_assertions(&assertions);
        host.restore_facets(&facets);
        host.restore_prompts(&prompts);
        host.restore_prompt_counter(prompt_counter);
        host
    }

    fn condition_matches(a: &WaitCondition, b: &WaitCondition) -> bool {
        match (a, b) {
            (
                WaitCondition::RecordFieldEq {
                    label: a_label,
                    field: a_field,
                    value: a_value,
                },
                WaitCondition::RecordFieldEq {
                    label: b_label,
                    field: b_field,
                    value: b_value,
                },
            ) => a_label == b_label && a_field == b_field && a_value == b_value,
            (WaitCondition::Signal { label: a }, WaitCondition::Signal { label: b }) => a == b,
            (WaitCondition::ToolResult { tag: a }, WaitCondition::ToolResult { tag: b }) => a == b,
            (
                WaitCondition::UserInput {
                    request_id: a_id,
                    tag: a_tag,
                    ..
                },
                WaitCondition::UserInput {
                    request_id: b_id,
                    tag: b_tag,
                    ..
                },
            ) => a_id == b_id && a_tag == b_tag,
            _ => false,
        }
    }

    fn snapshot_assertions(&self) -> Vec<RecordedAssertion> {
        let mut entries = Vec::new();
        for (value, handles) in &self.assertions {
            for handle in handles {
                entries.push(RecordedAssertion::new(value.clone(), handle.clone()));
            }
        }
        entries.sort_by(|a, b| a.handle.0.cmp(&b.handle.0));
        entries
    }

    fn restore_assertions(&mut self, assertions: &[RecordedAssertion]) {
        self.assertions.clear();
        for assertion in assertions {
            self.assertions
                .entry(assertion.value.clone())
                .or_default()
                .push(assertion.handle.clone());
        }
    }

    fn snapshot_facets(&self) -> Vec<FacetId> {
        self.facets.clone()
    }

    fn restore_facets(&mut self, facets: &[FacetId]) {
        self.facets = facets.to_vec();
    }

    fn snapshot_prompts(&self) -> Vec<RecordedPrompt> {
        let mut entries: Vec<_> = self.prompts.iter().map(PromptRecord::to_snapshot).collect();
        entries.sort_by(|a, b| a.handle.0.cmp(&b.handle.0));
        entries
    }

    fn restore_prompts(&mut self, prompts: &[RecordedPrompt]) {
        self.prompts.clear();
        self.prompt_counter = 0;
        for prompt in prompts {
            self.bump_prompt_counter_for(&prompt.request_id);
            self.prompts.push(PromptRecord::from(prompt.clone()));
        }
    }

    fn bump_prompt_counter_for(&mut self, request_id: &str) {
        if let Some(segment) = request_id.rsplit(':').next() {
            if let Ok(value) = segment.parse::<u64>() {
                if value > self.prompt_counter {
                    self.prompt_counter = value;
                }
            }
        }
    }

    fn next_prompt_request_id(&mut self) -> String {
        self.prompt_counter = self.prompt_counter.saturating_add(1);
        format!("{}:input:{}", self.instance_id, self.prompt_counter)
    }

    fn remove_prompt_by_request(&mut self, request_id: &str) -> Option<PromptRecord> {
        if let Some(pos) = self
            .prompts
            .iter()
            .position(|prompt| prompt.request_id == request_id)
        {
            Some(self.prompts.remove(pos))
        } else {
            None
        }
    }

    fn track_facet(&mut self, facet: FacetId) {
        if !self.facets.iter().any(|existing| existing == &facet) {
            self.facets.push(facet);
        }
    }

    fn current_facet(&self) -> FacetId {
        self.activation.current_facet.clone()
    }

    fn assert_facet_record(&mut self, facet: &FacetId) {
        let record = IOValue::record(
            IOValue::symbol("interpreter-facet"),
            vec![IOValue::new(facet.0.to_string())],
        );
        let handle = Handle::new();
        self.activation.assert(handle.clone(), record.clone());
        self.assertions.entry(record).or_default().push(handle);
    }

    fn retract_facet_assertion(&mut self, facet: &FacetId) {
        let record = IOValue::record(
            IOValue::symbol("interpreter-facet"),
            vec![IOValue::new(facet.0.to_string())],
        );
        if let Some(handles) = self.assertions.get_mut(&record) {
            if let Some(handle) = handles.pop() {
                self.activation.retract(handle.clone());
            }
            if handles.is_empty() {
                self.assertions.remove(&record);
            }
        }
    }

    fn drain_tracked_facets(&mut self) -> Vec<(FacetId, Vec<Handle>)> {
        let root = self.activation.root_facet.clone();
        let facets = std::mem::take(&mut self.facets);
        facets
            .into_iter()
            .filter(|facet| facet != &root)
            .map(|facet| {
                let record = IOValue::record(
                    IOValue::symbol("interpreter-facet"),
                    vec![IOValue::new(facet.0.to_string())],
                );
                let handles = self.assertions.remove(&record).unwrap_or_default();
                (facet, handles)
            })
            .collect()
    }

    fn drain_observers(&mut self) -> Vec<ObserverSpec> {
        std::mem::take(&mut self.observers)
    }

    fn role_binding_mut(&mut self, name: &str) -> Result<&mut RoleBinding, ActorError> {
        self.roles.get_mut(name).ok_or_else(|| {
            ActorError::InvalidActivation(format!("spawn-entity references unknown role '{name}'"))
        })
    }

    fn roles_snapshot(&self) -> Vec<RoleBinding> {
        self.roles.values().cloned().collect()
    }

    fn prompt_counter_value(&self) -> u64 {
        self.prompt_counter
    }

    fn restore_prompt_counter(&mut self, counter: u64) {
        self.prompt_counter = counter;
    }

    fn handle_invoke_tool(
        &mut self,
        role: &str,
        capability: &str,
        payload: Option<&Value>,
        tag: Option<&String>,
    ) -> Result<(), ActorError> {
        let binding = self.role_binding(role)?;
        let capability_id = self.resolve_capability_id(binding, capability)?;
        let payload_value = payload
            .map(|value| value.to_io_value())
            .unwrap_or_else(|| IOValue::symbol("none"));
        let request_tag = tag
            .cloned()
            .unwrap_or_else(|| format!("tool-{}", Uuid::new_v4()));
        let role_properties = Self::encode_role_properties(binding);

        let invocation = ToolInvocation::new(
            role.to_string(),
            capability.to_string(),
            capability_id,
            payload_value,
            request_tag,
            role_properties,
        );
        self.dispatch_tool_invocation(invocation);
        Ok(())
    }

    fn dispatch_tool_invocation(&mut self, invocation: ToolInvocation) {
        let request_record = invocation.request_record(&self.instance_id);
        let handle = Handle::new();
        self.activation
            .assert(handle.clone(), request_record.clone());
        self.assertions
            .entry(request_record)
            .or_default()
            .push(handle);

        self.activation
            .assert(Handle::new(), invocation.log_record());

        let completion = invocation.completion(
            &self.activation.actor_id,
            &self.activation.current_facet,
            &self.instance_id,
        );

        self.activation.request_capability_invocation(
            invocation.capability_id(),
            invocation.payload(),
            completion,
        );
    }

    fn role_binding(&self, name: &str) -> Result<&RoleBinding, ActorError> {
        self.roles.get(name).ok_or_else(|| {
            ActorError::InvalidActivation(format!("invoke-tool references unknown role '{name}'"))
        })
    }

    fn resolve_value(&self, value: &Value) -> Result<Value, ActorError> {
        match value {
            Value::Record { label, fields } => {
                let mut resolved_fields = Vec::with_capacity(fields.len());
                for field in fields {
                    resolved_fields.push(self.resolve_value(field)?);
                }
                Ok(Value::Record {
                    label: label.clone(),
                    fields: resolved_fields,
                })
            }
            Value::List(items) => {
                let mut resolved_items = Vec::with_capacity(items.len());
                for item in items {
                    resolved_items.push(self.resolve_value(item)?);
                }
                Ok(Value::List(resolved_items))
            }
            Value::RoleProperty { role, key } => {
                let resolved_role = self.resolve_value(role)?;
                let role_name = match resolved_role {
                    Value::String(name) => name,
                    Value::Symbol(name) => name,
                    other => {
                        return Err(ActorError::InvalidActivation(format!(
                            "role-property expected role name as string, found {:?}",
                            other
                        )));
                    }
                };
                let binding = self.role_binding(&role_name)?;
                let value = binding.properties.get(key).cloned().ok_or_else(|| {
                    ActorError::InvalidActivation(format!(
                        "role '{}' does not define property '{}'",
                        role_name, key
                    ))
                })?;
                Ok(Value::String(value))
            }
            other => Ok(other.clone()),
        }
    }

    fn resolve_capability_id(
        &self,
        binding: &RoleBinding,
        capability: &str,
    ) -> Result<Uuid, ActorError> {
        let candidate = binding
            .properties
            .get(capability)
            .cloned()
            .or_else(|| {
                binding
                    .properties
                    .get(&format!("{capability}-capability"))
                    .cloned()
            })
            .or_else(|| binding.properties.get("capability").cloned())
            .unwrap_or_else(|| capability.to_string());

        Uuid::parse_str(&candidate).map_err(|_| {
            ActorError::InvalidActivation(format!(
                "invoke-tool requires capability UUID; got '{candidate}'"
            ))
        })
    }

    fn encode_role_properties(binding: &RoleBinding) -> Option<IOValue> {
        if binding.properties.is_empty() {
            return None;
        }

        let mut entries = Vec::with_capacity(binding.properties.len());
        for (key, value) in &binding.properties {
            entries.push(IOValue::record(
                IOValue::symbol("role-property"),
                vec![IOValue::symbol(key.clone()), IOValue::new(value.clone())],
            ));
        }

        Some(IOValue::record(IOValue::symbol("role-properties"), entries))
    }

    fn generate_request_id(&mut self, role: &str, property: &str) -> Result<String, ActorError> {
        let instance_id = self.instance_id.clone();
        let binding = self.role_binding_mut(role)?;
        let counter_key = format!("counter::{property}");
        let next_value = binding
            .properties
            .get(&counter_key)
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(0)
            .saturating_add(1);

        binding
            .properties
            .insert(counter_key, next_value.to_string());

        let request_id = format!("{}:{}:{}", instance_id, role, next_value);
        binding
            .properties
            .insert(property.to_string(), request_id.clone());

        Ok(request_id)
    }

    fn resolve_spawn_target(
        &self,
        role: &str,
        explicit_entity_type: Option<&String>,
        explicit_agent_kind: Option<&String>,
    ) -> Result<(String, Option<String>), ActorError> {
        let binding = self.role_binding(role)?;
        let entity_type = explicit_entity_type
            .cloned()
            .or_else(|| binding.properties.get("entity-type").cloned());
        let agent_kind = explicit_agent_kind
            .cloned()
            .or_else(|| binding.properties.get("agent-kind").cloned());

        if let Some(entity_type) = entity_type {
            return Ok((entity_type, agent_kind));
        }

        let Some(kind) = agent_kind else {
            return Err(ActorError::InvalidActivation(format!(
                "spawn-entity for role '{role}' requires :entity-type or :agent-kind"
            )));
        };

        let entity_type = agent::entity_type_for_kind(&kind).ok_or_else(|| {
            ActorError::InvalidActivation(format!(
                "spawn-entity could not resolve agent-kind '{kind}' to an entity type"
            ))
        })?;

        Ok((entity_type.to_string(), Some(kind)))
    }
}

impl<'a> ValueContext for ActivationHost<'a> {
    fn role_property(&self, role: &str, key: &str) -> Option<String> {
        self.roles
            .get(role)
            .and_then(|binding| binding.properties.get(key).cloned())
    }
}

impl<'a> InterpreterHost for ActivationHost<'a> {
    type Error = ActorError;

    fn execute_action(&mut self, action: &Action) -> std::result::Result<(), Self::Error> {
        match action {
            Action::Log(message) => {
                let record = IOValue::record(
                    IOValue::symbol(LOG_RECORD_LABEL),
                    vec![IOValue::new(message.clone())],
                );
                self.activation.assert(Handle::new(), record);
                Ok(())
            }
            Action::Assert(value) => {
                let resolved = self.resolve_value(value)?;
                let coerced = resolved.to_io_value();
                let handle = Handle::new();
                self.activation.assert(handle.clone(), coerced.clone());
                self.assertions.entry(coerced).or_default().push(handle);
                Ok(())
            }
            Action::Retract(value) => {
                let resolved = self.resolve_value(value)?;
                let target = resolved.to_io_value();
                let mut remove_entry = false;
                let handle = {
                    let handles = self.assertions.get_mut(&target).ok_or_else(|| {
                        ActorError::InvalidActivation(
                            "interpreter attempted to retract value that was not asserted".into(),
                        )
                    })?;
                    let handle = handles.pop().ok_or_else(|| {
                        ActorError::InvalidActivation(
                            "interpreter attempted to retract value that was not asserted".into(),
                        )
                    })?;
                    if handles.is_empty() {
                        remove_entry = true;
                    }
                    handle
                };

                if remove_entry {
                    self.assertions.remove(&target);
                }

                self.activation.retract(handle);
                Ok(())
            }
            Action::RegisterPattern {
                role,
                pattern,
                property,
            } => {
                let (entity_id, facet_id) = {
                    let binding = self.role_binding(role)?;
                    let entity_str = binding.properties.get("entity").ok_or_else(|| {
                        ActorError::InvalidActivation(format!(
                            "register-pattern for role '{role}' requires role property 'entity'"
                        ))
                    })?;
                    let facet_str = binding.properties.get("facet").ok_or_else(|| {
                        ActorError::InvalidActivation(format!(
                            "register-pattern for role '{role}' requires role property 'facet'"
                        ))
                    })?;

                    let entity_value = entity_str.clone();
                    let facet_value = facet_str.clone();

                    let entity_uuid = Uuid::parse_str(&entity_value).map_err(|_| {
                        ActorError::InvalidActivation(format!(
                            "register-pattern role '{role}' property 'entity' must be a UUID, got {entity_value}"
                        ))
                    })?;
                    let facet_uuid = Uuid::parse_str(&facet_value).map_err(|_| {
                        ActorError::InvalidActivation(format!(
                            "register-pattern role '{role}' property 'facet' must be a UUID, got {facet_value}"
                        ))
                    })?;

                    (entity_uuid, FacetId::from_uuid(facet_uuid))
                };

                let resolved_pattern = self.resolve_value(pattern)?.to_io_value();
                let pattern_id = self.activation.register_pattern_for_entity(
                    entity_id,
                    facet_id,
                    resolved_pattern,
                );

                if let Some(prop) = property {
                    let binding = self.role_binding_mut(role)?;
                    binding
                        .properties
                        .insert(prop.clone(), pattern_id.to_string());
                }

                Ok(())
            }
            Action::UnregisterPattern {
                role,
                pattern,
                property,
            } => {
                let property_name = property
                    .clone()
                    .unwrap_or_else(|| "agent-request-pattern".to_string());

                let (pattern_id_str, remove_property): (String, bool) = if let Some(value) = pattern
                {
                    let id = match &value {
                        Value::String(text) => text.clone(),
                        Value::Symbol(sym) => sym.clone(),
                        other => {
                            return Err(ActorError::InvalidActivation(format!(
                                "unregister-pattern :pattern must resolve to a string, found {:?}",
                                other
                            )));
                        }
                    };
                    (id, property.is_some())
                } else {
                    let binding = self.role_binding(role)?;
                    let existing = binding.properties.get(&property_name).ok_or_else(|| {
                        ActorError::InvalidActivation(format!(
                            "role '{role}' does not define property '{}'",
                            property_name
                        ))
                    })?;
                    (existing.clone(), true)
                };

                let pattern_uuid = Uuid::parse_str(&pattern_id_str).map_err(|_| {
                    ActorError::InvalidActivation(format!(
                        "unregister-pattern expected UUID string, got {}",
                        pattern_id_str
                    ))
                })?;

                self.activation.unregister_pattern(pattern_uuid);

                if remove_property {
                    if let Ok(binding) = self.role_binding_mut(role) {
                        binding.properties.remove(&property_name);
                    }
                }

                Ok(())
            }
            Action::DetachEntity { role } => {
                let (_facet_value, entity_value, pattern_value) = {
                    let binding = self.role_binding(role)?;
                    let facet = binding.properties.get("facet").cloned();
                    let entity = binding.properties.get("entity").cloned().ok_or_else(|| {
                        ActorError::InvalidActivation(format!(
                            "detach-entity for role '{role}' requires role property 'entity'"
                        ))
                    })?;
                    let pattern = binding.properties.get("agent-request-pattern").cloned();
                    (facet, entity, pattern)
                };

                let entity_uuid = Uuid::parse_str(&entity_value).map_err(|_| {
                    ActorError::InvalidActivation(format!(
                        "detach-entity role '{role}' property 'entity' must be a UUID, got {}",
                        entity_value
                    ))
                })?;

                if let Some(pattern_str) = pattern_value.as_ref() {
                    if let Ok(pattern_uuid) = Uuid::parse_str(pattern_str) {
                        self.activation.unregister_pattern(pattern_uuid);
                    }
                }

                self.activation.detach_entity(entity_uuid);

                if let Some(key) = self
                    .assertions
                    .keys()
                    .find(|value| {
                        if let Some(view) = record_with_label(value, ENTITY_RECORD_LABEL) {
                            view.field_string(4)
                                .map(|candidate| candidate == entity_value)
                                .unwrap_or(false)
                        } else {
                            false
                        }
                    })
                    .cloned()
                {
                    if let Some(handles) = self.assertions.get_mut(&key) {
                        if let Some(handle) = handles.pop() {
                            self.activation.retract(handle.clone());
                        }
                        if handles.is_empty() {
                            self.assertions.remove(&key);
                        }
                    }
                }

                if let Ok(binding) = self.role_binding_mut(role) {
                    binding.properties.remove("actor");
                    binding.properties.remove("facet");
                    binding.properties.remove("entity");
                    binding.properties.remove("entity-type");
                    binding.properties.remove("agent-kind");
                    binding.properties.remove("agent-request-pattern");
                }

                Ok(())
            }
            Action::Send {
                actor,
                facet,
                payload,
            } => {
                let actor_value = self.resolve_value(actor)?;
                let actor_text = actor_value.as_str().ok_or_else(|| {
                    ActorError::InvalidActivation(
                        "send requires :actor to resolve to a string".into(),
                    )
                })?;
                let actor_id = Uuid::parse_str(actor_text).map_err(|_| {
                    ActorError::InvalidActivation(format!(
                        "send requires :actor to be a UUID, got {actor_text}"
                    ))
                })?;

                let facet_value = self.resolve_value(facet)?;
                let facet_text = facet_value.as_str().ok_or_else(|| {
                    ActorError::InvalidActivation(
                        "send requires :facet to resolve to a string".into(),
                    )
                })?;
                let facet_id = Uuid::parse_str(facet_text).map_err(|_| {
                    ActorError::InvalidActivation(format!(
                        "send requires :facet to be a UUID, got {facet_text}"
                    ))
                })?;

                let payload_value = self.resolve_value(payload)?.to_io_value();
                self.activation.send_message(
                    ActorId::from_uuid(actor_id),
                    FacetId::from_uuid(facet_id),
                    payload_value,
                );
                Ok(())
            }
            Action::Observe { label, handler } => {
                self.observers.push(ObserverSpec {
                    condition: WaitCondition::Signal {
                        label: label.clone(),
                    },
                    handler: handler.clone(),
                    facet: self.activation.current_facet.clone(),
                });
                Ok(())
            }
            Action::Spawn { parent } => {
                let parent_facet = if let Some(parent_str) = parent {
                    let uuid = Uuid::parse_str(parent_str).map_err(|_| {
                        ActorError::InvalidActivation(format!(
                            "spawn requires :parent to be a UUID, got {parent_str}"
                        ))
                    })?;
                    Some(FacetId::from_uuid(uuid))
                } else {
                    Some(self.activation.current_facet.clone())
                };

                let new_facet = self.activation.spawn_facet(parent_facet);
                self.track_facet(new_facet.clone());
                self.assert_facet_record(&new_facet);
                Ok(())
            }
            Action::GenerateRequestId { role, property } => {
                self.generate_request_id(role, property)?;
                Ok(())
            }
            Action::Stop { facet } => {
                let uuid = Uuid::parse_str(facet).map_err(|_| {
                    ActorError::InvalidActivation(format!(
                        "stop requires :facet to be a UUID, got {facet}"
                    ))
                })?;
                let facet_id = FacetId::from_uuid(uuid);
                self.activation.terminate_facet(facet_id.clone());
                self.facets.retain(|id| id != &facet_id);
                self.retract_facet_assertion(&facet_id);
                Ok(())
            }
            Action::SpawnEntity {
                role,
                entity_type,
                agent_kind,
                config,
            } => {
                let (resolved_entity_type, resolved_kind) =
                    self.resolve_spawn_target(role, entity_type.as_ref(), agent_kind.as_ref())?;

                let config_value = match config {
                    Some(value) => self.resolve_value(value)?.to_io_value(),
                    None => IOValue::symbol("nil"),
                };

                let spawned = self
                    .activation
                    .spawn_entity(resolved_entity_type.clone(), config_value);

                {
                    let binding = self.role_binding_mut(role)?;
                    binding
                        .properties
                        .insert("actor".into(), spawned.actor.0.to_string());
                    binding
                        .properties
                        .insert("facet".into(), spawned.root_facet.0.to_string());
                    binding
                        .properties
                        .insert("entity".into(), spawned.entity_id.to_string());
                    binding
                        .properties
                        .insert("entity-type".into(), resolved_entity_type.clone());
                    if let Some(kind) = resolved_kind.clone() {
                        binding.properties.insert("agent-kind".into(), kind);
                    }
                }

                let pattern_id = if resolved_kind.is_some() {
                    let request_pattern = IOValue::record(
                        IOValue::symbol("agent-request"),
                        vec![
                            IOValue::new(spawned.entity_id.to_string()),
                            IOValue::symbol("<_>"),
                            IOValue::symbol("<_>"),
                        ],
                    );
                    Some(self.activation.register_pattern_for_entity(
                        spawned.entity_id,
                        spawned.root_facet.clone(),
                        request_pattern,
                    ))
                } else {
                    None
                };

                if let Some(id) = pattern_id {
                    let binding = self.role_binding_mut(role)?;
                    binding
                        .properties
                        .insert("agent-request-pattern".into(), id.to_string());
                }

                let role_properties_value = {
                    let binding = self.role_binding(role)?;
                    Self::encode_role_properties(binding)
                };

                let mut fields = vec![
                    IOValue::new(self.instance_id.clone()),
                    IOValue::new(role.clone()),
                    IOValue::new(spawned.actor.0.to_string()),
                    IOValue::new(spawned.root_facet.0.to_string()),
                    IOValue::new(spawned.entity_id.to_string()),
                    IOValue::new(resolved_entity_type.clone()),
                ];

                if let Some(kind) = resolved_kind {
                    fields.push(IOValue::new(kind));
                }

                if let Some(props) = role_properties_value {
                    fields.push(props);
                }

                let record = IOValue::record(IOValue::symbol("interpreter-entity"), fields);
                let handle = Handle::new();
                self.activation.assert(handle.clone(), record.clone());
                self.assertions.entry(record).or_default().push(handle);
                Ok(())
            }
            Action::AttachEntity {
                role,
                facet,
                entity_type,
                agent_kind,
                config,
            } => {
                let target_facet = if let Some(facet_str) = facet {
                    let uuid = Uuid::parse_str(facet_str).map_err(|_| {
                        ActorError::InvalidActivation(format!(
                            "attach-entity requires :facet to be a UUID, got {facet_str}"
                        ))
                    })?;
                    FacetId::from_uuid(uuid)
                } else {
                    let new_facet = self
                        .activation
                        .spawn_facet(Some(self.activation.current_facet.clone()));
                    self.track_facet(new_facet.clone());
                    self.assert_facet_record(&new_facet);
                    new_facet
                };

                let (resolved_entity_type, resolved_kind) =
                    self.resolve_spawn_target(role, entity_type.as_ref(), agent_kind.as_ref())?;

                let config_value = match config {
                    Some(value) => self.resolve_value(value)?.to_io_value(),
                    None => IOValue::symbol("nil"),
                };

                let attached = self.activation.attach_entity_to_facet(
                    target_facet.clone(),
                    resolved_entity_type.clone(),
                    config_value,
                );

                let actor_id_str = self.activation.actor_id.0.to_string();
                {
                    let binding = self.role_binding_mut(role)?;
                    binding
                        .properties
                        .insert("actor".into(), actor_id_str.clone());
                    binding
                        .properties
                        .insert("facet".into(), attached.facet.0.to_string());
                    binding
                        .properties
                        .insert("entity".into(), attached.entity_id.to_string());
                    binding
                        .properties
                        .insert("entity-type".into(), resolved_entity_type.clone());
                    if let Some(kind) = resolved_kind.clone() {
                        binding.properties.insert("agent-kind".into(), kind);
                    }
                }

                let pattern_id = if resolved_kind.is_some() {
                    let request_pattern = IOValue::record(
                        IOValue::symbol("agent-request"),
                        vec![
                            IOValue::new(attached.entity_id.to_string()),
                            IOValue::symbol("<_>"),
                            IOValue::symbol("<_>"),
                        ],
                    );
                    Some(self.activation.register_pattern_for_entity(
                        attached.entity_id,
                        attached.facet.clone(),
                        request_pattern,
                    ))
                } else {
                    None
                };

                if let Some(id) = pattern_id {
                    let binding = self.role_binding_mut(role)?;
                    binding
                        .properties
                        .insert("agent-request-pattern".into(), id.to_string());
                }

                let role_properties_value = {
                    let binding = self.role_binding(role)?;
                    Self::encode_role_properties(binding)
                };

                let mut fields = vec![
                    IOValue::new(self.instance_id.clone()),
                    IOValue::new(role.clone()),
                    IOValue::new(actor_id_str.clone()),
                    IOValue::new(attached.facet.0.to_string()),
                    IOValue::new(attached.entity_id.to_string()),
                    IOValue::new(resolved_entity_type.clone()),
                ];

                if let Some(kind) = resolved_kind {
                    fields.push(IOValue::new(kind));
                }

                if let Some(props) = role_properties_value {
                    fields.push(props);
                }

                let record = IOValue::record(IOValue::symbol("interpreter-entity"), fields);
                let handle = Handle::new();
                self.activation.assert(handle.clone(), record.clone());
                self.assertions.entry(record).or_default().push(handle);
                Ok(())
            }
            Action::InvokeTool {
                role,
                capability,
                payload,
                tag,
            } => self.handle_invoke_tool(role, capability, payload.as_ref(), tag.as_ref()),
        }
    }

    fn prepare_wait(&mut self, wait: &mut WaitCondition) -> std::result::Result<(), Self::Error> {
        if let WaitCondition::RecordFieldEq { value, .. } = wait {
            let resolved = self.resolve_value(value)?;
            *value = resolved;
        }

        if let WaitCondition::UserInput {
            prompt,
            tag,
            request_id,
        } = wait
        {
            let effective_request = request_id.clone().unwrap_or_else(|| {
                let next = self.next_prompt_request_id();
                *request_id = Some(next.clone());
                next
            });

            self.bump_prompt_counter_for(&effective_request);

            let effective_tag = tag.clone().unwrap_or_else(|| {
                let derived = effective_request.clone();
                *tag = Some(derived.clone());
                derived
            });

            *request_id = Some(effective_request.clone());
            *tag = Some(effective_tag.clone());

            if let Some(existing) = self
                .prompts
                .iter_mut()
                .find(|prompt| prompt.request_id == effective_request)
            {
                existing.wait = wait.clone();
                existing.request_id = effective_request.clone();
                existing.tag = effective_tag.clone();
            } else {
                let record = InputRequestRecord {
                    instance_id: self.instance_id.clone(),
                    request_id: effective_request.clone(),
                    tag: effective_tag.clone(),
                    prompt: prompt.clone(),
                };
                let record_value = input_request_to_value(&record);
                let handle = Handle::new();
                self.activation.assert(handle.clone(), record_value);
                self.prompts.push(PromptRecord::new(
                    wait.clone(),
                    effective_request.clone(),
                    effective_tag.clone(),
                    handle,
                ));
            }
        }

        Ok(())
    }

    fn poll_wait(&mut self, wait: &WaitCondition) -> std::result::Result<bool, Self::Error> {
        if let Some(index) = self
            .satisfied
            .iter()
            .position(|entry| Self::condition_matches(&entry.condition, wait))
        {
            let ready = self.satisfied.remove(index);
            self.last_ready_value = ready.value;
            if let WaitCondition::UserInput { request_id, .. } = &ready.condition {
                if let Some(id) = request_id {
                    if let Some(prompt) = self.remove_prompt_by_request(id) {
                        self.activation.retract(prompt.handle);
                    }
                }
            }
            Ok(true)
        } else {
            Ok(false)
        }
    }

    fn take_ready_value(&mut self) -> Option<IOValue> {
        self.last_ready_value.take()
    }
}

#[cfg(test)]
impl InterpreterEntity {
    fn definition_count(&self) -> usize {
        self.definitions.lock().unwrap().len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::interpreter::protocol::INPUT_REQUEST_RECORD_LABEL;
    use crate::runtime::actor::Actor;
    use crate::runtime::turn::{ActorId, FacetId, TurnOutput};

    fn activation_for(actor: &Actor) -> Activation {
        activation_on(actor, actor.root_facet.clone())
    }

    fn activation_on(actor: &Actor, facet: FacetId) -> Activation {
        let mut activation = Activation::new(actor.id.clone(), facet, None);
        activation.set_current_entity(Some(Uuid::new_v4()));
        activation
    }

    #[test]
    fn interpreter_entity_emits_log_and_prompt() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let program = "(workflow demo)
               (state start
                 (emit (assert (record agent-request \"agent-1\" \"planner\" \"hello\")))
                 (emit (log \"hello\"))
                 (terminal))";
        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        assert!(activation
            .assertions_added
            .iter()
            .any(|(_, value)| matches!(value.label().as_symbol(), Some(sym) if sym.as_ref() == LOG_RECORD_LABEL)));

        assert!(activation.assertions_added.iter().any(|(_, value)| {
            value
                .label()
                .as_symbol()
                .map(|sym| sym.as_ref() == LOG_RECORD_LABEL)
                .unwrap_or(false)
        }));
        assert!(activation.assertions_added.iter().any(|(_, value)| {
            value
                .label()
                .as_symbol()
                .map(|sym| sym.as_ref() == "agent-request")
                .unwrap_or(false)
        }));
    }

    #[test]
    fn interpreter_send_message_action() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let target_actor = ActorId::new();
        let target_facet = FacetId::new();
        let program = format!(
            "(workflow send-demo)\n(state start\n  (emit (send :actor \"{}\" :facet \"{}\" :value (record ping \"hello\")))\n  (terminal))",
            target_actor.0, target_facet.0
        );

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        let message = activation
            .outputs
            .iter()
            .find_map(|output| {
                if let TurnOutput::Message {
                    target_actor: actor_id,
                    target_facet: facet_id,
                    payload,
                } = output
                {
                    Some((actor_id.clone(), facet_id.clone(), payload.clone()))
                } else {
                    None
                }
            })
            .expect("message output");

        let expected_payload = IOValue::record(
            IOValue::symbol("ping"),
            vec![IOValue::new("hello".to_string())],
        );

        assert_eq!(message.0, target_actor);
        assert_eq!(message.1, target_facet);
        assert_eq!(message.2, expected_payload);
    }

    #[test]
    fn user_input_wait_generates_prompt_request() {
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let mut host = ActivationHost::new(&mut activation, "instance".into(), &[]);
        let mut wait = WaitCondition::UserInput {
            prompt: Value::String("Enter value".into()),
            tag: None,
            request_id: None,
        };

        host.prepare_wait(&mut wait).expect("prepare wait");

        let (tag, request_id) = match &wait {
            WaitCondition::UserInput {
                tag, request_id, ..
            } => (tag.as_ref().cloned(), request_id.as_ref().cloned()),
            other => panic!("unexpected wait condition: {other:?}"),
        };

        assert!(tag.is_some(), "tag should be assigned by prepare_wait");
        assert!(
            request_id.is_some(),
            "request id should be assigned by prepare_wait"
        );

        let prompts = host.snapshot_prompts();
        assert_eq!(prompts.len(), 1);
        let recorded = &prompts[0];
        assert_eq!(recorded.request_id, request_id.unwrap());

        assert!(activation.assertions_added.iter().any(|(_, value)| {
            value
                .label()
                .as_symbol()
                .map(|sym| sym.as_ref() == INPUT_REQUEST_RECORD_LABEL)
                .unwrap_or(false)
        }));
    }

    #[test]
    fn interpreter_observe_signal_triggers_handler() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let handler_source = r#"(workflow observer-handler)
(state start
  (emit (assert (record observed)))
  (terminal))"#;

        let program = format!(
            "(workflow observe-demo)\n(state start (emit (observe (signal ready) \"{}\")) (terminal))",
            handler_source.replace("\"", "\\\"")
        );

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        let observer_record = activation
            .assertions_added
            .iter()
            .find_map(|(_, value)| {
                value
                    .label()
                    .as_symbol()
                    .filter(|sym| sym.as_ref() == OBSERVER_RECORD_LABEL)
                    .map(|_| value.clone())
            })
            .expect("observer registration should be asserted");

        let observer_view =
            record_with_label(&observer_record, OBSERVER_RECORD_LABEL).expect("observer record");
        let condition_label = observer_view
            .field(1)
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref().to_string());
        assert_eq!(
            condition_label.as_deref(),
            Some("signal"),
            "observer condition should encode signal wait"
        );

        activation.outputs.clear();
        activation.assertions_added.clear();

        let ready_signal = IOValue::record(IOValue::symbol("ready"), vec![]);
        entity
            .process_assertion(&mut activation, &ready_signal)
            .unwrap();

        let observed = activation.assertions_added.iter().any(|(_, value)| {
            value
                .label()
                .as_symbol()
                .map(|sym| sym.as_ref() == "observed")
                .unwrap_or(false)
        });

        assert!(observed, "observer handler should assert observed record");
    }

    #[test]
    fn interpreter_observer_survives_snapshot_restore() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let handler_source = r#"(workflow observer-handler)
(state start
  (emit (assert (record observed)))
  (terminal))"#;

        let program = format!(
            "(workflow observe-demo)\n(state start (emit (observe (signal ready) \"{}\")) (terminal))",
            handler_source.replace("\"", "\\\"")
        );

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        entity.on_message(&mut activation, &payload).unwrap();
        assert!(
            !entity.observers.lock().unwrap().is_empty(),
            "observer should be registered"
        );

        let snapshot = entity.snapshot_state();

        let mut restored = InterpreterEntity::default();
        restored.restore_state(&snapshot).unwrap();

        assert!(
            !restored.observers.lock().unwrap().is_empty(),
            "observer should be restored from snapshot"
        );

        let mut resume_activation = activation_for(&actor);
        let ready_signal = IOValue::record(IOValue::symbol("ready"), vec![]);
        restored
            .process_assertion(&mut resume_activation, &ready_signal)
            .unwrap();

        let observed = resume_activation.assertions_added.iter().any(|(_, value)| {
            value
                .label()
                .as_symbol()
                .map(|sym| sym.as_ref() == "observed")
                .unwrap_or(false)
        });

        assert!(
            observed,
            "restored observer handler should assert observed record"
        );
    }

    #[test]
    fn interpreter_spawn_facet_action() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let program = "(workflow spawn-demo)\n(state start (emit (spawn)) (terminal))";

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        let spawn_output = activation
            .outputs
            .iter()
            .find_map(|output| {
                if let TurnOutput::FacetSpawned { facet, parent } = output {
                    Some((facet.clone(), parent.clone()))
                } else {
                    None
                }
            })
            .expect("facet spawned output");

        assert_eq!(spawn_output.1, Some(actor.root_facet.clone()));

        let facet_asserted = activation.assertions_added.iter().any(|(_, value)| {
            value
                .label()
                .as_symbol()
                .map(|sym| sym.as_ref() == "interpreter-facet")
                .unwrap_or(false)
        });
        assert!(
            facet_asserted,
            "spawn should assert interpreter-facet record"
        );
    }

    #[test]
    fn interpreter_spawn_entity_action() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let program = "(workflow spawn-entity-demo)
(roles (worker :agent-kind \"claude-code\"))
(state start
  (emit (spawn-entity :role worker :config (record agent-config \"demo\")))
  (terminal))";

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program.to_string())],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        let (spawn_entity_type, spawned_entity_id, spawned_root_facet) = activation
            .outputs
            .iter()
            .find_map(|output| {
                if let TurnOutput::EntitySpawned {
                    entity_id,
                    entity_type,
                    child_root_facet,
                    ..
                } = output
                {
                    Some((entity_type.clone(), *entity_id, child_root_facet.clone()))
                } else {
                    None
                }
            })
            .expect("spawn-entity should emit EntitySpawned output");
        assert_eq!(spawn_entity_type, "agent-claude-code");

        let capability_granted = activation.outputs.iter().any(|output| {
            matches!(
                output,
                TurnOutput::CapabilityGranted { kind, .. }
                if kind == ENTITY_SPAWN_CAPABILITY_KIND
            )
        });
        assert!(
            capability_granted,
            "interpreter should mint the entity/spawn capability automatically"
        );

        let registered_pattern = activation
            .outputs
            .iter()
            .find_map(|output| match output {
                TurnOutput::PatternRegistered { entity_id, pattern }
                    if entity_id == &spawned_entity_id =>
                {
                    Some(pattern.clone())
                }
                _ => None,
            })
            .expect("spawn-entity should register agent-request pattern");
        assert_eq!(
            registered_pattern.facet, spawned_root_facet,
            "pattern should target spawned root facet"
        );
        let pattern_view = record_with_label(&registered_pattern.pattern, agent::REQUEST_LABEL)
            .expect("pattern should match agent-request label");
        assert_eq!(
            pattern_view.len(),
            3,
            "agent-request pattern matches agent id, request id, and prompt"
        );
        let expected_entity_id = spawned_entity_id.to_string();
        assert_eq!(
            pattern_view.field_string(0).as_deref(),
            Some(expected_entity_id.as_str()),
            "agent-request pattern should target spawned entity id"
        );

        let entity_record = activation
            .assertions_added
            .iter()
            .find_map(|(_, value)| {
                value
                    .label()
                    .as_symbol()
                    .filter(|sym| sym.as_ref() == "interpreter-entity")
                    .and(Some(value.clone()))
            })
            .expect("spawn-entity should assert interpreter-entity record");

        let entity_view =
            record_with_label(&entity_record, "interpreter-entity").expect("entity record view");
        let instance_id = entity_view.field_string(0).expect("instance id present");
        assert!(!instance_id.is_empty(), "instance id should not be empty");
        assert_eq!(entity_view.field_string(1).as_deref(), Some("worker"));
        assert_eq!(
            entity_view.field_string(5).as_deref(),
            Some("agent-claude-code")
        );
        assert_eq!(entity_view.field_string(6).as_deref(), Some("claude-code"));

        let props_index = entity_view.len() - 1;
        let props_field = entity_view.field(props_index);
        let props_view = record_with_label(&props_field, "role-properties")
            .expect("role properties record present");
        let mut observed_keys = Vec::new();
        for idx in 0..props_view.len() {
            if let Some(entry) = record_with_label(&props_view.field(idx), "role-property") {
                if let (Some(key), Some(value)) = (entry.field_symbol(0), entry.field_string(1)) {
                    match key.as_ref() {
                        "actor" | "facet" | "entity" => {
                            observed_keys.push(key.to_string());
                            assert!(!value.is_empty(), "{key} should not be empty");
                        }
                        "entity-type" => assert_eq!(value, "agent-claude-code"),
                        "agent-kind" => assert_eq!(value, "claude-code"),
                        _ => {}
                    }
                }
            }
        }
        assert!(observed_keys.contains(&"actor".to_string()));
        assert!(observed_keys.contains(&"facet".to_string()));
        assert!(observed_keys.contains(&"entity".to_string()));

        let instance_records: Vec<_> = activation
            .assertions_added
            .iter()
            .filter_map(|(_, value)| InstanceRecord::parse(value))
            .collect();
        let latest = instance_records.last().expect("instance record present");
        let worker_binding = latest
            .roles
            .iter()
            .find(|binding| binding.name == "worker")
            .expect("worker role binding recorded");
        assert_eq!(
            worker_binding.properties.get("entity-type"),
            Some(&"agent-claude-code".to_string())
        );
        assert_eq!(
            worker_binding.properties.get("agent-kind"),
            Some(&"claude-code".to_string())
        );
        assert!(worker_binding.properties.contains_key("actor"));
    }

    #[test]
    fn interpreter_spawn_entity_with_explicit_agent_kind() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let program = "(workflow spawn-entity-explicit)
(roles (worker :label \"primary\"))
(state start
  (emit (spawn-entity :role worker :agent-kind \"claude-code\"))
  (terminal))";

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program.to_string())],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        let spawn_output = activation.outputs.iter().find_map(|output| {
            if let TurnOutput::EntitySpawned { entity_type, .. } = output {
                Some(entity_type.clone())
            } else {
                None
            }
        });
        assert_eq!(
            spawn_output.as_deref(),
            Some("agent-claude-code"),
            "explicit agent-kind should resolve to Claude entity"
        );

        let role_binding_record = activation
            .assertions_added
            .iter()
            .find_map(|(_, value)| {
                value
                    .label()
                    .as_symbol()
                    .filter(|sym| sym.as_ref() == "interpreter-entity")
                    .and(Some(value.clone()))
            })
            .expect("interpreter-entity record asserted");

        let entity_view = record_with_label(&role_binding_record, "interpreter-entity").unwrap();
        let instance_id = entity_view.field_string(0).expect("instance id present");
        assert!(!instance_id.is_empty());
        assert_eq!(entity_view.field_string(1).as_deref(), Some("worker"));
        assert_eq!(entity_view.field_string(6).as_deref(), Some("claude-code"));

        let instance_records: Vec<_> = activation
            .assertions_added
            .iter()
            .filter_map(|(_, value)| InstanceRecord::parse(value))
            .collect();
        let latest = instance_records.last().expect("instance record present");
        let worker_binding = latest
            .roles
            .iter()
            .find(|binding| binding.name == "worker")
            .expect("worker role binding recorded");
        assert_eq!(
            worker_binding.properties.get("agent-kind"),
            Some(&"claude-code".to_string())
        );
        assert_eq!(
            worker_binding.properties.get("entity-type"),
            Some(&"agent-claude-code".to_string())
        );
    }

    #[test]
    fn interpreter_stop_facet_action() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let target_facet = FacetId::new();
        let program = format!(
            "(workflow stop-demo)\n(state start (emit (stop :facet \"{}\")) (terminal))",
            target_facet.0
        );

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        let terminated = activation.outputs.iter().any(|output| {
            matches!(
                output,
                TurnOutput::FacetTerminated { facet } if facet == &target_facet
            )
        });
        assert!(terminated, "stop should emit FacetTerminated output");
    }

    #[test]
    fn interpreter_define_and_run_by_id() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let define_payload = IOValue::record(
            IOValue::symbol(DEFINE_MESSAGE_LABEL),
            vec![IOValue::new(
                "(workflow hello) (state start (terminal))".to_string(),
            )],
        );

        entity.on_message(&mut activation, &define_payload).unwrap();
        assert_eq!(entity.definition_count(), 1);

        let definition_id = activation
            .assertions_added
            .iter()
            .find_map(|(_, value)| {
                if let Some(sym) = value.label().as_symbol() {
                    if sym.as_ref() == "interpreter-defined" {
                        return value.index(0).as_string().map(|s| s.as_ref().to_string());
                    }
                }
                None
            })
            .expect("definition id");

        let mut run_activation = activation_for(&actor);
        let run_payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::symbol("definition"), IOValue::new(definition_id)],
        );

        entity
            .on_message(&mut run_activation, &run_payload)
            .unwrap();
        let instance_records: Vec<_> = run_activation
            .assertions_added
            .iter()
            .filter_map(|(_, value)| InstanceRecord::parse(value))
            .collect();
        let completed = instance_records
            .iter()
            .find(|record| matches!(record.status, InstanceStatus::Completed))
            .expect("completed record");
        assert!(matches!(completed.program, ProgramRef::Definition(_)));
        let progress = completed
            .progress
            .as_ref()
            .expect("progress snapshot present");
        assert_eq!(progress.state.as_deref(), Some("start"));
        assert!(!progress.entry_pending);
        assert!(progress.waiting.is_none());
    }

    #[test]
    fn interpreter_definitions_survive_snapshot_restore() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let define_payload = IOValue::record(
            IOValue::symbol(DEFINE_MESSAGE_LABEL),
            vec![IOValue::new(
                "(workflow persist)
                   (state start (terminal))"
                    .to_string(),
            )],
        );

        entity.on_message(&mut activation, &define_payload).unwrap();
        assert_eq!(entity.definition_count(), 1);

        let definition_id = activation
            .assertions_added
            .iter()
            .find_map(|(_, value)| {
                if let Some(sym) = value.label().as_symbol() {
                    if sym.as_ref() == "interpreter-defined" {
                        return value.index(0).as_string().map(|s| s.as_ref().to_string());
                    }
                }
                None
            })
            .expect("definition id");

        let snapshot = entity.snapshot_state();

        let mut restored = InterpreterEntity::default();
        restored.restore_state(&snapshot).unwrap();
        assert_eq!(restored.definition_count(), 1);

        let mut run_activation = activation_for(&actor);
        let run_payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::symbol("definition"), IOValue::new(definition_id)],
        );

        restored
            .on_message(&mut run_activation, &run_payload)
            .unwrap();

        let completed = run_activation
            .assertions_added
            .iter()
            .filter_map(|(_, value)| InstanceRecord::parse(value))
            .find(|record| matches!(record.status, InstanceStatus::Completed))
            .expect("completed record after restore");

        assert!(matches!(completed.program, ProgramRef::Definition(_)));
        let progress = completed
            .progress
            .as_ref()
            .expect("progress snapshot present");
        assert_eq!(progress.state.as_deref(), Some("start"));
        assert!(progress.waiting.is_none());
    }
    #[test]
    fn interpreter_waits_and_resumes() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let program = r#"(workflow wait-demo)
(state start
  (emit (log "start"))
  (await (signal ready))
  (transition done))
(state done (terminal))"#;

        let run_payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program.to_string())],
        );

        entity.on_message(&mut activation, &run_payload).unwrap();
        let waiting_record = activation
            .assertions_added
            .iter()
            .filter_map(|(_, value)| InstanceRecord::parse(value))
            .find(|record| matches!(record.status, InstanceStatus::Waiting(_)))
            .expect("waiting record");
        let instance_id = waiting_record.instance_id.clone();

        assert!(entity.waiting.lock().unwrap().contains_key(&instance_id));
        assert!(
            waiting_record
                .progress
                .as_ref()
                .and_then(|progress| progress.waiting.as_ref())
                .is_some()
        );

        let resume_payload = IOValue::record(
            IOValue::symbol(RESUME_MESSAGE_LABEL),
            vec![
                IOValue::new(instance_id.clone()),
                IOValue::record(IOValue::symbol("signal"), vec![IOValue::symbol("ready")]),
            ],
        );

        let mut resume_activation = activation_for(&actor);
        entity
            .on_message(&mut resume_activation, &resume_payload)
            .unwrap();

        assert!(entity.waiting.lock().unwrap().is_empty());

        let completed_record = resume_activation
            .assertions_added
            .iter()
            .filter_map(|(_, value)| InstanceRecord::parse(value))
            .find(|record| matches!(record.status, InstanceStatus::Completed))
            .expect("completed record");
        assert!(
            completed_record
                .progress
                .as_ref()
                .and_then(|progress| progress.waiting.as_ref())
                .is_none()
        );
    }

    #[test]
    fn interpreter_invoke_tool_action() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let capability_id = Uuid::new_v4();
        let program = format!(
            "(workflow tool-demo)
               (roles (workspace :capability \"{}\" :agent-kind \"tester\"))
               (state start
                 (emit (invoke-tool :role workspace :capability \"capability\" :payload (record request \"payload\") :tag tool-req))
                 (terminal))",
            capability_id
        );

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        entity.on_message(&mut activation, &payload).unwrap();

        let tool_request = activation
            .assertions_added
            .iter()
            .find_map(|(_, value)| {
                value
                    .label()
                    .as_symbol()
                    .filter(|sym| sym.as_ref() == TOOL_REQUEST_RECORD_LABEL)
                    .map(|_| value.clone())
            })
            .expect("tool request record should be asserted");

        let view =
            record_with_label(&tool_request, TOOL_REQUEST_RECORD_LABEL).expect("record view");
        assert!(view.len() >= 6, "tool request must contain expected fields");

        let instance_id = view
            .field_string(0)
            .expect("instance id should be a string");
        assert!(!instance_id.is_empty(), "instance id should not be empty");

        assert_eq!(
            view.field_string(1).as_deref(),
            Some("tool-req"),
            "tag preserved"
        );
        assert_eq!(
            view.field_string(2).as_deref(),
            Some("workspace"),
            "role field should match"
        );
        assert_eq!(
            view.field_string(3).as_deref(),
            Some("capability"),
            "capability alias retained"
        );
        assert_eq!(
            view.field_string(4).as_deref(),
            Some(capability_id.to_string().as_str()),
            "capability id stored"
        );

        let payload_value = view.field(5);
        let payload_view =
            record_with_label(&payload_value, "request").expect("payload record missing");
        assert_eq!(
            payload_view.field_string(0).as_deref(),
            Some("payload"),
            "payload contents preserved"
        );

        if view.len() > 6 {
            let props_value = view.field(6);
            let props_view =
                record_with_label(&props_value, "role-properties").expect("properties record");
            assert!(
                props_view.len() >= 1,
                "role properties should contain at least one entry"
            );
        }
    }

    #[test]
    fn interpreter_invoke_tool_unknown_role_errors() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let program = "(workflow bad-role)
               (state start
                 (emit (invoke-tool :role missing :capability \"capability\"))
                 (terminal))";

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        let err = entity
            .on_message(&mut activation, &payload)
            .expect_err("invoke-tool should fail when role is unknown");

        match err {
            ActorError::InvalidActivation(message) => {
                assert!(
                    message.contains("unknown role"),
                    "unexpected error message: {message}"
                );
            }
            other => panic!("expected InvalidActivation error, got {other:?}"),
        }

        let record = activation
            .assertions_added
            .iter()
            .filter_map(|(_, value)| InstanceRecord::parse(value))
            .find(|entry| matches!(entry.status, InstanceStatus::Failed(_)))
            .expect("failed instance record should be asserted");
        assert!(
            matches!(record.status, InstanceStatus::Failed(_)),
            "status must be failed when role is unknown"
        );
    }

    #[test]
    fn interpreter_invoke_tool_invalid_capability_id_errors() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = activation_for(&actor);

        let program = "(workflow bad-capability)
               (roles (workspace :capability \"not-a-uuid\"))
               (state start
                 (emit (invoke-tool :role workspace :capability \"capability\"))
                 (terminal))";

        let payload = IOValue::record(
            IOValue::symbol(RUN_MESSAGE_LABEL),
            vec![IOValue::new(program)],
        );

        let err = entity
            .on_message(&mut activation, &payload)
            .expect_err("invoke-tool should fail when capability id is invalid");

        match err {
            ActorError::InvalidActivation(message) => {
                assert!(
                    message.contains("capability UUID"),
                    "unexpected error message: {message}"
                );
            }
            other => panic!("expected InvalidActivation error, got {other:?}"),
        }

        let record = activation
            .assertions_added
            .iter()
            .filter_map(|(_, value)| InstanceRecord::parse(value))
            .find(|entry| matches!(entry.status, InstanceStatus::Failed(_)))
            .expect("failed instance record should be asserted");
        assert!(
            matches!(record.status, InstanceStatus::Failed(_)),
            "status must be failed when capability ID is invalid"
        );
    }
}
