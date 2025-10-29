use std::collections::BTreeMap;

/// Fully validated interpreter program ready for execution.
#[derive(Debug, Clone, PartialEq)]
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
#[derive(Debug, Clone, PartialEq)]
pub struct RoleBinding {
    /// Role name referenced by the program.
    pub name: String,
    /// Arbitrary key/value properties (agent kind, handles, etc.).
    pub properties: BTreeMap<String, String>,
}

/// A named state in the program's state machine.
#[derive(Debug, Clone, PartialEq)]
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
#[derive(Debug, Clone, PartialEq)]
pub enum Instruction {
    /// Perform an action immediately.
    Action(Action),
    /// Wait until a condition is satisfied.
    Await(WaitCondition),
    /// Conditional or looping branch.
    Branch(Vec<BranchArm>, Option<Vec<Instruction>>),
}

/// One arm of a conditional branch.
#[derive(Debug, Clone, PartialEq)]
pub struct BranchArm {
    /// Condition to evaluate for this arm.
    pub condition: Condition,
    /// Body executed when the condition holds.
    pub body: Vec<Instruction>,
}

/// Primitive actions emitted by interpreter programs.
#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    /// Send a prompt to a role-bound agent.
    SendPrompt {
        /// Role to deliver the prompt to.
        agent_role: String,
        /// Template text for the prompt (arguments handled later).
        template: String,
        /// Optional tag correlating the prompt with later responses.
        tag: Option<String>,
    },
    /// Invoke a capability exposed by another entity.
    InvokeTool {
        /// Role responsible for the invocation (e.g., workspace entity).
        role: String,
        /// Capability identifier.
        capability: String,
        /// Optional correlation tag.
        tag: Option<String>,
    },
    /// Emit a diagnostic log string.
    EmitLog(String),
    /// Assert a value into the dataspace (encoded as text for now).
    Assert(String),
    /// Retract a value from the dataspace (encoded as text for now).
    Retract(String),
}

/// Conditions that may be awaited.
#[derive(Debug, Clone, PartialEq)]
pub enum WaitCondition {
    /// Wait for an agent transcript response matching the tag.
    TranscriptResponse {
        /// Correlation tag (typically emitted by `send-prompt`).
        tag: String,
    },
    /// Wait for a generic dataspace signal (label match).
    Signal {
        /// Label to match in the dataspace.
        label: String,
    },
}

/// Conditional expressions used in branches.
#[derive(Debug, Clone, PartialEq)]
pub enum Condition {
    /// Await a dataspace assertion labelled accordingly.
    Signal {
        /// Label to match in the dataspace.
        label: String,
    },
}
