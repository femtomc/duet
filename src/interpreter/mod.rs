//! Workflow interpreter scaffolding.
//!
//! This module houses the workflow DSL (AST/parser) and the runtime that
//! executes workflow programs on top of the Syndicated Actor VM. The language
//! is still under construction; current functionality is limited to stubs so
//! other components can depend on the public API.

/// Abstract syntax tree definitions for the workflow DSL.
pub mod ast;
/// Runtime driver that will execute workflow programs.
pub mod runtime;

pub use ast::{Expr, Program};
pub use runtime::WorkflowRuntime;

use thiserror::Error;

/// Convenience result alias for interpreter operations.
pub type Result<T> = std::result::Result<T, WorkflowError>;

/// Errors surfaced by the workflow parser/interpreter.
#[derive(Debug, Error)]
pub enum WorkflowError {
    /// Parsing failed due to invalid syntax.
    #[error("invalid workflow syntax: {0}")]
    Syntax(String),

    /// Semantic validation failed (unknown symbol, missing state, etc.).
    #[error("workflow validation failed: {0}")]
    Validation(String),

    /// Placeholder error while the interpreter is still being implemented.
    #[error("workflow interpreter is not yet implemented")]
    Unimplemented,
}
