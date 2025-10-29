use super::{Program, Result, WorkflowError};

/// Minimal runtime wrapper for workflow programs.
///
/// Eventually this struct will execute compiled programs on top of the actor
/// runtime. For now it simply holds a reference to the parsed program and
/// exposes a `tick` method that returns `Unimplemented`.
pub struct WorkflowRuntime {
    program: Program,
}

impl WorkflowRuntime {
    /// Create a new runtime driver for the given program.
    pub fn new(program: Program) -> Self {
        Self { program }
    }

    /// Execute the next step of the workflow. This is currently a stub.
    pub fn tick(&mut self) -> Result<()> {
        Err(WorkflowError::Unimplemented)
    }

    /// Access the underlying program (useful for debugging / observers).
    pub fn program(&self) -> &Program {
        &self.program
    }
}
