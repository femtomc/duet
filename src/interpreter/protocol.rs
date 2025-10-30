//! Dataspace protocol helpers for the interpreter entity.
//!
//! This module centralises the record layouts used to describe interpreter
//! definitions, instances, and control-plane messages within the syndicated
//! actor dataspace. Keeping the schema in one place helps the entity, service,
//! and CLI remain in lockstep while we iterate on the interpreter design.

use preserves::IOValue;
use serde::{Deserialize, Serialize};
use serde_json;
use std::collections::BTreeMap;
use std::convert::TryFrom;

use crate::interpreter::ir::RoleBinding;
use crate::interpreter::{RuntimeSnapshot, Value};
use crate::runtime::turn::FacetId;
use crate::util::io_value::{RecordView, as_record, record_with_label};

/// Message label used to publish a new interpreter definition.
pub const DEFINE_MESSAGE_LABEL: &str = "interpreter-define";
/// Message label used to request execution of a definition/program.
pub const RUN_MESSAGE_LABEL: &str = "interpreter-run";
/// Message label used to resume a waiting interpreter instance.
pub const RESUME_MESSAGE_LABEL: &str = "interpreter-resume";
/// Message label used to notify the interpreter about a new assertion.
pub const NOTIFY_MESSAGE_LABEL: &str = "interpreter-notify";
/// Dataspace label used for wait records.
const WAIT_RECORD_LABEL: &str = "interpreter-wait";
/// Dataspace label used for tool invocation requests.
pub const TOOL_REQUEST_RECORD_LABEL: &str = "interpreter-tool-request";
/// Dataspace label used for tool invocation results.
pub const TOOL_RESULT_RECORD_LABEL: &str = "interpreter-tool-result";
/// Dataspace label used for observer registrations.
pub const OBSERVER_RECORD_LABEL: &str = "interpreter-observer";

/// Dataspace label for persisted interpreter definitions.
pub const DEFINITION_RECORD_LABEL: &str = "interpreter-definition";
/// Dataspace label for interpreter instance status records.
pub const INSTANCE_RECORD_LABEL: &str = "interpreter-instance";
/// Dataspace label for interpreter log entries.
pub const LOG_RECORD_LABEL: &str = "interpreter-log";
/// Dataspace label for interpreter entity spawn records.
pub const ENTITY_RECORD_LABEL: &str = "interpreter-entity";

const ROLES_RECORD_LABEL: &str = "roles";
const ROLE_BINDING_RECORD_LABEL: &str = "role-binding";
const ROLE_PROPERTY_RECORD_LABEL: &str = "role-property";
const ROLE_PROPERTIES_RECORD_LABEL: &str = "role-properties";

fn role_properties_to_value(props: &BTreeMap<String, String>) -> Option<IOValue> {
    if props.is_empty() {
        return None;
    }

    let entries = props
        .iter()
        .map(|(key, value)| {
            IOValue::record(
                IOValue::symbol(ROLE_PROPERTY_RECORD_LABEL),
                vec![IOValue::symbol(key.clone()), IOValue::new(value.clone())],
            )
        })
        .collect();

    Some(IOValue::record(
        IOValue::symbol(ROLE_PROPERTIES_RECORD_LABEL),
        entries,
    ))
}

fn role_properties_from_value(value: &IOValue) -> BTreeMap<String, String> {
    let mut props = BTreeMap::new();
    if let Some(view) = record_with_label(value, ROLE_PROPERTIES_RECORD_LABEL) {
        for idx in 0..view.len() {
            let entry = view.field(idx);
            if let Some(prop_view) = record_with_label(&entry, ROLE_PROPERTY_RECORD_LABEL) {
                if prop_view.len() >= 2 {
                    if let (Some(key), Some(val)) =
                        (prop_view.field_symbol(0), prop_view.field_string(1))
                    {
                        props.insert(key.to_string(), val);
                    }
                }
            }
        }
    }
    props
}

