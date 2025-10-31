use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

use crate::interpreter::ProgramRef;

use super::value::{Value, ValueExpr};

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
    /// Function definitions available to the runtime.
    pub functions: Vec<Function>,
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
    /// Actions executed immediately upon entering the state.
    pub entry: Vec<Action>,
    /// Body instructions (actions, awaits, branches).
    pub body: Vec<Instruction>,
    /// Whether the state terminates the program.
    pub terminal: bool,
}

/// Instructions allowed within a state body.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Instruction {
    /// Perform an action immediately.
    Action(Action),
    /// Wait until a condition is satisfied.
    Await(WaitCondition),
    /// Conditional branch with optional `otherwise` body.
    Branch {
        /// Conditional arms evaluated in order.
        arms: Vec<BranchArm>,
        /// Fallback body executed when no conditions match.
        otherwise: Option<Vec<Instruction>>,
    },
    /// Repeat the enclosed instructions until a transition breaks out.
    Loop(Vec<Instruction>),
    /// Transition to another named state.
    Transition(String),
    /// Invoke a function by index with resolved argument values.
    Call {
        /// Function index inside `ProgramIr::functions`.
        function: usize,
        /// Resolved argument values supplied to the call.
        args: Vec<ValueExpr>,
    },
}

/// One arm of a conditional branch.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BranchArm {
    /// Condition to evaluate for this arm.
    pub condition: Condition,
    /// Body executed when the condition holds.
    pub body: Vec<Instruction>,
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
        actor: String,
        /// Target facet identifier (UUID string).
        facet: String,
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
}

/// Function definition stored in the IR.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Function {
    /// Function name.
    pub name: String,
    /// Parameter names in order.
    pub params: Vec<String>,
    /// Compiled instruction templates (instantiated at call time).
    pub body: Vec<InstructionTemplate>,
}

/// Instruction template used inside function bodies.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum InstructionTemplate {
    /// Template action.
    Action(ActionTemplate),
    /// Wait condition (no templating required).
    Await(WaitConditionTemplate),
    /// Branch with templated bodies.
    Branch {
        /// Conditional arms evaluated during instantiation.
        arms: Vec<BranchArmTemplate>,
        /// Optional fallback body instantiated when no conditions match.
        otherwise: Option<Vec<InstructionTemplate>>,
    },
    /// Loop with templated body.
    Loop(Vec<InstructionTemplate>),
    /// Transition (no templating required).
    Transition(String),
    /// Nested function call awaiting instantiation.
    Call {
        /// Target function index inside the program definition list.
        function: usize,
        /// Templated arguments resolved when the function is invoked.
        args: Vec<ValueExpr>,
    },
}

/// Branch arm template.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BranchArmTemplate {
    /// Condition is fully concrete (no parameter support yet).
    pub condition: Condition,
    /// Body instructions to instantiate.
    pub body: Vec<InstructionTemplate>,
}

/// Action template capable of referencing parameters.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum ActionTemplate {
    /// Invoke a capability exposed by another entity using templated inputs.
    InvokeTool {
        /// Role responsible for the invocation (typically bound to an entity).
        role: String,
        /// Capability identifier resolved at build time.
        capability: String,
        /// Structured payload expression evaluated at runtime.
        payload: Option<ValueExpr>,
        /// Optional correlation tag to associate with results.
        tag: Option<String>,
    },
    /// Send a message to another actor/facet with templated addressing.
    Send {
        /// Target actor expression.
        actor: ValueExpr,
        /// Target facet expression.
        facet: ValueExpr,
        /// Payload expression evaluated at runtime.
        payload: ValueExpr,
    },
    /// Observe a dataspace signal and run a handler program.
    Observe {
        /// Signal label to watch for.
        label: String,
        /// Handler program reference to execute.
        handler: ProgramRef,
    },
    /// Spawn a child facet using the provided parent expression.
    Spawn {
        /// Optional parent facet identifier captured at build time.
        parent: Option<String>,
    },
    /// Spawn a new entity/actor and bind it to a role with templated config.
    SpawnEntity {
        /// Role whose properties will receive the spawned identifiers.
        role: String,
        /// Optional explicit entity type identifier.
        entity_type: Option<String>,
        /// Agent kind identifier used to infer the entity type.
        agent_kind: Option<String>,
        /// Configuration payload expression evaluated at runtime.
        config: Option<ValueExpr>,
    },
    /// Attach an entity to an existing facet with templated fields.
    AttachEntity {
        /// Role whose properties will be updated with attached identifiers.
        role: String,
        /// Optional facet expression selecting the attachment target.
        facet: Option<ValueExpr>,
        /// Optional explicit entity type identifier.
        entity_type: Option<String>,
        /// Agent kind identifier used when deriving the entity type.
        agent_kind: Option<String>,
        /// Configuration payload expression evaluated at runtime.
        config: Option<ValueExpr>,
    },
    /// Generate a request identifier for a role property.
    GenerateRequestId {
        /// Role whose request counter should be incremented.
        role: String,
        /// Property name that receives the generated identifier.
        property: String,
    },
    /// Terminate a facet selected via an expression.
    Stop {
        /// Facet identifier captured at build time.
        facet: String,
    },
    /// Write a diagnostic log string.
    Log(String),
    /// Assert a structured value into the dataspace.
    Assert(ValueExpr),
    /// Retract a structured value from the dataspace.
    Retract(ValueExpr),
}

/// Templated wait condition that may reference parameters.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum WaitConditionTemplate {
    /// Wait for a record field to match a value expression.
    RecordFieldEq {
        /// Record label to match.
        label: String,
        /// Field index that must equal the resolved value.
        field: usize,
        /// Value expression resolved at call time.
        value: ValueExpr,
    },
    /// Wait for a dataspace signal label.
    Signal {
        /// Signal label to match.
        label: String,
    },
    /// Wait for a tool invocation result bearing the supplied tag.
    ToolResult {
        /// Tag expression evaluated at instantiation time.
        tag: ValueExpr,
    },
    /// Wait for interactive user input surfaced via the CLI.
    UserInput {
        /// Prompt payload presented to the user.
        prompt: ValueExpr,
        /// Optional tag used to correlate the response.
        tag: Option<ValueExpr>,
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

/// Conditional expressions used in branches.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Condition {
    /// Await a dataspace assertion labelled accordingly.
    Signal {
        /// Label to match in the dataspace.
        label: String,
    },
}
