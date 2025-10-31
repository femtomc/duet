use crate::interpreter::ir::{
    Action, ActionTemplate, BranchArm, Condition, Instruction, InstructionTemplate, ProgramIr,
    WaitCondition, WaitConditionTemplate,
};
use crate::interpreter::value::{Value, ValueContext, ValueExpr};
use preserves::IOValue;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Snapshot of the interpreter runtime's internal state.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RuntimeSnapshot {
    /// Index of the active state within the program.
    pub state_index: usize,
    /// Whether the interpreter still needs to execute entry actions.
    pub entry_pending: bool,
    /// Outstanding wait condition (if any).
    pub waiting: Option<WaitCondition>,
    /// Stack of in-flight instruction frames.
    pub frames: Vec<FrameSnapshot>,
    /// Whether execution has finished.
    pub completed: bool,
    /// Value associated with the most recently satisfied wait, if any.
    pub last_wait_value: Option<Value>,
}

/// Snapshot of a single interpreter frame.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FrameSnapshot {
    /// Instructions owned by the frame.
    pub instructions: Vec<Instruction>,
    /// Current instruction index.
    pub index: usize,
    /// Frame execution mode.
    pub kind: FrameKindSnapshot,
}

/// Snapshot representation of the frame kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum FrameKindSnapshot {
    /// Frame executes instructions once.
    Normal,
    /// Frame loops until interrupted.
    Loop,
}

/// Host trait implemented by runtimes that execute interpreter programs.
pub trait InterpreterHost: ValueContext {
    /// Error type surfaced by host operations.
    type Error;

    /// Execute the provided action.
    fn execute_action(&mut self, action: &Action) -> std::result::Result<(), Self::Error>;
    /// Evaluate a branch condition.
    fn check_condition(&mut self, condition: &Condition) -> std::result::Result<bool, Self::Error>;
    /// Poll whether a wait condition has been satisfied.
    fn poll_wait(&mut self, wait: &WaitCondition) -> std::result::Result<bool, Self::Error>;
    /// Obtain the value associated with the most recently satisfied wait, if any.
    fn take_ready_value(&mut self) -> Option<IOValue>;
}

/// Outcome of a `tick` call on the interpreter runtime.
#[derive(Debug, Clone, PartialEq)]
pub enum RuntimeEvent {
    /// Progress was made (action executed, branch evaluated, etc.).
    Progress,
    /// The runtime is waiting on the supplied condition.
    Waiting(WaitCondition),
    /// The runtime transitioned between states.
    Transition {
        /// State exited by the interpreter.
        from: String,
        /// State entered by the interpreter.
        to: String,
    },
    /// Program execution completed.
    Completed,
}

/// Errors surfaced while executing a program.
#[derive(Debug)]
pub enum RuntimeError<E> {
    /// Host-level error bubbled up from an action/condition call.
    Host(E),
    /// Program referenced a state that does not exist.
    UnknownState(String),
    /// Program contained no states.
    NoStates,
    /// Function invocation failed (unknown function, arity mismatch).
    InvalidCall(String),
}

/// Stateful interpreter runtime that drives IR programs against a host.
pub struct InterpreterRuntime<H> {
    host: H,
    program: ProgramIr,
    state_index: usize,
    frames: Vec<Frame>,
    waiting: Option<WaitCondition>,
    entry_pending: bool,
    completed: bool,
    last_wait_value: Option<Value>,
}

impl<H: InterpreterHost> InterpreterRuntime<H> {
    /// Create a new runtime for the provided program and host.
    pub fn new(host: H, program: ProgramIr) -> Self {
        Self {
            host,
            program,
            state_index: 0,
            frames: Vec::new(),
            waiting: None,
            entry_pending: true,
            completed: false,
            last_wait_value: None,
        }
    }

