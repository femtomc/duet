use crate::interpreter::ir::{Action, Condition, Instruction, ProgramIr, WaitCondition};

/// Host trait implemented by runtimes that execute interpreter programs.
pub trait InterpreterHost {
    /// Error type surfaced by host operations.
    type Error;

    /// Execute the provided action.
    fn execute_action(&mut self, action: &Action) -> std::result::Result<(), Self::Error>;
    /// Evaluate a branch condition.
    fn check_condition(&mut self, condition: &Condition) -> std::result::Result<bool, Self::Error>;
    /// Poll whether a wait condition has been satisfied.
    fn poll_wait(&mut self, wait: &WaitCondition) -> std::result::Result<bool, Self::Error>;
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
            if self
                .host
                .poll_wait(&wait)
                .map_err(RuntimeError::Host)?
            {
                self.waiting = None;
            } else {
                return Ok(RuntimeEvent::Waiting(wait));
            }
        }

        loop {
            // Initialise state entry actions if pending.
            if self.entry_pending {
                let state = self
                    .program
                    .states
                    .get(self.state_index)
                    .ok_or_else(|| RuntimeError::UnknownState(format!("state index {}", self.state_index)))?;
                for action in &state.entry {
                    self
                        .host
                        .execute_action(action)
                        .map_err(RuntimeError::Host)?;
                }
                self.entry_pending = false;
                self.frames.clear();
                if !state.body.is_empty() {
                    self.frames.push(Frame::new(state.body.clone(), FrameKind::Normal));
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
                    self
                        .host
                        .execute_action(&action)
                        .map_err(RuntimeError::Host)?;
                    frame.index += 1;
                    return Ok(RuntimeEvent::Progress);
                }
                Instruction::Await(wait) => {
                    if self
                        .host
                        .poll_wait(&wait)
                        .map_err(RuntimeError::Host)?
                    {
                        frame.index += 1;
                        continue;
                    } else {
                        self.waiting = Some(wait.clone());
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
                                self.frames.push(Frame::new(arm.body.clone(), FrameKind::Normal));
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
                Instruction::Transition(target) => {
                    frame.index += 1;
                    let from = self
                        .program
                        .states
                        .get(self.state_index)
                        .map(|s| s.name.clone())
                        .unwrap_or_default();
                    self.set_state(&target)?;
                    return Ok(RuntimeEvent::Transition {
                        from,
                        to: target,
                    });
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

    /// Access the host (useful for inspection in tests).
    pub fn host(&self) -> &H {
        &self.host
    }

    /// Mutable access to the host, allowing tests to satisfy waits.
    pub fn host_mut(&mut self) -> &mut H {
        &mut self.host
    }
}

struct Frame {
    instructions: Vec<Instruction>,
    index: usize,
    kind: FrameKind,
}

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
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::interpreter::ir::{Action, Instruction, ProgramIr, RoleBinding, State, WaitCondition};
    use std::collections::{BTreeMap, HashSet};

    #[derive(Default)]
    struct MockHost {
        actions: Vec<Action>,
        ready_responses: HashSet<String>,
        ready_signals: HashSet<String>,
    }

    impl InterpreterHost for MockHost {
        type Error = ();

        fn execute_action(&mut self, action: &Action) -> std::result::Result<(), Self::Error> {
            self.actions.push(action.clone());
            Ok(())
        }

        fn check_condition(&mut self, condition: &Condition) -> std::result::Result<bool, Self::Error> {
            match condition {
                Condition::Signal { label } => Ok(self.ready_signals.contains(label)),
            }
        }

        fn poll_wait(&mut self, wait: &WaitCondition) -> std::result::Result<bool, Self::Error> {
            match wait {
                WaitCondition::TranscriptResponse { tag } => Ok(self.ready_responses.contains(tag)),
                WaitCondition::Signal { label } => Ok(self.ready_signals.contains(label)),
            }
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
                    entry: vec![Action::SendPrompt {
                        agent_role: "planner".into(),
                        template: "write code".into(),
                        tag: Some("req".into()),
                    }],
                    body: vec![
                        Instruction::Await(WaitCondition::TranscriptResponse { tag: "req".into() }),
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
        }
    }

    #[test]
    fn runs_basic_program_with_wait() {
        let program = simple_program();
        let host = MockHost::default();
        let mut runtime = InterpreterRuntime::new(host, program);

        // Entry action executes immediately.
        assert_eq!(runtime.tick().unwrap(), RuntimeEvent::Progress);
        assert_eq!(runtime.host.actions.len(), 1);

        // Await not yet satisfied -> waiting.
        match runtime.tick().unwrap() {
            RuntimeEvent::Waiting(wait) => {
                assert!(matches!(wait, WaitCondition::TranscriptResponse { .. }));
            }
            other => panic!("expected waiting, got {:?}", other),
        }

        runtime
            .host_mut()
            .ready_responses
            .insert("req".into());

        // Resume wait, transition to complete state.
        match runtime.tick().unwrap() {
            RuntimeEvent::Transition { from, to } => {
                assert_eq!(from, "plan");
                assert_eq!(to, "complete");
            }
            other => panic!("expected transition, got {:?}", other),
        }

        // Final tick completes program.
        assert_eq!(runtime.tick().unwrap(), RuntimeEvent::Completed);
    }
}
