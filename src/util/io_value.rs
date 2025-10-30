//! Convenience helpers for working with `preserves::IOValue` records.

use preserves::IOValue;
use preserves::types::{AtomClass, CompoundClass, ValueClass};
use serde_json::{Value, json};
use std::borrow::Cow;
use std::convert::TryFrom;

/// Lightweight view over a preserves record.
pub struct RecordView<'a> {
    value: &'a IOValue,
}

impl<'a> RecordView<'a> {
    /// Return the number of fields in the record.
    pub fn len(&self) -> usize {
        self.value.len()
    }

    /// Check whether the record label matches an expected symbol.
    pub fn has_label(&self, expected: &str) -> bool {
        self.value
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref() == expected)
            == Some(true)
    }

    /// Access a field by index.
    pub fn field(&self, index: usize) -> IOValue {
        IOValue::from(self.value.index(index))
    }

    /// Interpret the field at `index` as a UTF-8 string.
    pub fn field_string(&self, index: usize) -> Option<String> {
        self.field(index).as_string().map(|s| s.to_string())
    }

    /// Interpret the field at `index` as a symbol string.
    pub fn field_symbol(&self, index: usize) -> Option<String> {
        self.field(index)
            .as_symbol()
            .map(|s| s.as_ref().to_string())
    }

    /// Interpret the field at `index` as an RFC3339 timestamp.
    pub fn field_timestamp(&self, index: usize) -> Option<chrono::DateTime<chrono::Utc>> {
        let field = self.field(index);
        let text = field.as_string()?;
        chrono::DateTime::parse_from_rfc3339(text.as_ref())
            .ok()
            .map(|dt| dt.with_timezone(&chrono::Utc))
    }
}

/// Attempt to treat an [`IOValue`] as a record.
pub fn as_record(value: &IOValue) -> Option<RecordView<'_>> {
    if value.is_record() {
        Some(RecordView { value })
    } else {
        None
    }
}

/// Return the record view if the label matches the expected symbol.
pub fn record_with_label<'a>(value: &'a IOValue, expected: &str) -> Option<RecordView<'a>> {
    let view = as_record(value)?;
    if view.has_label(expected) {
        Some(view)
    } else {
        None
    }
}