    /// Execute until one meaningful event occurs (progress, wait, transition, completion).
    pub fn tick(&mut self) -> Result<RuntimeEvent, RuntimeError<H::Error>> {
        if self.completed {
            return Ok(RuntimeEvent::Completed);
        }
        if self.program.states.is_empty() {
            self.completed = true;
            return Err(RuntimeError::NoStates);
        }

        // If we are currently waiting, poll the host to see if the wait condition is satisfied.
        if let Some(wait) = self.waiting.clone() {
            if self.host.poll_wait(&wait).map_err(RuntimeError::Host)? {
                self.waiting = None;
                if let Some(value) = self.host.take_ready_value() {
                    self.last_wait_value = Value::from_io_value(&value);
                }
                if let Some(frame) = self.frames.last_mut() {
                    if frame.index < frame.instructions.len() {
                        if let Instruction::Await(cond) = &frame.instructions[frame.index] {
                            if cond == &wait {
                                frame.index += 1;
                                return Ok(RuntimeEvent::Progress);
                            }
                        }
                    }
                }
            } else {
                self.last_wait_value = None;
                return Ok(RuntimeEvent::Waiting(wait));
            }
        }

        loop {
            // Initialise state entry actions if pending.
            if self.entry_pending {
                let state = self.program.states.get(self.state_index).ok_or_else(|| {
                    RuntimeError::UnknownState(format!("state index {}", self.state_index))
                })?;
                for action in &state.entry {
                    self.host
                        .execute_action(action)
                        .map_err(RuntimeError::Host)?;
                }
                self.entry_pending = false;
                self.frames.clear();
                if !state.body.is_empty() {
                    self.frames
                        .push(Frame::new(state.body.clone(), FrameKind::Normal));
                }

                if self.frames.is_empty() {
                    if state.terminal {
                        self.completed = true;
                        return Ok(RuntimeEvent::Completed);
                    } else {
                        return self.advance_state();
                    }
                }

                return Ok(RuntimeEvent::Progress);
            }

            if self.frames.is_empty() {
                return self.advance_state();
            }

            let frame = self.frames.last_mut().unwrap();
            if frame.index >= frame.instructions.len() {
                match frame.kind {
                    FrameKind::Normal => {
                        self.frames.pop();
                        continue;
                    }
                    FrameKind::Loop => {
                        frame.index = 0;
                        continue;
                    }
                }
            }

            let instr = frame.instructions[frame.index].clone();
            match instr {
                Instruction::Action(action) => {
                    self.host
                        .execute_action(&action)
                        .map_err(RuntimeError::Host)?;
                    frame.index += 1;
                    return Ok(RuntimeEvent::Progress);
                }
                Instruction::Await(wait) => {
                    if self.host.poll_wait(&wait).map_err(RuntimeError::Host)? {
                        self.waiting = None;
                        if let Some(value) = self.host.take_ready_value() {
                            self.last_wait_value = Value::from_io_value(&value);
                        }
                        frame.index += 1;
                        return Ok(RuntimeEvent::Progress);
                    } else {
                        self.waiting = Some(wait.clone());
                        self.last_wait_value = None;
                        return Ok(RuntimeEvent::Waiting(wait));
                    }
                }
                Instruction::Branch { arms, otherwise } => {
                    frame.index += 1;
                    let mut executed = false;
                    for arm in arms {
                        if self
                            .host
                            .check_condition(&arm.condition)
                            .map_err(RuntimeError::Host)?
                        {
                            if !arm.body.is_empty() {
                                self.frames
                                    .push(Frame::new(arm.body.clone(), FrameKind::Normal));
                            }
                            executed = true;
                            break;
                        }
                    }
                    if !executed {
                        if let Some(body) = otherwise {
                            if !body.is_empty() {
                                self.frames.push(Frame::new(body, FrameKind::Normal));
                            }
                        }
                    }
                    return Ok(RuntimeEvent::Progress);
                }
                Instruction::Loop(body) => {
                    frame.index += 1;
                    if !body.is_empty() {
                        self.frames.push(Frame::new(body, FrameKind::Loop));
                    }
                    return Ok(RuntimeEvent::Progress);
                }
                Instruction::Call { function, args } => {
                    frame.index += 1;
                    let mut resolved_args = Vec::new();
                    for arg in args {
                        resolved_args.push(
                            self.resolve_call_argument(&arg)
                                .map_err(RuntimeError::InvalidCall)?,
                        );
                    }
                    let instructions = self
                        .instantiate_function(function, resolved_args)
                        .map_err(RuntimeError::InvalidCall)?;
                    if !instructions.is_empty() {
                        self.frames
                            .push(Frame::new(instructions, FrameKind::Normal));
                    }
                    return Ok(RuntimeEvent::Progress);
                }
                Instruction::Transition(target) => {
                    frame.index += 1;
                    let from = self
                        .program
                        .states
                        .get(self.state_index)
                        .map(|s| s.name.clone())
                        .unwrap_or_default();
                    self.set_state(&target)?;
                    return Ok(RuntimeEvent::Transition { from, to: target });
                }
            }
        }
    }

