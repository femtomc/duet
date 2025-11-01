use preserves::IOValue;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::convert::TryFrom;

use super::{WorkflowError, ast::Expr};

/// Structured interpreter value that can be converted into a preserves
/// [`IOValue`]. This lets programs construct dataspace records without falling
/// back to manual string rendering.
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

    /// Convenience accessor for string references (used by action templates).
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(text) => Some(text),
            Value::Symbol(sym) => Some(sym),
            _ => None,
        }
    }
}

/// Expression-level value supporting parameter references.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum ValueExpr {
    /// Literal value with no parameter capture.
    Literal(Value),
    /// Reference to a function parameter.
    Parameter(String),
    /// Sequence whose elements may reference parameters.
    List(Vec<ValueExpr>),
    /// Record whose fields may reference parameters.
    Record {
        /// Record label encoded as a preserves symbol.
        label: String,
        /// Field expressions evaluated at runtime.
        fields: Vec<ValueExpr>,
    },
    /// Property value currently bound to a role.
    RoleProperty {
        /// Expression producing the role name.
        role: Box<ValueExpr>,
        /// Property key to read from the role.
        key: String,
    },
    /// Entire value associated with the most recently satisfied wait.
    LastWait,
    /// Field extracted from the most recent wait result.
    LastWaitField {
        /// Field index to extract from the prior wait result.
        index: usize,
    },
    /// Concatenate string fragments resolved from expressions.
    StringConcat(Vec<ValueExpr>),
}

/// Evaluation context exposed to value expressions.
pub trait ValueContext {
    /// Retrieve the current string property for the given role/key pair.
    fn role_property(&self, role: &str, key: &str) -> Option<String>;
}

impl ValueExpr {
    /// Resolve the expression using the supplied bindings, producing a concrete
    /// [`Value`]. Bindings map parameter names to provided argument values.
    pub fn resolve<C: ValueContext>(
        &self,
        bindings: &HashMap<String, Value>,
        last_wait: Option<&Value>,
        context: &C,
    ) -> Result<Value, WorkflowError> {
        match self {
            ValueExpr::Literal(value) => Ok(value.clone()),
            ValueExpr::Parameter(param) => bindings
                .get(param)
                .cloned()
                .ok_or_else(|| validation_error(&format!("unknown parameter: {}", param))),
            ValueExpr::List(items) => {
                let mut resolved = Vec::new();
                for item in items {
                    resolved.push(item.resolve(bindings, last_wait, context)?);
                }
                Ok(Value::List(resolved))
            }
            ValueExpr::Record { label, fields } => {
                let mut resolved_fields = Vec::new();
                for field in fields {
                    resolved_fields.push(field.resolve(bindings, last_wait, context)?);
                }
                Ok(Value::Record {
                    label: label.clone(),
                    fields: resolved_fields,
                })
            }
            ValueExpr::RoleProperty { role, key } => {
                let role_value = role.resolve(bindings, last_wait, context)?;
                let role_name = match role_value {
                    Value::String(ref text) => text.clone(),
                    Value::Symbol(ref sym) => sym.clone(),
                    other => {
                        return Err(validation_error(&format!(
                            "role-property expected role name as string, found {:?}",
                            other
                        )));
                    }
                };
                let value = context.role_property(&role_name, key).ok_or_else(|| {
                    validation_error(&format!(
                        "role '{}' does not define property '{}'",
                        role_name, key
                    ))
                })?;
                Ok(Value::String(value))
            }
            ValueExpr::LastWait => last_wait
                .cloned()
                .ok_or_else(|| validation_error("last wait value is not available")),
            ValueExpr::LastWaitField { index } => {
                let wait_value = last_wait
                    .ok_or_else(|| validation_error("last wait value is not available"))?;
                match wait_value {
                    Value::Record { fields, .. } => fields.get(*index).cloned().ok_or_else(|| {
                        validation_error(&format!("last wait does not contain field {}", index))
                    }),
                    Value::List(items) => items.get(*index).cloned().ok_or_else(|| {
                        validation_error(&format!("last wait does not contain field {}", index))
                    }),
                    _ => Err(validation_error(
                        "last wait value does not expose record fields",
                    )),
                }
            }
            ValueExpr::StringConcat(parts) => {
                let mut buffer = String::new();
                for part in parts {
                    let value = part.resolve(bindings, last_wait, context)?;
                    buffer.push_str(&value_to_string(&value));
                }
                Ok(Value::String(buffer))
            }
        }
    }
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

/// Parse an [`Expr`] into a [`ValueExpr`], treating the provided symbols as
/// parameter references.
pub fn parse_value_expr(expr: &Expr, params: &HashSet<String>) -> Result<ValueExpr, WorkflowError> {
    match expr {
        Expr::Symbol(sym) => {
            if params.contains(sym) {
                Ok(ValueExpr::Parameter(sym.clone()))
            } else {
                Ok(ValueExpr::Literal(Value::Symbol(sym.clone())))
            }
        }
        Expr::Keyword(kw) => Ok(ValueExpr::Literal(Value::Keyword(kw.clone()))),
        Expr::String(text) => Ok(ValueExpr::Literal(Value::String(text.clone()))),
        Expr::Integer(num) => Ok(ValueExpr::Literal(Value::Integer(*num))),
        Expr::Float(num) => Ok(ValueExpr::Literal(Value::Float(*num))),
        Expr::Boolean(flag) => Ok(ValueExpr::Literal(Value::Boolean(*flag))),
        Expr::List(items) => {
            if let Some(Expr::Symbol(head)) = items.first() {
                if head == "record" {
                    return parse_record_expr(items, params);
                } else if head == "role-property" {
                    if items.len() != 3 {
                        return Err(validation_error(
                            "role-property expects role symbol and property string",
                        ));
                    }
                    let role = Box::new(parse_value_expr(&items[1], params)?);
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
                    return Ok(ValueExpr::RoleProperty { role, key });
                } else if head == "last-wait" {
                    if items.len() != 1 {
                        return Err(validation_error("last-wait takes no arguments"));
                    }
                    return Ok(ValueExpr::LastWait);
                } else if head == "last-wait-field" {
                    if items.len() != 2 {
                        return Err(validation_error(
                            "last-wait-field expects a single numeric index",
                        ));
                    }
                    let index = match &items[1] {
                        Expr::Integer(num) => *num,
                        other => {
                            return Err(validation_error(&format!(
                                "last-wait-field index must be integer, found {:?}",
                                other
                            )));
                        }
                    };
                    if index < 0 {
                        return Err(validation_error("field index must be non-negative"));
                    }
                    return Ok(ValueExpr::LastWaitField {
                        index: index as usize,
                    });
                } else if head == "string-append" {
                    let mut parts = Vec::new();
                    for expr in &items[1..] {
                        parts.push(parse_value_expr(expr, params)?);
                    }
                    return Ok(ValueExpr::StringConcat(parts));
                }
            }
            let mut values = Vec::new();
            for item in items {
                values.push(parse_value_expr(item, params)?);
            }
            Ok(ValueExpr::List(values))
        }
    }
}

fn parse_record_expr(items: &[Expr], params: &HashSet<String>) -> Result<ValueExpr, WorkflowError> {
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
        fields.push(parse_value_expr(expr, params)?);
    }

