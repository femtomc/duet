use preserves::IOValue;
use serde::{Deserialize, Serialize};
use std::convert::TryFrom;

use super::{WorkflowError, ast::Expr};

/// Structured interpreter value that can be converted into a preserves [`IOValue`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Value {
    /// Symbol literal (treated as a preserves symbol).
    Symbol(String),
    /// Keyword literal (preserved as a symbol with a leading colon).
    Keyword(String),
    /// UTF-8 string literal.
    String(String),
    /// Signed integer literal.
    Integer(i64),
    /// Floating-point literal.
    Float(f64),
    /// Boolean literal.
    Boolean(bool),
    /// Heterogeneous list/sequence.
    List(Vec<Value>),
    /// Record value with a symbolic label and positional fields.
    Record {
        /// Record label encoded as a preserves symbol.
        label: String,
        /// Positional field values.
        fields: Vec<Value>,
    },
    /// Placeholder that resolves a role property at runtime for literal contexts.
    RoleProperty {
        /// Role whose property should be fetched.
        role: Box<Value>,
        /// Property key to read from the role binding.
        key: String,
    },
}

impl Value {
    /// Convert the interpreter value into a preserves [`IOValue`].
    pub fn to_io_value(&self) -> IOValue {
        match self {
            Value::Symbol(sym) => IOValue::symbol(sym.clone()),
            Value::Keyword(kw) => IOValue::symbol(format!(":{}", kw)),
            Value::String(text) => IOValue::new(text.clone()),
            Value::Integer(num) => IOValue::new(*num),
            Value::Float(num) => IOValue::new(*num),
            Value::Boolean(flag) => IOValue::new(*flag),
            Value::List(items) => {
                let converted: Vec<IOValue> = items.iter().map(|item| item.to_io_value()).collect();
                IOValue::new(converted)
            }
            Value::Record { label, fields } => {
                let field_values: Vec<IOValue> =
                    fields.iter().map(|field| field.to_io_value()).collect();
                IOValue::record(IOValue::symbol(label.clone()), field_values)
            }
            Value::RoleProperty { .. } => {
                panic!("role-property must be resolved before conversion to IOValue")
            }
        }
    }

    /// Attempt to reconstruct a [`Value`] from a preserves [`IOValue`].
    pub fn from_io_value(value: &IOValue) -> Option<Value> {
        if let Some(sym) = value.as_symbol() {
            let text = sym.as_ref();
            if text.starts_with(':') {
                return Some(Value::Keyword(text[1..].to_string()));
            }
            return Some(Value::Symbol(text.to_string()));
        }

        if let Some(text) = value.as_string() {
            return Some(Value::String(text.to_string()));
        }

        if let Some(flag) = value.as_boolean() {
            return Some(Value::Boolean(flag));
        }

        if let Some(int) = value.as_signed_integer() {
            if let Ok(num) = i64::try_from(int.as_ref()) {
                return Some(Value::Integer(num));
            }
        }

        if let Some(float) = value.as_double() {
            return Some(Value::Float(float));
        }

        if value.is_sequence() {
            let mut items = Vec::new();
            for item in value.iter() {
                items.push(Value::from_io_value(&IOValue::from(item))?);
            }
            return Some(Value::List(items));
        }

        if value.is_record() {
            let label = value.label().as_symbol().map(|sym| sym.to_string())?;
            let mut fields = Vec::new();
            for idx in 0..value.len() {
                let field = Value::from_io_value(&IOValue::from(value.index(idx)))?;
                fields.push(field);
            }
            return Some(Value::Record { label, fields });
        }

        None
    }

    /// Convenience accessor for string references.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(text) => Some(text),
            Value::Symbol(sym) => Some(sym),
            _ => None,
        }
    }
}

/// Evaluation context exposed to value expressions.
pub trait ValueContext {
    /// Retrieve the current string property for the given role/key pair.
    fn role_property(&self, role: &str, key: &str) -> Option<String>;
}

/// Parse a literal [`Expr`] into a structured [`Value`].
pub fn parse_value_literal(expr: &Expr) -> Result<Value, WorkflowError> {
    match expr {
        Expr::Symbol(sym) => Ok(Value::Symbol(sym.clone())),
        Expr::Keyword(kw) => Ok(Value::Keyword(kw.clone())),
        Expr::String(text) => Ok(Value::String(text.clone())),
        Expr::Integer(num) => Ok(Value::Integer(*num)),
        Expr::Float(num) => Ok(Value::Float(*num)),
        Expr::Boolean(flag) => Ok(Value::Boolean(*flag)),
        Expr::List(items) => {
            if let Some(Expr::Symbol(head)) = items.first() {
                if head == "record" {
                    return parse_record_literal(items);
                } else if head == "role-property" {
                    if items.len() != 3 {
                        return Err(validation_error(
                            "role-property expects role symbol and property string",
                        ));
                    }
                    let role = parse_value_literal(&items[1])?;
                    let key = match &items[2] {
                        Expr::String(text) => text.clone(),
                        Expr::Symbol(sym) => sym.clone(),
                        other => {
                            return Err(validation_error(&format!(
                                "role-property name must be string or symbol, found {:?}",
                                other
                            )));
                        }
                    };
                    return Ok(Value::RoleProperty {
                        role: Box::new(role),
                        key,
                    });
                }
            }
            let values = items
                .iter()
                .map(parse_value_literal)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(Value::List(values))
        }
    }
}

fn parse_record_literal(items: &[Expr]) -> Result<Value, WorkflowError> {
    if items.len() < 2 {
        return Err(validation_error("record requires a label"));
    }

    let label = match &items[1] {
        Expr::Symbol(sym) => sym.clone(),
        other => {
            return Err(validation_error(&format!(
                "record label must be a symbol, found {:?}",
                other
            )));
        }
    };

    let mut fields = Vec::new();
    for expr in &items[2..] {
        fields.push(parse_value_literal(expr)?);
    }

    Ok(Value::Record { label, fields })
}

/// Backwards-compatible helper retained while the builder migrates to
/// `parse_value_literal`.
pub fn parse_value(expr: &Expr) -> Result<Value, WorkflowError> {
    parse_value_literal(expr)
}

fn validation_error(message: &str) -> WorkflowError {
    WorkflowError::Validation(message.to_string())
}