    fn advance_state(&mut self) -> Result<RuntimeEvent, RuntimeError<H::Error>> {
        let current_name = self
            .program
            .states
            .get(self.state_index)
            .map(|s| s.name.clone())
            .unwrap_or_default();

        if self
            .program
            .states
            .get(self.state_index)
            .map(|s| s.terminal)
            .unwrap_or(false)
        {
            self.completed = true;
            return Ok(RuntimeEvent::Completed);
        }

        if self.state_index + 1 >= self.program.states.len() {
            self.completed = true;
            return Ok(RuntimeEvent::Completed);
        }

        self.state_index += 1;
        self.entry_pending = true;
        self.waiting = None;
        self.frames.clear();
        let to_name = self.program.states[self.state_index].name.clone();
        Ok(RuntimeEvent::Transition {
            from: current_name,
            to: to_name,
        })
    }

    fn set_state(&mut self, target: &str) -> Result<(), RuntimeError<H::Error>> {
        if let Some((idx, _)) = self
            .program
            .states
            .iter()
            .enumerate()
            .find(|(_, state)| state.name == target)
        {
            self.state_index = idx;
            self.entry_pending = true;
            self.waiting = None;
            self.frames.clear();
            Ok(())
        } else {
            Err(RuntimeError::UnknownState(target.to_string()))
        }
    }

    /// Expose a reference to the underlying program.
    pub fn program(&self) -> &ProgramIr {
        &self.program
    }

    /// Mutable access to the underlying program.
    pub fn program_mut(&mut self) -> &mut ProgramIr {
        &mut self.program
    }

    /// Access the host (useful for inspection in tests).
    pub fn host(&self) -> &H {
        &self.host
    }

    /// Mutable access to the host, allowing tests to satisfy waits.
    pub fn host_mut(&mut self) -> &mut H {
        &mut self.host
    }

    /// Name of the current state (if any).
    pub fn current_state_name(&self) -> Option<String> {
        self.program
            .states
            .get(self.state_index)
            .map(|state| state.name.clone())
    }

    /// Whether the runtime still needs to execute entry actions for the current state.
    pub fn entry_pending(&self) -> bool {
        self.entry_pending
    }

    /// Current wait condition, if the runtime is paused.
    pub fn waiting_condition(&self) -> Option<&WaitCondition> {
        self.waiting.as_ref()
    }

    /// Depth of the interpreter frame stack.
    pub fn frame_depth(&self) -> usize {
        self.frames.len()
    }

    /// Capture the current execution snapshot.
    pub fn snapshot(&self) -> RuntimeSnapshot {
        RuntimeSnapshot {
            state_index: self.state_index,
            entry_pending: self.entry_pending,
            waiting: self.waiting.clone(),
            frames: self.frames.iter().map(Frame::to_snapshot).collect(),
            completed: self.completed,
            last_wait_value: self.last_wait_value.clone(),
        }
    }