    Ok(ValueExpr::Record { label, fields })
}

fn value_to_string(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Symbol(sym) => sym.clone(),
        Value::Keyword(kw) => format!(":{}", kw),
        Value::Integer(num) => num.to_string(),
        Value::Float(num) => num.to_string(),
        Value::Boolean(flag) => flag.to_string(),
        Value::List(items) => {
            let rendered: Vec<String> = items.iter().map(value_to_string).collect();
            format!("[{}]", rendered.join(", "))
        }
        Value::Record { label, fields } => {
            let rendered: Vec<String> = fields.iter().map(value_to_string).collect();
            format!("<{} {}>", label, rendered.join(" "))
        }
        Value::RoleProperty { role, key } => {
            format!("<role-property {} {}>", value_to_string(role), key)
        }
    }
}

fn validation_error(message: &str) -> WorkflowError {
    WorkflowError::Validation(message.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::iter::FromIterator;

    struct EmptyContext;

    impl ValueContext for EmptyContext {
        fn role_property(&self, _role: &str, _key: &str) -> Option<String> {
            None
        }
    }

    #[test]
    fn parse_and_convert_record() {
        let expr = Expr::List(vec![
            Expr::Symbol("record".to_string()),
            Expr::Symbol("agent-request".to_string()),
            Expr::String("tag-123".to_string()),
            Expr::String("payload".to_string()),
        ]);

        let value = parse_value_literal(&expr).expect("record value");
        if let Value::Record { label, fields } = &value {
            assert_eq!(label, "agent-request");
            assert_eq!(fields.len(), 2);
        } else {
            panic!("expected record value, got {:?}", value);
        }

        let as_io = value.to_io_value();
        let label = as_io
            .label()
            .as_symbol()
            .map(|sym| sym.to_string())
            .unwrap();
        assert_eq!(label, "agent-request");
        assert_eq!(as_io.len(), 2);
        assert_eq!(
            as_io.index(0).as_string().map(|s| s.to_string()),
            Some("tag-123".to_string())
        );
        assert_eq!(
            as_io.index(1).as_string().map(|s| s.to_string()),
            Some("payload".to_string())
        );
    }

    #[test]
    fn resolves_parameterised_record() {
        let expr = Expr::List(vec![
            Expr::Symbol("record".to_string()),
            Expr::Symbol("greeting".to_string()),
            Expr::Symbol("person".to_string()),
        ]);
        let params = HashSet::from_iter(vec!["person".to_string()]);
        let value_expr = parse_value_expr(&expr, &params).expect("value expr");
        let mut bindings = HashMap::new();
        bindings.insert("person".to_string(), Value::String("Ada".into()));

        let resolved = value_expr
            .resolve(&bindings, None, &EmptyContext)
            .expect("resolved");
        match resolved {
            Value::Record { label, fields } => {
                assert_eq!(label, "greeting");
                assert_eq!(fields.len(), 1);
                assert_eq!(fields[0].as_str(), Some("Ada"));
            }
            other => panic!("unexpected value {:?}", other),
        }
    }
}