fn role_binding_to_value(binding: &RoleBinding) -> IOValue {
    let mut fields = vec![IOValue::new(binding.name.clone())];
    if let Some(props) = role_properties_to_value(&binding.properties) {
        fields.push(props);
    }
    IOValue::record(IOValue::symbol(ROLE_BINDING_RECORD_LABEL), fields)
}

fn role_binding_from_value(value: &IOValue) -> Option<RoleBinding> {
    let view = record_with_label(value, ROLE_BINDING_RECORD_LABEL)?;
    if view.len() == 0 {
        return None;
    }
    let name = view.field_string(0)?;
    let properties = if view.len() > 1 {
        role_properties_from_value(&view.field(1))
    } else {
        BTreeMap::new()
    };
    Some(RoleBinding { name, properties })
}

fn roles_to_value(roles: &[RoleBinding]) -> Option<IOValue> {
    if roles.is_empty() {
        return None;
    }

    let entries = roles.iter().map(role_binding_to_value).collect();

    Some(IOValue::record(
        IOValue::symbol(ROLES_RECORD_LABEL),
        entries,
    ))
}

fn roles_from_value(value: &IOValue) -> Vec<RoleBinding> {
    let mut roles = Vec::new();
    if let Some(view) = record_with_label(value, ROLES_RECORD_LABEL) {
        for idx in 0..view.len() {
            let entry = view.field(idx);
            if let Some(binding) = role_binding_from_value(&entry) {
                roles.push(binding);
            }
        }
    }
    roles
}

/// Parsed representation of an interpreter definition record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DefinitionRecord {
    /// Stable identifier assigned to the definition.
    pub definition_id: String,
    /// Program name supplied in the source.
    pub program_name: String,
    /// Original source text (S-expression program).
    pub source: String,
}

impl DefinitionRecord {
    /// Encode the definition record as a preserves value suitable for asserting
    /// into the dataspace.
    pub fn to_value(&self) -> IOValue {
        let fields = vec![
            IOValue::new(self.definition_id.clone()),
            IOValue::new(self.program_name.clone()),
            IOValue::new(self.source.clone()),
        ];

        IOValue::record(IOValue::symbol(DEFINITION_RECORD_LABEL), fields)
    }

    /// Attempt to parse an interpreter definition from a dataspace value.
    pub fn parse(value: &IOValue) -> Option<Self> {
        let record = record_with_label(value, DEFINITION_RECORD_LABEL)?;
        if record.len() < 3 {
            return None;
        }

        let definition_id = record.field_string(0)?;
        let program_name = record.field_string(1)?;
        let source = record.field_string(2)?;

        Some(Self {
            definition_id,
            program_name,
            source,
        })
    }
}

/// Current lifecycle status of an interpreter instance.
#[derive(Debug, Clone, PartialEq)]
pub enum InstanceStatus {
    /// Program is actively executing instructions.
    Running,
    /// Program is paused waiting for an external condition.
    Waiting(WaitStatus),
    /// Program completed successfully.
    Completed,
    /// Program aborted with an error.
    Failed(String),
}

impl InstanceStatus {
    /// Encode the status as a preserves value for dataspace storage.
    pub fn to_value(&self) -> IOValue {
        match self {
            InstanceStatus::Running => IOValue::symbol("running"),
            InstanceStatus::Waiting(wait) => {
                IOValue::record(IOValue::symbol("waiting"), vec![wait.as_value()])
            }
            InstanceStatus::Completed => IOValue::symbol("completed"),
            InstanceStatus::Failed(message) => IOValue::record(
                IOValue::symbol("failed"),
                vec![IOValue::new(message.clone())],
            ),
        }
    }