    /// Restore a runtime from a previously captured snapshot.
    pub fn from_snapshot(host: H, program: ProgramIr, snapshot: RuntimeSnapshot) -> Self {
        let mut runtime = InterpreterRuntime::new(host, program);
        runtime.state_index = snapshot.state_index;
        runtime.entry_pending = snapshot.entry_pending;
        runtime.waiting = snapshot.waiting;
        runtime.frames = snapshot
            .frames
            .into_iter()
            .map(Frame::from_snapshot)
            .collect();
        runtime.completed = snapshot.completed;
        runtime.last_wait_value = snapshot.last_wait_value;
        runtime
    }

    fn instantiate_function(
        &self,
        index: usize,
        args: Vec<Value>,
    ) -> Result<Vec<Instruction>, String> {
        let function = self
            .program
            .functions
            .get(index)
            .ok_or_else(|| format!("unknown function index {}", index))?;
        if function.params.len() != args.len() {
            return Err(format!(
                "function '{}' expects {} arguments, received {}",
                function.name,
                function.params.len(),
                args.len()
            ));
        }

        let mut bindings = HashMap::new();
        for (param, value) in function.params.iter().cloned().zip(args.into_iter()) {
            bindings.insert(param, value);
        }

        self.instantiate_templates(&function.body, &bindings)
    }

    fn instantiate_templates(
        &self,
        templates: &[InstructionTemplate],
        bindings: &HashMap<String, Value>,
    ) -> Result<Vec<Instruction>, String> {
        let mut instructions = Vec::new();
        for template in templates {
            match template {
                InstructionTemplate::Action(action_template) => {
                    instructions.push(Instruction::Action(
                        self.instantiate_action(action_template, bindings)?,
                    ));
                }
                InstructionTemplate::Await(wait) => {
                    instructions.push(Instruction::Await(self.instantiate_wait(wait, bindings)?));
                }
                InstructionTemplate::Branch { arms, otherwise } => {
                    let mut instantiated_arms = Vec::new();
                    for arm in arms {
                        instantiated_arms.push(BranchArm {
                            condition: arm.condition.clone(),
                            body: self.instantiate_templates(&arm.body, bindings)?,
                        });
                    }
                    let instantiated_otherwise = if let Some(body) = otherwise {
                        Some(self.instantiate_templates(body, bindings)?)
                    } else {
                        None
                    };
                    instructions.push(Instruction::Branch {
                        arms: instantiated_arms,
                        otherwise: instantiated_otherwise,
                    });
                }
                InstructionTemplate::Loop(body) => {
                    let instantiated_body = self.instantiate_templates(body, bindings)?;
                    instructions.push(Instruction::Loop(instantiated_body));
                }
                InstructionTemplate::Transition(target) => {
                    instructions.push(Instruction::Transition(target.clone()));
                }
                InstructionTemplate::Call { function, args } => {
                    let mut resolved_args = Vec::new();
                    for arg in args {
                        let value = self.resolve_expr(arg, bindings)?;
                        resolved_args.push(ValueExpr::Literal(value));
                    }
                    instructions.push(Instruction::Call {
                        function: *function,
                        args: resolved_args,
                    });
                }
            }
        }
        Ok(instructions)
    }

