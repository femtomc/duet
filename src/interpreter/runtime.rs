use crate::interpreter::ir::{Action, Command, ProgramIr, WaitCondition};
use crate::interpreter::value::{Value, ValueContext};
use preserves::IOValue;
use serde::{Deserialize, Serialize};

/// Snapshot of the interpreter runtime's internal state.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RuntimeSnapshot {
    /// Index of the active state within the program.
    pub state_index: usize,
    /// Index of the next command within the current state.
    pub command_index: usize,
    /// Outstanding wait condition (if any).
    pub waiting: Option<WaitCondition>,
    /// Whether execution has finished.
    pub completed: bool,
    /// Value associated with the most recently satisfied wait, if any.
    pub last_wait_value: Option<Value>,
}

/// Host trait implemented by runtimes that execute interpreter programs.
pub trait InterpreterHost: ValueContext {
    /// Error type surfaced by host operations.
    type Error;

    /// Execute the provided action.
    fn execute_action(&mut self, action: &Action) -> std::result::Result<(), Self::Error>;
    /// Prepare a wait condition before it is evaluated (assign identifiers, emit side effects).
    fn prepare_wait(&mut self, _wait: &mut WaitCondition) -> std::result::Result<(), Self::Error> {
        Ok(())
    }
    /// Poll whether a wait condition has been satisfied.
    fn poll_wait(&mut self, wait: &WaitCondition) -> std::result::Result<bool, Self::Error>;
    /// Obtain the value associated with the most recently satisfied wait, if any.
    fn take_ready_value(&mut self) -> Option<IOValue>;
}

/// Outcome of a `tick` call on the interpreter runtime.
#[derive(Debug, Clone, PartialEq)]
pub enum RuntimeEvent {
    /// Progress was made (action executed or wait satisfied).
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
    /// Host-level error bubbled up from an action or wait call.
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
    command_index: usize,
    waiting: Option<WaitCondition>,
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
            command_index: 0,
            waiting: None,
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

        if let Some(wait) = self.waiting.clone() {
            if self.host.poll_wait(&wait).map_err(RuntimeError::Host)? {
                self.waiting = None;
                if let Some(value) = self.host.take_ready_value() {
                    self.last_wait_value = Value::from_io_value(&value);
                }
                self.command_index = self.command_index.saturating_add(1);
            } else {
                self.last_wait_value = None;
                return Ok(RuntimeEvent::Waiting(wait));
            }
        }

        loop {
            let state = self.program.states.get(self.state_index).ok_or_else(|| {
                RuntimeError::UnknownState(format!("state index {}", self.state_index))
            })?;

            if self.command_index >= state.commands.len() {
                if state.terminal {
                    self.completed = true;
                    return Ok(RuntimeEvent::Completed);
                } else {
                    return self.advance_state();
                }
            }

            let command = state.commands[self.command_index].clone();

            match command {
                Command::Emit(action) => {
                    self.host
                        .execute_action(&action)
                        .map_err(RuntimeError::Host)?;
                    self.command_index += 1;
                    return Ok(RuntimeEvent::Progress);
                }
                Command::Await(wait) => {
                    let mut prepared = wait.clone();
                    self.host
                        .prepare_wait(&mut prepared)
                        .map_err(RuntimeError::Host)?;
                    if self.host.poll_wait(&prepared).map_err(RuntimeError::Host)? {
                        self.waiting = None;
                        if let Some(value) = self.host.take_ready_value() {
                            self.last_wait_value = Value::from_io_value(&value);
                        }
                        self.command_index += 1;
                        return Ok(RuntimeEvent::Progress);
                    } else {
                        self.waiting = Some(prepared.clone());
                        self.last_wait_value = None;
                        return Ok(RuntimeEvent::Waiting(prepared));
                    }
                }
                Command::Transition(ref target) => {
                    let from = state.name.clone();
                    self.set_state(target)?;
                    return Ok(RuntimeEvent::Transition {
                        from,
                        to: target.clone(),
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

        if self.state_index + 1 >= self.program.states.len() {
            self.completed = true;
            return Ok(RuntimeEvent::Completed);
        }

        self.state_index += 1;
        self.command_index = 0;
        self.waiting = None;
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
            self.command_index = 0;
            self.waiting = None;
            Ok(())
        } else {
            Err(RuntimeError::UnknownState(target.to_string()))
        }
    }

    /// Capture the current execution snapshot.
    pub fn snapshot(&self) -> RuntimeSnapshot {
        RuntimeSnapshot {
            state_index: self.state_index,
            command_index: self.command_index,
            waiting: self.waiting.clone(),
            completed: self.completed,
            last_wait_value: self.last_wait_value.clone(),
        }
    }

    /// Restore a runtime from a previously captured snapshot.
    pub fn from_snapshot(host: H, program: ProgramIr, snapshot: RuntimeSnapshot) -> Self {
        Self {
            host,
            program,
            state_index: snapshot.state_index,
            command_index: snapshot.command_index,
            waiting: snapshot.waiting,
            completed: snapshot.completed,
            last_wait_value: snapshot.last_wait_value,
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

    /// Whether the runtime still needs to execute commands at index zero.
    pub fn entry_pending(&self) -> bool {
        !self.completed && self.command_index == 0 && self.waiting.is_none()
    }

    /// Current wait condition, if the runtime is paused.
    pub fn waiting_condition(&self) -> Option<&WaitCondition> {
        self.waiting.as_ref()
    }

    /// Depth of the interpreter frame stack (always zero in the minimal kernel).
    pub fn frame_depth(&self) -> usize {
        0
    }

    /// Value captured from the most recent wait.
    pub fn last_wait_value(&self) -> Option<&Value> {
        self.last_wait_value.as_ref()
    }
}