    pub fn parse(value: &IOValue) -> Option<Self> {
        if let Some(sym) = value.as_symbol() {
            return match sym.as_ref() {
                "running" => Some(InstanceStatus::Running),
                "completed" => Some(InstanceStatus::Completed),
                _ => None,
            };
        }

        if let Some(record) = record_with_label(value, "waiting") {
            if record.len() == 0 {
                return None;
            }
            let wait_value = record.field(0);
            let wait_record = as_record(&wait_value)?;
            let wait = WaitStatus::parse_record(wait_record)?;
            return Some(InstanceStatus::Waiting(wait));
        }

        if let Some(record) = record_with_label(value, "failed") {
            let message = if record.len() > 0 {
                record
                    .field_string(0)
                    .unwrap_or_else(|| "unknown failure".to_string())
            } else {
                "unknown failure".to_string()
            };
            return Some(InstanceStatus::Failed(message));
        }

        None
    }
}

/// Details of the condition an instance is waiting on.
#[derive(Debug, Clone, PartialEq)]
pub enum WaitStatus {
    /// Waiting for a record field to equal a value.
    RecordFieldEq {
        /// Record label that must appear.
        label: String,
        /// Field index that must match.
        field: usize,
        /// Expected field value.
        value: Value,
    },
    /// Waiting for a dataspace signal labelled accordingly.
    Signal {
        /// Signal label that must appear in the dataspace.
        label: String,
    },
    /// Waiting for a tool invocation result tagged accordingly.
    ToolResult {
        /// Correlation tag associated with the tool invocation.
        tag: String,
    },
}

impl WaitStatus {
    pub fn as_value(&self) -> IOValue {
        match self {
            WaitStatus::RecordFieldEq {
                label,
                field,
                value,
            } => IOValue::record(
                IOValue::symbol("record-field-eq"),
                vec![
                    IOValue::symbol(label.clone()),
                    IOValue::new(*field as i64),
                    value.to_io_value(),
                ],
            ),
            WaitStatus::Signal { label } => IOValue::record(
                IOValue::symbol("signal"),
                vec![IOValue::symbol(label.clone())],
            ),
            WaitStatus::ToolResult { tag } => IOValue::record(
                IOValue::symbol("tool-result"),
                vec![IOValue::new(tag.clone())],
            ),
        }
    }

    pub fn parse_record(record: RecordView<'_>) -> Option<Self> {
        if record.has_label("signal") {
            if record.len() == 0 {
                return None;
            }
            let label = record.field_symbol(0)?;
            return Some(WaitStatus::Signal { label });
        }

        if record.has_label("tool-result") {
            if record.len() == 0 {
                return None;
            }
            let tag = record.field_string(0)?;
            return Some(WaitStatus::ToolResult { tag });
        }

        if record.has_label("record-field-eq") {
            if record.len() < 3 {
                return None;
            }
            let label = record.field_symbol(0)?;
            let field_value = record.field(1);
            let field_index = field_value.as_signed_integer()?;
            let field_index = i64::try_from(field_index.as_ref()).ok()?;
            if field_index < 0 {
                return None;
            }
            let expected_value = record.field(2);
            let value = Value::from_io_value(&expected_value)?;
            return Some(WaitStatus::RecordFieldEq {
                label,
                field: field_index as usize,
                value,
            });
        }

        None
    }

    /// Convert a runtime wait condition into a protocol wait status.
    pub fn from_condition(wait: &crate::interpreter::ir::WaitCondition) -> Self {
        match wait {
            crate::interpreter::ir::WaitCondition::RecordFieldEq {
                label,
                field,
                value,
            } => WaitStatus::RecordFieldEq {
                label: label.clone(),
                field: *field,
                value: value.clone(),
            },
            crate::interpreter::ir::WaitCondition::Signal { label } => WaitStatus::Signal {
                label: label.clone(),
            },
            crate::interpreter::ir::WaitCondition::ToolResult { tag } => {
                WaitStatus::ToolResult { tag: tag.clone() }
            }
        }
    }

    /// Convert the status back into a concrete wait condition.
    pub fn into_condition(self) -> crate::interpreter::ir::WaitCondition {
        match self {
            WaitStatus::RecordFieldEq {
                label,
                field,
                value,
            } => crate::interpreter::ir::WaitCondition::RecordFieldEq {
                label,
                field,
                value,
            },
            WaitStatus::Signal { label } => crate::interpreter::ir::WaitCondition::Signal { label },
            WaitStatus::ToolResult { tag } => {
                crate::interpreter::ir::WaitCondition::ToolResult { tag }
            }
        }
    }
}