    fn instantiate_action(
        &self,
        template: &ActionTemplate,
        bindings: &HashMap<String, Value>,
    ) -> Result<Action, String> {
        match template {
            ActionTemplate::InvokeTool {
                role,
                capability,
                payload,
                tag,
            } => Ok(Action::InvokeTool {
                role: role.clone(),
                capability: capability.clone(),
                payload: if let Some(expr) = payload {
                    Some(self.resolve_expr(expr, bindings)?)
                } else {
                    None
                },
                tag: tag.clone(),
            }),
            ActionTemplate::Send {
                actor,
                facet,
                payload,
            } => Ok(Action::Send {
                actor: self
                    .resolve_expr(actor, bindings)?
                    .as_str()
                    .ok_or_else(|| "send :actor must resolve to a string".to_string())?
                    .to_string(),
                facet: self
                    .resolve_expr(facet, bindings)?
                    .as_str()
                    .ok_or_else(|| "send :facet must resolve to a string".to_string())?
                    .to_string(),
                payload: self.resolve_expr(payload, bindings)?,
            }),
            ActionTemplate::Observe { label, handler } => Ok(Action::Observe {
                label: label.clone(),
                handler: handler.clone(),
            }),
            ActionTemplate::Spawn { parent } => Ok(Action::Spawn {
                parent: parent.clone(),
            }),
            ActionTemplate::SpawnEntity {
                role,
                entity_type,
                agent_kind,
                config,
            } => Ok(Action::SpawnEntity {
                role: role.clone(),
                entity_type: entity_type.clone(),
                agent_kind: agent_kind.clone(),
                config: match config {
                    Some(expr) => Some(self.resolve_expr(expr, bindings)?),
                    None => None,
                },
            }),
            ActionTemplate::AttachEntity {
                role,
                facet,
                entity_type,
                agent_kind,
                config,
            } => Ok(Action::AttachEntity {
                role: role.clone(),
                facet: if let Some(expr) = facet {
                    Some(
                        self.resolve_expr(expr, bindings)?
                            .as_str()
                            .ok_or_else(|| {
                                "attach-entity :facet must resolve to a string".to_string()
                            })?
                            .to_string(),
                    )
                } else {
                    None
                },
                entity_type: entity_type.clone(),
                agent_kind: agent_kind.clone(),
                config: match config {
                    Some(expr) => Some(self.resolve_expr(expr, bindings)?),
                    None => None,
                },
            }),
            ActionTemplate::GenerateRequestId { role, property } => Ok(Action::GenerateRequestId {
                role: role.clone(),
                property: property.clone(),
            }),
            ActionTemplate::Stop { facet } => Ok(Action::Stop {
                facet: facet.clone(),
            }),
            ActionTemplate::Log(message) => Ok(Action::Log(message.clone())),
            ActionTemplate::Assert(value_expr) => {
                Ok(Action::Assert(self.resolve_expr(value_expr, bindings)?))
            }
            ActionTemplate::Retract(value_expr) => {
                Ok(Action::Retract(self.resolve_expr(value_expr, bindings)?))
            }
        }
    }

    fn instantiate_wait(
        &self,
        template: &WaitConditionTemplate,
        bindings: &HashMap<String, Value>,
    ) -> Result<WaitCondition, String> {
        match template {
            WaitConditionTemplate::RecordFieldEq {
                label,
                field,
                value,
            } => Ok(WaitCondition::RecordFieldEq {
                label: label.clone(),
                field: *field,
                value: self.resolve_expr(value, bindings)?,
            }),
            WaitConditionTemplate::Signal { label } => Ok(WaitCondition::Signal {
                label: label.clone(),
            }),
            WaitConditionTemplate::ToolResult { tag } => {
                let resolved = self.resolve_expr(tag, bindings)?;
                let tag_str = resolved
                    .as_str()
                    .ok_or_else(|| "tool-result wait :tag must resolve to a string".to_string())?;
                Ok(WaitCondition::ToolResult {
                    tag: tag_str.to_string(),
                })
            }
        }
    }

    fn resolve_expr(
        &self,
        expr: &ValueExpr,
        bindings: &HashMap<String, Value>,
    ) -> Result<Value, String> {
        expr.resolve(bindings, self.last_wait_value.as_ref(), &self.host)
            .map_err(|err| err.to_string())
    }

    fn resolve_call_argument(&self, expr: &ValueExpr) -> Result<Value, String> {
        let bindings = HashMap::new();
        expr.resolve(&bindings, self.last_wait_value.as_ref(), &self.host)
            .map_err(|err| err.to_string())
    }
}

#[derive(Clone)]
struct Frame {
    instructions: Vec<Instruction>,
    index: usize,
    kind: FrameKind,
}