/// Convert an `IOValue` into a JSON structure that highlights its semantic shape.
pub fn io_value_to_json(value: &IOValue) -> Value {
    let mut node = match value.value_class() {
        ValueClass::Atomic(atom) => match atom {
            AtomClass::Boolean => json!({
                "type": "boolean",
                "value": value.as_boolean().unwrap_or(false),
            }),
            AtomClass::Double => json!({
                "type": "float",
                "value": value.as_double().unwrap_or(0.0),
            }),
            AtomClass::SignedInteger => {
                let signed = value
                    .as_signed_integer()
                    .map(Cow::into_owned)
                    .expect("signed integer available");
                let as_string = signed.to_string();
                let maybe_i128 = i128::try_from(&signed).ok();
                let maybe_u128 = u128::try_from(&signed).ok();
                let mut payload = json!({
                    "type": "integer",
                    "display": as_string,
                });
                if let Some(i) = maybe_i128 {
                    payload
                        .as_object_mut()
                        .unwrap()
                        .insert("value".to_string(), json!(i));
                } else if let Some(u) = maybe_u128 {
                    payload
                        .as_object_mut()
                        .unwrap()
                        .insert("value".to_string(), json!(u));
                }
                payload
            }
            AtomClass::String => json!({
                "type": "string",
                "value": value
                    .as_string()
                    .map(|s| s.to_string())
                    .unwrap_or_default(),
            }),
            AtomClass::ByteString => {
                let bytes = value
                    .as_bytestring()
                    .map(Cow::into_owned)
                    .unwrap_or_default();
                let hex: String = bytes.iter().map(|byte| format!("{:02x}", byte)).collect();
                json!({
                    "type": "bytes",
                    "hex": hex,
                    "length": bytes.len(),
                })
            }
            AtomClass::Symbol => json!({
                "type": "symbol",
                "value": value
                    .as_symbol()
                    .map(|s| s.as_ref().to_string())
                    .unwrap_or_default(),
            }),
        },
        ValueClass::Compound(kind) => match kind {
            CompoundClass::Record => {
                let label = value
                    .label()
                    .as_symbol()
                    .map(|sym| sym.as_ref().to_string());
                let fields: Vec<Value> = value
                    .iter()
                    .map(|field| {
                        let nested = IOValue::from(field);
                        io_value_to_json(&nested)
                    })
                    .collect();
                let mut payload = json!({
                    "type": "record",
                    "fields": fields,
                    "field_count": value.len(),
                });
                if let Some(label) = label {
                    payload
                        .as_object_mut()
                        .unwrap()
                        .insert("label".to_string(), Value::String(label));
                }
                if let Some(annotations) = value.annotations() {
                    if !annotations.is_empty() {
                        let rendered: Vec<Value> = annotations
                            .iter()
                            .map(|annotation| io_value_to_json(annotation))
                            .collect();
                        payload
                            .as_object_mut()
                            .unwrap()
                            .insert("annotations".to_string(), Value::Array(rendered));
                    }
                }
                payload
            }
            CompoundClass::Sequence => {
                let items: Vec<Value> = value
                    .iter()
                    .map(|item| io_value_to_json(&IOValue::from(item)))
                    .collect();
                json!({
                    "type": "sequence",
                    "items": items,
                    "length": items.len(),
                })
            }
            CompoundClass::Set => {
                let items: Vec<Value> = value
                    .iter()
                    .map(|item| io_value_to_json(&IOValue::from(item)))
                    .collect();
                json!({
                    "type": "set",
                    "items": items,
                    "length": items.len(),
                })
            }
            CompoundClass::Dictionary => {
                let entries: Vec<Value> = value
                    .entries()
                    .map(|(key, entry_value)| {
                        json!({
                            "key": io_value_to_json(&IOValue::from(key)),
                            "value": io_value_to_json(&IOValue::from(entry_value)),
                        })
                    })
                    .collect();
                json!({
                    "type": "dictionary",
                    "entries": entries,
                    "length": entries.len(),
                })
            }
        },
        ValueClass::Embedded => value
            .as_embedded()
            .map(|inner| {
                json!({
                    "type": "embedded",
                    "value": io_value_to_json(&inner),
                })
            })
            .unwrap_or_else(|| json!({ "type": "embedded" })),
    };

    if let Some(map) = node.as_object_mut() {
        map.insert(
            "summary".to_string(),
            Value::String(io_value_summary(value, 80)),
        );
    }

    node
}

/// Produce a concise textual summary for an `IOValue`.
pub fn io_value_summary(value: &IOValue, limit: usize) -> String {
    if let Some(string) = value.as_string() {
        return truncate(&string, limit);
    }
    if let Some(sym) = value.as_symbol() {
        return format!(":{sym}");
    }
    if let Some(boolean) = value.as_boolean() {
        return boolean.to_string();
    }
    if let Some(integer) = value.as_signed_integer() {
        return truncate(&integer.to_string(), limit);
    }
    if let Some(float) = value.as_double() {
        return format!("{float}");
    }
    if value.is_record() {
        let label = value
            .label()
            .as_symbol()
            .map(|sym| sym.as_ref().to_string())
            .unwrap_or_else(|| "record".to_string());
        return format!("{label}#{field_count}", field_count = value.len());
    }
    if value.is_sequence() {
        return format!("sequence[{len}]", len = value.len());
    }
    if value.is_set() {
        return format!("set[{len}]", len = value.len());
    }
    if value.is_dictionary() {
        return format!("dict[{len}]", len = value.len());
    }
    truncate(&format!("{value:?}"), limit)
}

fn truncate(input: &str, limit: usize) -> String {
    if input.len() <= limit {
        input.to_string()
    } else if limit <= 1 {
        "…".to_string()
    } else {
        let mut result = input.chars().take(limit - 1).collect::<String>();
        result.push('…');
        result
    }
}