/// Wait record stored in the dataspace for automatic wake-up.
#[derive(Debug, Clone, PartialEq)]
pub struct WaitRecord {
    /// Interpreter instance identifier.
    pub instance_id: String,
    /// Facet that should receive the resume message.
    pub facet: FacetId,
    /// Condition the interpreter is waiting on.
    pub wait_status: WaitStatus,
}

/// Encode a wait record as a preserves value.
pub fn wait_record_to_value(record: &WaitRecord) -> IOValue {
    IOValue::record(
        IOValue::symbol(WAIT_RECORD_LABEL),
        vec![
            IOValue::new(record.instance_id.clone()),
            IOValue::new(record.facet.0.to_string()),
            record.wait_status.as_value(),
        ],
    )
}

/// Parse a wait record from a preserves value.
pub fn wait_record_from_value(value: &IOValue) -> Option<WaitRecord> {
    let record = record_with_label(value, WAIT_RECORD_LABEL)?;
    if record.len() < 3 {
        return None;
    }

    let instance_id = record.field_string(0)?;
    let facet_id = record.field_string(1)?;
    let facet_uuid = uuid::Uuid::parse_str(&facet_id).ok()?;
    let wait_status = as_record(&record.field(2)).and_then(WaitStatus::parse_record)?;

    Some(WaitRecord {
        instance_id,
        facet: FacetId::from_uuid(facet_uuid),
        wait_status,
    })
}

/// Snapshot of interpreter execution progress.
#[derive(Debug, Clone, PartialEq)]
pub struct InstanceProgress {
    /// Current state name (if known).
    pub state: Option<String>,
    /// Whether the runtime is about to execute state entry actions.
    pub entry_pending: bool,
    /// Outstanding wait condition (if the runtime is paused).
    pub waiting: Option<WaitStatus>,
    /// Depth of the frame stack (useful for debugging loops/branches).
    pub frame_depth: usize,
}

impl InstanceProgress {
    pub fn to_value(&self) -> IOValue {
        let state_value = self
            .state
            .as_ref()
            .map(|name| IOValue::new(name.clone()))
            .unwrap_or_else(|| IOValue::symbol("unknown"));
        let waiting_value = self
            .waiting
            .as_ref()
            .map(|wait| wait.as_value())
            .unwrap_or_else(|| IOValue::symbol("none"));
        let entry_value = if self.entry_pending {
            IOValue::symbol("true")
        } else {
            IOValue::symbol("false")
        };

        IOValue::record(
            IOValue::symbol("interpreter-progress"),
            vec![
                state_value,
                entry_value,
                waiting_value,
                IOValue::new(self.frame_depth as i64),
            ],
        )
    }

    pub fn parse(value: &IOValue) -> Option<Self> {
        let record = record_with_label(value, "interpreter-progress")?;
        if record.len() < 4 {
            return None;
        }

        let state = match record.field(0).as_string() {
            Some(text) => Some(text.to_string()),
            None => None,
        };
        let entry_pending = match record.field_symbol(1).as_deref() {
            Some("true") => true,
            Some("false") => false,
            _ => false,
        };

        let waiting = if let Some(wait_record) = as_record(&record.field(2)) {
            WaitStatus::parse_record(wait_record)
        } else {
            None
        };

        let frame_depth = record
            .field(3)
            .as_signed_integer()
            .and_then(|value| i64::try_from(value.as_ref()).ok())
            .and_then(|value| usize::try_from(value).ok())
            .unwrap_or(0);

        Some(Self {
            state,
            entry_pending,
            waiting,
            frame_depth,
        })
    }
}

