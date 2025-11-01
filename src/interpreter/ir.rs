use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

use crate::interpreter::ProgramRef;

use super::value::Value;

/// Fully validated interpreter program ready for execution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProgramIr {
    /// Program identifier.
    pub name: String,
    /// Optional metadata fields (labels, descriptions, etc.).
    pub metadata: BTreeMap<String, String>,
    /// Declared role bindings.
    pub roles: Vec<RoleBinding>,
    /// Ordered state machine for the program.
    pub states: Vec<State>,
}

/// Binding between a logical role and its properties.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RoleBinding {
    /// Role name referenced by the program.
    pub name: String,
    /// Arbitrary key/value properties (agent kind, handles, etc.).
    pub properties: BTreeMap<String, String>,
}

/// A named state in the program's state machine.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct State {
    /// Unique state name.
    pub name: String,
    /// Ordered commands executed by the interpreter.
    pub commands: Vec<Command>,
    /// Whether the state terminates the program.
    pub terminal: bool,
}

/// Commands allowed within a state.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Command {
    /// Perform an action immediately.
    Emit(Action),
    /// Wait until a condition is satisfied.
    Await(WaitCondition),
    /// Transition to another named state.
    Transition(String),
}

/// Primitive actions emitted by interpreter programs.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Action {
    /// Invoke a capability exposed by another entity.
    InvokeTool {
        /// Role responsible for the invocation (e.g., workspace entity).
        role: String,
        /// Capability identifier.
        capability: String,
        /// Structured payload supplied to the tool.
        payload: Option<Value>,
        /// Optional correlation tag.
        tag: Option<String>,
    },
    /// Send a message to another actor/facet.
    Send {
        /// Target actor identifier (UUID string).
        actor: Value,
        /// Target facet identifier (UUID string).
        facet: Value,
        /// Payload to deliver.
        payload: Value,
    },
    /// Observe a dataspace signal and run a handler program when it appears.
    Observe {
        /// Signal label to watch for.
        label: String,
        /// Program to run each time the signal is observed.
        handler: ProgramRef,
    },
    /// Spawn a child facet (optionally under a specific parent).
    Spawn {
        /// Optional parent facet identifier; defaults to current facet when `None`.
        parent: Option<String>,
    },
    /// Spawn a new entity/actor and bind it to a role.
    SpawnEntity {
        /// Role whose properties will be updated with the spawned entity identifiers.
        role: String,
        /// Explicit entity type identifier, when provided.
        entity_type: Option<String>,
        /// Agent kind identifier, used to derive the entity type when supplied.
        agent_kind: Option<String>,
        /// Optional configuration payload supplied to the entity.
        config: Option<Value>,
    },
    /// Attach an entity to an existing facet within the current actor.
    AttachEntity {
        /// Role whose properties will be updated with the attached entity identifiers.
        role: String,
        /// Optional facet identifier (UUID string) to attach to; defaults to current facet.
        facet: Option<String>,
        /// Explicit entity type identifier, when provided.
        entity_type: Option<String>,
        /// Agent kind identifier, used to derive the entity type when supplied.
        agent_kind: Option<String>,
        /// Optional configuration payload supplied to the entity.
        config: Option<Value>,
    },
    /// Generate a request identifier for a role and store it as a property.
    GenerateRequestId {
        /// Role whose request counter should be incremented.
        role: String,
        /// Property name that will store the generated identifier.
        property: String,
    },
    /// Terminate a facet by identifier.
    Stop {
        /// Facet identifier (UUID string) to terminate.
        facet: String,
    },
    /// Write a diagnostic log string into the dataspace.
    Log(String),
    /// Assert a structured value into the dataspace.
    Assert(Value),
    /// Retract a structured value from the dataspace.
    Retract(Value),
    /// Register a dataspace pattern on behalf of a role-bound entity.
    RegisterPattern {
        /// Role whose bound entity should watch for the pattern.
        role: String,
        /// Pattern expression asserted on behalf of the entity.
        pattern: Value,
        /// Optional role property that will store the generated pattern id.
        property: Option<String>,
    },
    /// Remove a previously registered pattern.
    UnregisterPattern {
        /// Role whose pattern subscription should be removed.
        role: String,
        /// Identifier of the pattern to remove (string/UUID).
        pattern: Option<Value>,
        /// Role property that should be cleared; defaults to `agent-request-pattern`.
        property: Option<String>,
    },
    /// Detach an entity currently bound to a role.
    DetachEntity {
        /// Role whose bound entity should be detached.
        role: String,
    },
}

/// Conditions that may be awaited.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum WaitCondition {
    /// Wait for a dataspace record whose field equals the provided value.
    RecordFieldEq {
        /// Record label to match.
        label: String,
        /// Positional field index that must equal the supplied value.
        field: usize,
        /// Expected field value.
        value: Value,
    },
    /// Wait for a generic dataspace signal (label match).
    Signal {
        /// Label to match in the dataspace.
        label: String,
    },
    /// Wait for a tool invocation result bearing the supplied tag.
    ToolResult {
        /// Tag that identifies the awaited result.
        tag: String,
    },
    /// Wait for interactive user input surfaced via the CLI.
    UserInput {
        /// Prompt payload presented to the user.
        prompt: Value,
        /// Optional correlation tag associated with the request.
        tag: Option<String>,
        /// Deterministic request identifier assigned at runtime.
        request_id: Option<String>,
    },
}
