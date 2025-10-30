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
        label: String,
        fields: Vec<ValueExpr>,
    },
}

impl ValueExpr {
    /// Resolve the expression using the supplied bindings, producing a concrete
    /// [`Value`]. Bindings map parameter names to provided argument values.
    pub fn resolve(&self, bindings: &HashMap<String, Value>) -> Result<Value, WorkflowError> {
        match self {
            ValueExpr::Literal(value) => Ok(value.clone()),
            ValueExpr::Parameter(param) => bindings
                .get(param)
                .cloned()
                .ok_or_else(|| validation_error(&format!("unknown parameter: {}", param))),
            ValueExpr::List(items) => {
                let mut resolved = Vec::new();
                for item in items {
                    resolved.push(item.resolve(bindings)?);
                }
                Ok(Value::List(resolved))
            }
            ValueExpr::Record { label, fields } => {
                let mut resolved_fields = Vec::new();
                for field in fields {
                    resolved_fields.push(field.resolve(bindings)?);
                }
                Ok(Value::Record {
                    label: label.clone(),
                    fields: resolved_fields,
                })
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

fn validation_error(message: &str) -> WorkflowError {
    WorkflowError::Validation(message.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::iter::FromIterator;

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

        let resolved = value_expr.resolve(&bindings).expect("resolved");
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
