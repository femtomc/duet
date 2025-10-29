//! Interpreter scaffolding for the Duet language runtime.
//!
//! Programs expressed in our DSL are translated into Syndicated Actor VM
//! actions—spawning facets, sending messages, awaiting assertions, and so on.
//! This module provides the AST, parser, and runtime hooks that let higher-level
//! tools (like `codebased`) interpret those programs without hand-writing actor
//! logic.

/// Abstract syntax tree definitions for the interpreter language.
pub mod ast;
/// Runtime driver that will execute programs against the actor VM.
pub mod runtime;
/// Parser for the interpreter DSL.
pub mod parser;
/// Typed intermediate representation structures.
pub mod ir;
/// Builders that translate parsed programs into the IR.
pub mod builder;

pub use ast::{Expr, Program};
pub use builder::build_ir;
pub use ir::{Action, BranchArm, Condition, Instruction, ProgramIr, RoleBinding, State, WaitCondition};
pub use parser::parse_program;
pub use runtime::{InterpreterHost, InterpreterRuntime, RuntimeError, RuntimeEvent};

use thiserror::Error;

/// Convenience result alias for interpreter operations.
pub type Result<T> = std::result::Result<T, WorkflowError>;

/// Errors surfaced by the parser/interpreter.
#[derive(Debug, Error)]
pub enum WorkflowError {
    /// Parsing failed due to invalid syntax.
    #[error("invalid interpreter syntax: {0}")]
    Syntax(String),

    /// Semantic validation failed (unknown symbol, missing state, etc.).
    #[error("interpreter validation failed: {0}")]
    Validation(String),

    /// Placeholder error while the interpreter is still being implemented.
    #[error("interpreter runtime is not yet implemented")]
    Unimplemented,
}
