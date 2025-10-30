use std::collections::HashMap;
use std::sync::Mutex;

use crate::interpreter::protocol::{
    OBSERVER_RECORD_LABEL, TOOL_REQUEST_RECORD_LABEL, TOOL_RESULT_RECORD_LABEL, WaitRecord,
    runtime_snapshot_from_value, runtime_snapshot_to_value, wait_record_to_value,
};
use crate::interpreter::{
    Action, Condition, DEFINE_MESSAGE_LABEL, DefinitionRecord, InstanceProgress, InstanceRecord,
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
    resume_pending: bool,
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
                    vec![IOValue::new(instance_id.clone()), entry.wait.as_value()],
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

        let mut ready_conditions = Vec::new();
        match &wait_status {
            WaitStatus::RecordFieldEq {
                label,
                field,
                value,
            } => {
                ready_conditions.push(WaitCondition::RecordFieldEq {
                    label: label.clone(),
                    field: *field,
                    value: value.clone(),
                });
            }
            WaitStatus::Signal { label } => {
                ready_conditions.push(WaitCondition::Signal {
                    label: label.clone(),
                });
            }
            WaitStatus::ToolResult { tag } => {
                ready_conditions.push(WaitCondition::ToolResult { tag: tag.clone() });
            }
        }

        let program_ref = waiting_entry.program_ref.clone();
        let program = waiting_entry.program.clone();
        let snapshot = waiting_entry.snapshot.clone();
        let status_handle = waiting_entry.handle.clone();
        let stored_assertions = waiting_entry.assertions.clone();
        let stored_facets = waiting_entry.facets.clone();

        let mut progress = InstanceProgress {
            state: program.states.first().map(|state| state.name.clone()),
            entry_pending: true,
            waiting: None,
            frame_depth: 0,
        };

        let mut status = InstanceStatus::Running;
        let mut next_wait: Option<WaitingInstance> = None;
        let mut result_error: Option<ActorError> = None;
        let mut pending_wait_status: Option<WaitStatus> = None;
        let mut pending_wait_snapshot: Option<RuntimeSnapshot> = None;
        let mut pending_wait_handle: Option<Handle> = None;
        let mut pending_wait_assertions: Option<Vec<RecordedAssertion>> = None;
        let mut pending_wait_facets: Option<Vec<FacetId>> = None;

        let host = ActivationHost::with_ready(
            activation,
            instance_id.clone(),
            &program.roles,
            ready_conditions,
            stored_assertions,
            stored_facets,
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
                Err(RuntimeError::InvalidCall(message)) => {
                    status = InstanceStatus::Failed(message.clone());
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    result_error = Some(ActorError::InvalidActivation(message));
                    break;
                }
            }
        }

        let observer_specs = runtime.host_mut().drain_observers();

        if let (Some(wait_status), Some(snapshot), Some(wait_handle)) = (
            pending_wait_status,
            pending_wait_snapshot,
            pending_wait_handle,
        ) {
            let facet = activation.current_facet.clone();
            let wait_record = wait_record_to_value(&WaitRecord {
                instance_id: instance_id.clone(),
                facet: facet.clone(),
                wait_status: wait_status.clone(),
            });
            activation.assert(wait_handle.clone(), wait_record);
            let assertions = pending_wait_assertions.take().unwrap_or_default();
            let facets = pending_wait_facets.take().unwrap_or_default();
            next_wait = Some(WaitingInstance {
                program_ref: program_ref.clone(),
                program: program.clone(),
                snapshot,
                wait: wait_status,
                handle: status_handle.clone(),
                wait_handle,
                facet,
                assertions,
                facets,
                resume_pending: false,
            });
        }

        for spec in observer_specs {
            self.register_observer(activation, spec)?;
        }

        activation.retract(waiting_entry.wait_handle.clone());
        let final_record = InstanceRecord {
            instance_id: instance_id.clone(),
            program: program_ref.clone(),
            program_name: program.name.clone(),
            state: progress.state.clone(),
            status: status.clone(),
            progress: Some(progress.clone()),
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
                        IOValue::symbol(if entry.resume_pending {
                            "true"
                        } else {
                            "false"
                        }),
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

                                let resume_pending = if wait_view.len() > 9 {
                                    wait_view
                                        .field(9)
                                        .as_symbol()
                                        .map(|sym| sym.as_ref() == "true")
                                        .unwrap_or(false)
                                } else {
                                    false
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
                                        resume_pending,
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
        let program_clone = program.clone();
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

        let running_record = InstanceRecord {
            instance_id: instance_id.clone(),
            program: program_ref_clone.clone(),
            program_name: program_clone.name.clone(),
            state: progress.state.clone(),
            status: status.clone(),
            progress: Some(progress.clone()),
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
                Err(RuntimeError::InvalidCall(message)) => {
                    status = InstanceStatus::Failed(message.clone());
                    progress.state = runtime.current_state_name();
                    progress.entry_pending = runtime.entry_pending();
                    progress.waiting = None;
                    progress.frame_depth = runtime.frame_depth();
                    result_error = Some(ActorError::InvalidActivation(message));
                    break;
                }
            }
        }

        let observer_specs = runtime.host_mut().drain_observers();
        for spec in observer_specs {
            self.register_observer(activation, spec)?;
        }

        if let (Some(wait_status), Some(snapshot), Some(wait_handle)) = (
            pending_wait_status,
            pending_wait_snapshot,
            pending_wait_handle,
        ) {
            let facet = activation.current_facet.clone();
            let wait_record = wait_record_to_value(&WaitRecord {
                instance_id: instance_id.clone(),
                facet: facet.clone(),
                wait_status: wait_status.clone(),
            });
            activation.assert(wait_handle.clone(), wait_record);
            let assertions = pending_wait_assertions.take().unwrap_or_default();
            let facets = pending_wait_facets.take().unwrap_or_default();
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
                resume_pending: false,
            });
        }

        let final_record = InstanceRecord {
            instance_id: instance_id.clone(),
            program: program_ref_clone.clone(),
            program_name: program_clone.name.clone(),
            state: progress.state.clone(),
            status: status.clone(),
            progress: Some(progress.clone()),
        };

        if let Some(entry) = waiting_entry {
            let mut waiting_guard = self.waiting.lock().unwrap();
            waiting_guard.insert(instance_id.clone(), entry);
        } else {
            self.waiting.lock().unwrap().remove(&instance_id);
        }

        activation.assert(status_handle, final_record.to_value());

        if let Some(err) = result_error {
            Err(err)
        } else {
            Ok(())
        }
    }
}

