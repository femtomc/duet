use serde::{Deserialize, Serialize};

/// Generic S-expression nodes used throughout the workflow language.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", content = "value")]
pub enum Expr {
    /// A bare symbol.
    Symbol(String),
    /// Keyword tokens (leading colon).
    Keyword(String),
    /// String literal.
    String(String),
    /// Signed integer literal.
    Integer(i64),
    /// Floating-point literal.
    Float(f64),
    /// Boolean literal.
    Boolean(bool),
    /// Nested list.
    List(Vec<Expr>),
}

/// Program container. Future passes will translate `forms` into strongly typed
/// structures (states, actions, waits, etc.).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Program {
    /// Program identifier, usually derived from the top-level form name.
    pub name: String,
    /// Parsed forms (the raw S-expressions).
    pub forms: Vec<Expr>,
    /// Original source text, retained for error reporting and debugging.
    pub source: String,
}

impl Program {
    /// Construct a new program stub with the provided name/source.
    pub fn new(name: impl Into<String>, source: impl Into<String>, forms: Vec<Expr>) -> Self {
        Self {
            name: name.into(),
            source: source.into(),
            forms,
        }
    }
}