#[derive(Clone)]
enum FrameKind {
    Normal,
    Loop,
}

impl Frame {
    fn new(instructions: Vec<Instruction>, kind: FrameKind) -> Self {
        Self {
            instructions,
            index: 0,
            kind,
        }
    }

    fn to_snapshot(&self) -> FrameSnapshot {
        FrameSnapshot {
            instructions: self.instructions.clone(),
            index: self.index,
            kind: match self.kind {
                FrameKind::Normal => FrameKindSnapshot::Normal,
                FrameKind::Loop => FrameKindSnapshot::Loop,
            },
        }
    }

    fn from_snapshot(snapshot: FrameSnapshot) -> Self {
        let kind = match snapshot.kind {
            FrameKindSnapshot::Normal => FrameKind::Normal,
            FrameKindSnapshot::Loop => FrameKind::Loop,
        };
        Self {
            instructions: snapshot.instructions,
            index: snapshot.index,
            kind,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::interpreter::ir::{
        Action, ActionTemplate, Function, Instruction, InstructionTemplate, ProgramIr, RoleBinding,
        State, WaitCondition, WaitConditionTemplate,
    };
    use crate::interpreter::value::{ValueContext, ValueExpr};
    use std::collections::BTreeMap;

    #[derive(Default)]
    struct MockHost {
        actions: Vec<Action>,
        ready_wait: Option<WaitCondition>,
        ready_flag: bool,
        ready_value: Option<IOValue>,
        role_props: std::collections::HashMap<(String, String), String>,
    }

    impl MockHost {
        fn with_ready(wait: WaitCondition) -> Self {
            Self {
                actions: Vec::new(),
                ready_wait: Some(wait),
                ready_flag: true,
                ready_value: None,
                role_props: std::collections::HashMap::new(),
            }
        }
    }

    impl InterpreterHost for MockHost {
        type Error = ();

        fn execute_action(&mut self, action: &Action) -> std::result::Result<(), Self::Error> {
            self.actions.push(action.clone());
            Ok(())
        }

        fn check_condition(
            &mut self,
            condition: &Condition,
        ) -> std::result::Result<bool, Self::Error> {
            match condition {
                Condition::Signal { label } => match &self.ready_wait {
                    Some(WaitCondition::Signal { label: ready })
                        if ready == label && self.ready_flag =>
                    {
                        Ok(true)
                    }
                    _ => Ok(false),
                },
            }
        }

        fn poll_wait(&mut self, wait: &WaitCondition) -> std::result::Result<bool, Self::Error> {
            if self.ready_flag {
                if let Some(ready) = &self.ready_wait {
                    if ready == wait {
                        self.ready_flag = false;
                        return Ok(true);
                    }
                }
            }
            Ok(false)
        }

        fn take_ready_value(&mut self) -> Option<IOValue> {
            self.ready_value.take()
        }
    }

    impl ValueContext for MockHost {
        fn role_property(&self, role: &str, key: &str) -> Option<String> {
            self.role_props
                .get(&(role.to_string(), key.to_string()))
                .cloned()
        }
    }

    fn simple_program() -> ProgramIr {
        ProgramIr {
            name: "demo".into(),
            metadata: BTreeMap::new(),
            roles: vec![RoleBinding {
                name: "planner".into(),
                properties: BTreeMap::new(),
            }],
            states: vec![
                State {
                    name: "plan".into(),
                    entry: vec![Action::Assert(Value::Record {
                        label: "agent-request".into(),
                        fields: vec![
                            Value::String("planner".into()),
                            Value::String("write code".into()),
                            Value::String("req".into()),
                        ],
                    })],
                    body: vec![
                        Instruction::Await(WaitCondition::RecordFieldEq {
                            label: "agent-response".into(),
                            field: 0,
                            value: Value::String("req".into()),
                        }),
                        Instruction::Transition("complete".into()),
                    ],
                    terminal: false,
                },
                State {
                    name: "complete".into(),
                    entry: Vec::new(),
                    body: Vec::new(),
                    terminal: true,
                },
            ],
            functions: Vec::new(),
        }
    }

    #[test]
    #[ignore]
    fn runs_basic_program_with_wait() {
        let program = simple_program();
        let mut runtime = InterpreterRuntime::new(MockHost::default(), program.clone());

        // Entry action executes immediately.
        assert_eq!(runtime.tick().unwrap(), RuntimeEvent::Progress);
        assert_eq!(runtime.host.actions.len(), 1);

        // Await not yet satisfied -> waiting.
        let wait = match runtime.tick().unwrap() {
            RuntimeEvent::Waiting(wait) => {
                assert_eq!(
                    wait,
                    WaitCondition::RecordFieldEq {
                        label: "agent-response".into(),
                        field: 0,
                        value: Value::String("req".into()),
                    }
                );
                wait
            }
            other => panic!("expected waiting, got {:?}", other),
        };

        let snapshot = runtime.snapshot();

        let mut resumed = InterpreterRuntime::from_snapshot(
            MockHost::with_ready(wait.clone()),
            program,
            snapshot,
        );

        // Resume wait, advance execution past the await.
        match resumed.tick().unwrap() {
            RuntimeEvent::Progress => {}
            other => panic!("expected progress after resuming wait, got {:?}", other),
        }

        // Next tick transitions to the terminal state.
        match resumed.tick().unwrap() {
            RuntimeEvent::Transition { from, to } => {
                assert_eq!(from, "plan");
                assert_eq!(to, "complete");
            }
            other => panic!("expected transition, got {:?}", other),
        }

        // Final tick completes program.
        assert_eq!(resumed.tick().unwrap(), RuntimeEvent::Completed);
    }

    #[test]
    #[ignore]
    fn function_waits_using_parameter_value() {
        let program = ProgramIr {
            name: "demo".into(),
            metadata: BTreeMap::new(),
            roles: Vec::new(),
            states: vec![
                State {
                    name: "start".into(),
                    entry: Vec::new(),
                    body: vec![
                        Instruction::Call {
                            function: 0,
                            args: vec![ValueExpr::Literal(Value::String("req".into()))],
                        },
                        Instruction::Transition("done".into()),
                    ],
                    terminal: false,
                },
                State {
                    name: "done".into(),
                    entry: Vec::new(),
                    body: Vec::new(),
                    terminal: true,
                },
            ],
            functions: vec![Function {
                name: "wait-tag".into(),
                params: vec!["tag".into()],
                body: vec![
                    InstructionTemplate::Await(WaitConditionTemplate::RecordFieldEq {
                        label: "agent-response".into(),
                        field: 0,
                        value: ValueExpr::Parameter("tag".into()),
                    }),
                    InstructionTemplate::Action(ActionTemplate::Log("done".into())),
                ],
            }],
        };

        let mut runtime = InterpreterRuntime::new(MockHost::default(), program.clone());

        // Execute call instruction (instantiates function frame).
        assert_eq!(runtime.tick().unwrap(), RuntimeEvent::Progress);

        // Function awaits agent response tagged with argument.
        let wait = match runtime.tick().unwrap() {
            RuntimeEvent::Waiting(wait) => wait,
            other => panic!("expected waiting, got {:?}", other),
        };
        assert_eq!(
            wait,
            WaitCondition::RecordFieldEq {
                label: "agent-response".into(),
                field: 0,
                value: Value::String("req".into()),
            }
        );

        // Satisfy wait by posting matching record.
        let snapshot = runtime.snapshot();
        let mut resumed = InterpreterRuntime::from_snapshot(
            MockHost::with_ready(wait.clone()),
            program,
            snapshot,
        );

        // Resume execution and run the log action emitted by the function.
        assert_eq!(resumed.tick().unwrap(), RuntimeEvent::Progress);
        assert_eq!(resumed.host.actions.len(), 1);
        match &resumed.host.actions[0] {
            Action::Log(message) => assert_eq!(message, "done"),
            other => panic!("expected log action, got {:?}", other),
        }

        // Transition to terminal state and finish program.
        match resumed.tick().unwrap() {
            RuntimeEvent::Transition { to, .. } => assert_eq!(to, "done"),
            other => panic!("expected transition, got {:?}", other),
        }
        assert_eq!(resumed.tick().unwrap(), RuntimeEvent::Completed);
    }

    #[test]
    fn function_call_receives_dynamic_argument() {
        let wait_condition = WaitCondition::RecordFieldEq {
            label: "agent-response".into(),
            field: 0,
            value: Value::String("req-42".into()),
        };

        let program = ProgramIr {
            name: "demo".into(),
            metadata: BTreeMap::new(),
            roles: vec![RoleBinding {
                name: "planner".into(),
                properties: BTreeMap::new(),
            }],
            states: vec![
                State {
                    name: "start".into(),
                    entry: Vec::new(),
                    body: vec![
                        Instruction::Await(wait_condition.clone()),
                        Instruction::Call {
                            function: 0,
                            args: vec![
                                ValueExpr::RoleProperty {
                                    role: "planner".into(),
                                    key: "label".into(),
                                },
                                ValueExpr::LastWaitField { index: 2 },
                            ],
                        },
                        Instruction::Transition("done".into()),
                    ],
                    terminal: false,
                },
                State {
                    name: "done".into(),
                    entry: Vec::new(),
                    body: Vec::new(),
                    terminal: true,
                },
            ],
            functions: vec![Function {
                name: "record-turn".into(),
                params: vec!["label".into(), "response".into()],
                body: vec![InstructionTemplate::Action(ActionTemplate::Assert(
                    ValueExpr::Record {
                        label: "conversation-turn".into(),
                        fields: vec![
                            ValueExpr::Parameter("label".into()),
                            ValueExpr::Parameter("response".into()),
                        ],
                    },
                ))],
            }],
        };

        let response_value = IOValue::record(
            IOValue::symbol("agent-response"),
            vec![
                IOValue::new("req-42".to_string()),
                IOValue::new("prompt"),
                IOValue::new("tabs forever"),
            ],
        );

        let mut runtime = InterpreterRuntime::new(MockHost::default(), program);

        runtime
            .host_mut()
            .role_props
            .insert(("planner".into(), "label".into()), "Planner".into());

        match runtime.tick().unwrap() {
            RuntimeEvent::Progress => {}
            other => panic!("expected entry progress, got {:?}", other),
        }

        match runtime.tick().unwrap() {
            RuntimeEvent::Waiting(cond) => assert_eq!(cond, wait_condition),
            other => panic!("expected waiting, got {:?}", other),
        }

        {
            let host = runtime.host_mut();
            host.ready_wait = Some(wait_condition.clone());
            host.ready_flag = true;
            host.ready_value = Some(response_value);
        }

        match runtime.tick().unwrap() {
            RuntimeEvent::Progress => {}
            other => panic!("expected progress executing call stack, got {:?}", other),
        }

        match runtime.tick().unwrap() {
            RuntimeEvent::Progress => {}
            other => panic!("expected progress executing function body, got {:?}", other),
        }

        match runtime.tick().unwrap() {
            RuntimeEvent::Transition { to, .. } => assert_eq!(to, "done"),
            RuntimeEvent::Progress => {}
            other => panic!(
                "expected progress or transition after function body, got {:?}",
                other
            ),
        }

        assert_eq!(runtime.host.actions.len(), 1);
        match &runtime.host.actions[0] {
            Action::Assert(Value::Record { label, fields }) => {
                assert_eq!(label, "conversation-turn");
                assert_eq!(fields.len(), 2);
                assert_eq!(fields[0], Value::String("Planner".into()));
                assert_eq!(fields[1], Value::String("tabs forever".into()));
            }
            other => panic!("expected conversation-turn record, got {:?}", other),
        }
    }
}
