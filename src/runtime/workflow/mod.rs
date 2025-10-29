//! Workflow language scaffolding.
//!
//! This module will eventually host the compiler and interpreter for Duet's
//! workflow DSL. For now we define the core data structures and error types so
//! other subsystems can start depending on the API surface.

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Convenience result alias for workflow operations.
pub type Result<T> = std::result::Result<T, WorkflowError>;

/// Errors surfaced by the workflow compiler/interpreter.
#[derive(Debug, Error)]
pub enum WorkflowError {
    /// Placeholder error used while the compiler is under construction.
    #[error("workflow support is not implemented yet")]
    Unimplemented,

    /// Raised when parsing encounters an invalid form.
    #[error("invalid workflow syntax: {0}")]
    Syntax(String),

    /// Raised when semantic validation fails (unknown role, missing state, etc.).
    #[error("workflow validation failed: {0}")]
    Validation(String),
}

/// Parsed representation of a workflow program.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub struct WorkflowProgram {
    /// Program identifier (usually derived from the `(workflow <name> …)` form).
    pub name: String,
    /// Raw source text (useful for logging / future recompilation).
    pub source: String,
    /// Placeholder for the future AST; currently unused.
    #[serde(skip)]
    pub ast: Option<WorkflowAst>,
}

/// Placeholder AST node until the real parser is implemented.
#[derive(Debug, Clone)]
pub struct WorkflowAst;

impl WorkflowProgram {
    /// Create a new program record from raw source.
    pub fn from_source(name: impl Into<String>, source: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            source: source.into(),
            ast: None,
        }
    }
}

/// Skeleton interpreter that will eventually execute workflow programs.
pub struct WorkflowInterpreter {
    program: WorkflowProgram,
}

impl WorkflowInterpreter {
    /// Instantiate an interpreter for a parsed program.
    pub fn new(program: WorkflowProgram) -> Self {
        Self { program }
    }

    /// Evaluate the next step of the workflow.
    ///
    /// This is currently a stub returning `WorkflowError::Unimplemented`.
    pub fn tick(&mut self) -> Result<()> {
        Err(WorkflowError::Unimplemented)
    }

    /// Access the underlying program (useful for debugging/UI).
    pub fn program(&self) -> &WorkflowProgram {
        &self.program
    }
}

/// Parse workflow source text into a [`WorkflowProgram`].
pub fn parse_workflow(source: &str) -> Result<WorkflowProgram> {
    // This will eventually convert the Lisp-like language into an AST.
    // For now we simply return an error so callers know the feature is pending.
    let program = WorkflowProgram::from_source("unnamed-workflow", source);
    let _ = program;
    Err(WorkflowError::Unimplemented)
}
