//! Convenience helpers for working with `preserves::IOValue` records.

use preserves::IOValue;

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
