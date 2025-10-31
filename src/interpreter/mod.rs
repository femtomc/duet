//! Interpreter scaffolding for the Duet language runtime.
//!
//! Programs expressed in our DSL are translated into Syndicated Actor VM
//! actions—spawning facets, sending messages, awaiting assertions, and so on.
//! This module provides the AST, parser, and runtime hooks that let higher-level
//! tools (like `codebased`) interpret those programs without hand-writing actor
//! logic.

/// Abstract syntax tree definitions for the interpreter language.
pub mod ast;
/// Builders that translate parsed programs into the IR.
pub mod builder;
/// Interpreter entity implementation.
pub mod entity;
/// Typed intermediate representation structures.
pub mod ir;
/// Parser for the interpreter DSL.
pub mod parser;
/// Dataspace protocol structures for interpreter definitions/instances.
pub mod protocol;
/// Runtime driver that will execute programs against the actor VM.
pub mod runtime;
/// Structured value handling for interpreter programs.
pub mod value;

pub use ast::{Expr, Program};
pub use builder::build_ir;
pub use entity::InterpreterEntity;
pub use ir::{
    Action, BranchArm, Condition, Instruction, ProgramIr, RoleBinding, State, WaitCondition,
};
pub use parser::parse_program;
pub use protocol::{
    DEFINE_MESSAGE_LABEL, DEFINITION_RECORD_LABEL, DefinitionRecord, INPUT_REQUEST_RECORD_LABEL,
    INPUT_RESPONSE_RECORD_LABEL, INSTANCE_RECORD_LABEL, InputRequestRecord, InputResponseRecord,
    InstanceProgress, InstanceRecord, InstanceStatus, LOG_RECORD_LABEL, NOTIFY_MESSAGE_LABEL,
    ProgramRef, RESUME_MESSAGE_LABEL, RUN_MESSAGE_LABEL, WaitStatus, input_request_from_value,
    input_request_to_value, input_response_from_value, input_response_to_value,
};
pub use runtime::{
    FrameKindSnapshot, FrameSnapshot, InterpreterHost, InterpreterRuntime, RuntimeError,
    RuntimeEvent, RuntimeSnapshot,
};
pub use value::{Value, ValueExpr, parse_value, parse_value_expr, parse_value_literal};

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