struct ActivationHost<'a> {
    activation: &'a mut Activation,
    satisfied: Vec<WaitCondition>,
    assertions: HashMap<IOValue, Vec<Handle>>,
    facets: Vec<FacetId>,
    observers: Vec<ObserverSpec>,
    instance_id: String,
    roles: HashMap<String, RoleBinding>,
}

#[derive(Clone)]
struct ObserverSpec {
    condition: WaitCondition,
    handler: ProgramRef,
    facet: FacetId,
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
            instance_id,
            roles: role_map,
        }
    }

    fn with_ready(
        activation: &'a mut Activation,
        instance_id: String,
        roles: &[RoleBinding],
        satisfied: Vec<WaitCondition>,
        assertions: Vec<RecordedAssertion>,
        facets: Vec<FacetId>,
    ) -> Self {
        let mut host = Self::new(activation, instance_id, roles);
        host.satisfied = satisfied;
        host.restore_assertions(&assertions);
        host.restore_facets(&facets);
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

    fn drain_observers(&mut self) -> Vec<ObserverSpec> {
        std::mem::take(&mut self.observers)
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
                let coerced = value.to_io_value();
                let handle = Handle::new();
                self.activation.assert(handle.clone(), coerced.clone());
                self.assertions.entry(coerced).or_default().push(handle);
                Ok(())
            }
            Action::Retract(value) => {
                let target = value.to_io_value();
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
            Action::Send {
                actor,
                facet,
                payload,
            } => {
                let actor_id = Uuid::parse_str(actor).map_err(|_| {
                    ActorError::InvalidActivation(format!(
                        "send requires :actor to be a UUID, got {actor}"
                    ))
                })?;
                let facet_id = Uuid::parse_str(facet).map_err(|_| {
                    ActorError::InvalidActivation(format!(
                        "send requires :facet to be a UUID, got {facet}"
                    ))
                })?;

                let payload_value = payload.to_io_value();
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
                self.facets.push(new_facet.clone());

                let record = IOValue::record(
                    IOValue::symbol("interpreter-facet"),
                    vec![IOValue::new(new_facet.0.to_string())],
                );
                let handle = Handle::new();
                self.activation.assert(handle.clone(), record.clone());
                self.assertions.entry(record).or_default().push(handle);
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

                let record = IOValue::record(
                    IOValue::symbol("interpreter-facet"),
                    vec![IOValue::new(facet.to_string())],
                );
                if let Some(handles) = self.assertions.get_mut(&record) {
                    if let Some(handle) = handles.pop() {
                        self.activation.retract(handle.clone());
                    }
                    if handles.is_empty() {
                        self.assertions.remove(&record);
                    }
                }
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

    fn check_condition(&mut self, condition: &Condition) -> std::result::Result<bool, Self::Error> {
        match condition {
            Condition::Signal { label } => Ok(self.satisfied.iter().any(
                |cond| matches!(cond, WaitCondition::Signal { label: ready } if ready == label),
            )),
        }
    }

    fn poll_wait(&mut self, wait: &WaitCondition) -> std::result::Result<bool, Self::Error> {
        if let Some(index) = self
            .satisfied
            .iter()
            .position(|cond| Self::condition_matches(cond, wait))
        {
            self.satisfied.remove(index);
            Ok(true)
        } else {
            Ok(false)
        }
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
    use crate::runtime::actor::Actor;
    use crate::runtime::turn::{ActorId, FacetId, TurnOutput};

    #[test]
    fn interpreter_entity_emits_log_and_prompt() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let program = "(workflow demo)
               (state start (action (assert (record agent-request \"planner\" \"hello\")) (log \"hello\")) (terminal))";
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let target_actor = ActorId::new();
        let target_facet = FacetId::new();
        let program = format!(
            "(workflow send-demo)\n(state start\n  (action (send :actor \"{}\" :facet \"{}\" :value (record ping \"hello\")))\n  (terminal))",
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
    fn interpreter_observe_signal_triggers_handler() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let handler_source = r#"(workflow observer-handler)
(state start
  (action (assert (record observed)))
  (terminal))"#;

        let program = format!(
            "(workflow observe-demo)\n(state start (action (observe (signal ready) \"{}\")) (terminal))",
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let handler_source = r#"(workflow observer-handler)
(state start
  (action (assert (record observed)))
  (terminal))"#;

        let program = format!(
            "(workflow observe-demo)\n(state start (action (observe (signal ready) \"{}\")) (terminal))",
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

        let mut resume_activation =
            Activation::new(actor.id.clone(), actor.root_facet.clone(), None);
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let program = "(workflow spawn-demo)\n(state start (action (spawn)) (terminal))";

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
    fn interpreter_stop_facet_action() {
        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let target_facet = FacetId::new();
        let program = format!(
            "(workflow stop-demo)\n(state start (action (stop :facet \"{}\")) (terminal))",
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

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

        let mut run_activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

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

        let mut run_activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let program = r#"(workflow wait-demo)
(state start
  (enter (log "start"))
  (await (signal ready))
  (goto done))
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

        let mut resume_activation =
            Activation::new(actor.id.clone(), actor.root_facet.clone(), None);
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let capability_id = Uuid::new_v4();
        let program = format!(
            "(workflow tool-demo)
               (roles (workspace :capability \"{}\" :agent-kind \"tester\"))
               (state start
                 (action (invoke-tool :role workspace :capability \"capability\" :payload (record request \"payload\") :tag tool-req))
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let program = "(workflow bad-role)
               (state start
                 (action (invoke-tool :role missing :capability \"capability\"))
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
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let program = "(workflow bad-capability)
               (roles (workspace :capability \"not-a-uuid\"))
               (state start
                 (action (invoke-tool :role workspace :capability \"capability\"))
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