/// Dataspace representation of an interpreter instance.
#[derive(Debug, Clone, PartialEq)]
pub struct InstanceRecord {
    /// Stable identifier for the instance.
    pub instance_id: String,
    /// Program reference associated with the instance.
    pub program: ProgramRef,
    /// Program name for reference.
    pub program_name: String,
    /// Current state (if known).
    pub state: Option<String>,
    /// Lifecycle status.
    pub status: InstanceStatus,
    /// Execution progress snapshot.
    pub progress: Option<InstanceProgress>,
    /// Role bindings (including runtime-populated properties).
    pub roles: Vec<RoleBinding>,
}

impl InstanceRecord {
    /// Encode the instance record into a preserves value.
    pub fn to_value(&self) -> IOValue {
        let mut fields = vec![
            IOValue::new(self.instance_id.clone()),
            self.program.to_value(),
            IOValue::new(self.program_name.clone()),
        ];

        if let Some(state) = &self.state {
            fields.push(IOValue::new(state.clone()));
        } else {
            fields.push(IOValue::symbol("unknown"));
        }

        fields.push(self.status.to_value());

        if let Some(progress) = &self.progress {
            fields.push(progress.to_value());
        }

        if let Some(roles_value) = roles_to_value(&self.roles) {
            fields.push(roles_value);
        }

        IOValue::record(IOValue::symbol(INSTANCE_RECORD_LABEL), fields)
    }

    /// Attempt to parse an instance record from a preserves value.
    pub fn parse(value: &IOValue) -> Option<Self> {
        let record = record_with_label(value, INSTANCE_RECORD_LABEL)?;
        if record.len() < 5 {
            return None;
        }

        let instance_id = record.field_string(0)?;
        let program = ProgramRef::parse(&record.field(1))?;
        let program_name = record.field_string(2)?;

        let state = match record.field(3).as_string() {
            Some(text) => Some(text.to_string()),
            None => None,
        };

        let status = InstanceStatus::parse(&record.field(4))?;

        let mut next_index = 5;
        let mut progress = None;
        if record.len() > next_index {
            let candidate = record.field(next_index);
            if let Some(parsed) = InstanceProgress::parse(&candidate) {
                progress = Some(parsed);
                next_index += 1;
            }
        }

        let mut roles = Vec::new();
        if record.len() > next_index {
            roles = roles_from_value(&record.field(next_index));
        }

        Some(Self {
            instance_id,
            program,
            program_name,
            state,
            status,
            progress,
            roles,
        })
    }
}
/// Reference to the program executed by an interpreter instance.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ProgramRef {
    /// Reference a previously defined program by identifier.
    Definition(String),
    /// Inline program source attached directly to the instance.
    Inline(String),
}

impl ProgramRef {
    pub fn to_value(&self) -> IOValue {
        match self {
            ProgramRef::Definition(id) => IOValue::record(
                IOValue::symbol("definition"),
                vec![IOValue::new(id.clone())],
            ),
            ProgramRef::Inline(source) => IOValue::record(
                IOValue::symbol("inline"),
                vec![IOValue::new(source.clone())],
            ),
        }
    }

    pub fn parse(value: &IOValue) -> Option<Self> {
        if let Some(view) = record_with_label(value, "definition") {
            if view.len() >= 1 {
                if let Some(id) = view.field_string(0) {
                    return Some(ProgramRef::Definition(id));
                }
            }
            return None;
        }

        if let Some(view) = record_with_label(value, "inline") {
            if view.len() >= 1 {
                if let Some(source) = view.field_string(0) {
                    return Some(ProgramRef::Inline(source));
                }
            }
            return None;
        }

        None
    }
}

/// Encode a runtime snapshot as a preserves value.
pub fn runtime_snapshot_to_value(snapshot: &RuntimeSnapshot) -> IOValue {
    let encoded = serde_json::to_string(snapshot).expect("serialize runtime snapshot");
    IOValue::new(encoded)
}

/// Decode a runtime snapshot from a preserves value.
pub fn runtime_snapshot_from_value(value: &IOValue) -> Option<RuntimeSnapshot> {
    let text = value.as_string()?;
    serde_json::from_str(text.as_ref()).ok()
}
