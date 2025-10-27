//! Pattern matching and subscription engine
//!
//! Compiles and evaluates dataspace patterns, maintains subscription tables,
//! and emits match/mismatch events.

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

use super::turn::{FacetId, Handle};

/// Pattern identifier
pub type PatternId = Uuid;

/// A dataspace pattern for matching assertions
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Pattern {
    /// Pattern ID
    pub id: PatternId,

    /// Pattern expression
    #[serde(with = "super::registry::preserves_text_serde")]
    pub pattern: preserves::IOValue,

    /// Facet that registered this pattern
    pub facet: FacetId,
}

/// A match event
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PatternMatch {
    /// Pattern that matched
    pub pattern_id: PatternId,

    /// Handle that matched
    pub handle: Handle,

    /// Matched value
    pub value: preserves::IOValue,
}

/// Pattern matcher and subscription manager
pub struct PatternEngine {
    /// Registered patterns by ID
    pub(crate) patterns: HashMap<PatternId, Pattern>,

    /// Current matches by pattern ID and handle
    matches: HashMap<PatternId, HashMap<Handle, PatternMatch>>,

    /// Index of handles to pattern IDs that matched them
    handle_to_patterns: HashMap<Handle, HashSet<PatternId>>,
}

impl PatternEngine {
    /// Create a new pattern engine
    pub fn new() -> Self {
        Self {
            patterns: HashMap::new(),
            matches: HashMap::new(),
            handle_to_patterns: HashMap::new(),
        }
    }

    /// Register a pattern subscription
    pub fn register(&mut self, pattern: Pattern) -> PatternId {
        let id = pattern.id;
        self.patterns.insert(id, pattern);
        self.matches.insert(id, HashMap::new());
        id
    }

    /// Unregister a pattern subscription
    pub fn unregister(&mut self, id: PatternId) {
        // Remove pattern
        self.patterns.remove(&id);

        // Remove all matches for this pattern
        if let Some(pattern_matches) = self.matches.remove(&id) {
            // Remove from handle_to_patterns index
            for handle in pattern_matches.keys() {
                if let Some(pattern_set) = self.handle_to_patterns.get_mut(handle) {
                    pattern_set.remove(&id);
                    if pattern_set.is_empty() {
                        self.handle_to_patterns.remove(handle);
                    }
                }
            }
        }
    }

    /// Evaluate all patterns against a new assertion
    pub fn eval_assert(
        &mut self,
        handle: &Handle,
        value: &preserves::IOValue,
    ) -> Vec<PatternMatch> {
        let mut new_matches = Vec::new();

        // Test all registered patterns against this assertion
        for (pattern_id, pattern) in &self.patterns {
            if matches_pattern(&pattern.pattern, value) {
                let pattern_match = PatternMatch {
                    pattern_id: *pattern_id,
                    handle: handle.clone(),
                    value: value.clone(),
                };

                // Store the match
                self.matches
                    .entry(*pattern_id)
                    .or_insert_with(HashMap::new)
                    .insert(handle.clone(), pattern_match.clone());

                // Update handle_to_patterns index
                self.handle_to_patterns
                    .entry(handle.clone())
                    .or_insert_with(HashSet::new)
                    .insert(*pattern_id);

                new_matches.push(pattern_match);
            }
        }

        new_matches
    }

    /// Handle a retraction
    pub fn eval_retract(&mut self, handle: &Handle) -> Vec<PatternId> {
        let mut affected_patterns = Vec::new();

        // Find all patterns that had this handle as a match
        if let Some(pattern_ids) = self.handle_to_patterns.remove(handle) {
            for pattern_id in pattern_ids {
                // Remove the match from the pattern's match set
                if let Some(pattern_matches) = self.matches.get_mut(&pattern_id) {
                    pattern_matches.remove(handle);
                }
                affected_patterns.push(pattern_id);
            }
        }

        affected_patterns
    }

    /// Get current matches for a pattern
    pub fn get_matches(&self, pattern_id: &PatternId) -> Vec<PatternMatch> {
        self.matches
            .get(pattern_id)
            .map(|m| m.values().cloned().collect())
            .unwrap_or_default()
    }
}

/// Check if a value matches a pattern
///
/// Pattern matching rules:
/// - Wildcard symbols (starting with `<` and ending with `>`) match anything
/// - Literal values must match exactly
/// - Records match if labels match and all fields match recursively
/// - Sequences match if lengths are equal and all elements match recursively
/// - Sets and dictionaries use structural equality (no wildcard support yet)
fn matches_pattern(pattern: &preserves::IOValue, value: &preserves::IOValue) -> bool {
    use preserves::ValueImpl;

    // Check for wildcard symbol pattern
    if let Some(sym) = pattern.as_symbol() {
        if is_wildcard_symbol(&sym) {
            return true;
        }
    }

    // Check booleans
    if let (Some(p), Some(v)) = (pattern.as_boolean(), value.as_boolean()) {
        return p == v;
    }

    // Check integers
    if let (Some(p), Some(v)) = (pattern.as_signed_integer(), value.as_signed_integer()) {
        return p == v;
    }

    // Check doubles (floats)
    if let (Some(p), Some(v)) = (pattern.as_double(), value.as_double()) {
        // Use bit-level equality to handle NaN consistently
        return p.to_bits() == v.to_bits();
    }

    // Check strings
    if let (Some(p), Some(v)) = (pattern.as_string(), value.as_string()) {
        return p == v;
    }

    // Check bytestrings
    if let (Some(p), Some(v)) = (pattern.as_bytestring(), value.as_bytestring()) {
        return p == v;
    }

    // Check symbols
    if let (Some(p), Some(v)) = (pattern.as_symbol(), value.as_symbol()) {
        return p == v;
    }

    // Check records
    if pattern.is_record() && value.is_record() {
        let p_label = pattern.label();
        let v_label = value.label();

        if !matches_pattern(&p_label.into(), &v_label.into()) {
            return false;
        }

        if pattern.len() != value.len() {
            return false;
        }

        // Check all fields match
        for i in 0..pattern.len() {
            let p_field = pattern.index(i);
            let v_field = value.index(i);
            if !matches_pattern(&p_field.into(), &v_field.into()) {
                return false;
            }
        }

        return true;
    }

    // Check sequences
    if pattern.is_sequence() && value.is_sequence() {
        if pattern.len() != value.len() {
            return false;
        }

        // Check all elements match
        for i in 0..pattern.len() {
            let p_elem = pattern.index(i);
            let v_elem = value.index(i);
            if !matches_pattern(&p_elem.into(), &v_elem.into()) {
                return false;
            }
        }

        return true;
    }

    // Check sets - use structural equality for now
    if pattern.is_set() && value.is_set() {
        return pattern == value;
    }

    // Check dictionaries - use structural equality for now
    if pattern.is_dictionary() && value.is_dictionary() {
        return pattern == value;
    }

    // Check embedded values
    if let (Some(p), Some(v)) = (pattern.as_embedded(), value.as_embedded()) {
        return p == v;
    }

    // Different types or no match
    false
}

/// Check if a symbol string represents a wildcard pattern
///
/// Wildcard symbols start with '<' and end with '>' (e.g., `<_>`, `<any>`, `<x>`)
fn is_wildcard_symbol(sym: &str) -> bool {
    sym.starts_with('<') && sym.ends_with('>')
}

impl Default for PatternEngine {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use preserves::IOValue;

    #[test]
    fn test_pattern_registration() {
        let mut engine = PatternEngine::new();
        let pattern = Pattern {
            id: Uuid::new_v4(),
            pattern: IOValue::symbol("test-pattern"),
            facet: FacetId::new(),
        };

        let id = pattern.id;
        engine.register(pattern);

        assert!(engine.patterns.contains_key(&id));
    }

    #[test]
    fn test_exact_match() {
        let mut engine = PatternEngine::new();
        let pattern_id = Uuid::new_v4();
        let pattern = Pattern {
            id: pattern_id,
            pattern: IOValue::symbol("hello"),
            facet: FacetId::new(),
        };

        engine.register(pattern);

        // Should match exact symbol
        let handle1 = Handle::new();
        let matches = engine.eval_assert(&handle1, &IOValue::symbol("hello"));
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].pattern_id, pattern_id);

        // Should not match different symbol
        let handle2 = Handle::new();
        let matches = engine.eval_assert(&handle2, &IOValue::symbol("goodbye"));
        assert_eq!(matches.len(), 0);
    }

    #[test]
    fn test_wildcard_match() {
        let mut engine = PatternEngine::new();
        let pattern_id = Uuid::new_v4();
        let pattern = Pattern {
            id: pattern_id,
            pattern: IOValue::symbol("<_>"),
            facet: FacetId::new(),
        };

        engine.register(pattern);

        // Wildcard should match anything
        let handle1 = Handle::new();
        let matches = engine.eval_assert(&handle1, &IOValue::symbol("anything"));
        assert_eq!(matches.len(), 1);

        let handle2 = Handle::new();
        let matches = engine.eval_assert(&handle2, &IOValue::new(42));
        assert_eq!(matches.len(), 1);

        let handle3 = Handle::new();
        let matches = engine.eval_assert(&handle3, &IOValue::new("test".to_string()));
        assert_eq!(matches.len(), 1);
    }

    #[test]
    fn test_record_pattern() {
        let mut engine = PatternEngine::new();
        let pattern_id = Uuid::new_v4();

        // Pattern: (point <x> <y>)
        let pattern_value = IOValue::record(
            IOValue::symbol("point"),
            vec![IOValue::symbol("<x>"), IOValue::symbol("<y>")],
        );

        let pattern = Pattern {
            id: pattern_id,
            pattern: pattern_value,
            facet: FacetId::new(),
        };

        engine.register(pattern);

        // Should match: (point 10 20)
        let handle1 = Handle::new();
        let value1 = IOValue::record(
            IOValue::symbol("point"),
            vec![IOValue::new(10), IOValue::new(20)],
        );
        let matches = engine.eval_assert(&handle1, &value1);
        assert_eq!(matches.len(), 1);

        // Should not match: (point 10) - wrong arity
        let handle2 = Handle::new();
        let value2 = IOValue::record(IOValue::symbol("point"), vec![IOValue::new(10)]);
        let matches = engine.eval_assert(&handle2, &value2);
        assert_eq!(matches.len(), 0);

        // Should not match: (line 10 20) - wrong label
        let handle3 = Handle::new();
        let value3 = IOValue::record(
            IOValue::symbol("line"),
            vec![IOValue::new(10), IOValue::new(20)],
        );
        let matches = engine.eval_assert(&handle3, &value3);
        assert_eq!(matches.len(), 0);
    }

    #[test]
    fn test_sequence_pattern() {
        let mut engine = PatternEngine::new();
        let pattern_id = Uuid::new_v4();

        // Pattern: [1, <_>, 3]
        let pattern_value = IOValue::new(vec![
            IOValue::new(1),
            IOValue::symbol("<_>"),
            IOValue::new(3),
        ]);

        let pattern = Pattern {
            id: pattern_id,
            pattern: pattern_value,
            facet: FacetId::new(),
        };

        engine.register(pattern);

        // Should match: [1, 2, 3]
        let handle1 = Handle::new();
        let value1 = IOValue::new(vec![IOValue::new(1), IOValue::new(2), IOValue::new(3)]);
        let matches = engine.eval_assert(&handle1, &value1);
        assert_eq!(matches.len(), 1);

        // Should match: [1, "anything", 3]
        let handle2 = Handle::new();
        let value2 = IOValue::new(vec![
            IOValue::new(1),
            IOValue::new("anything".to_string()),
            IOValue::new(3),
        ]);
        let matches = engine.eval_assert(&handle2, &value2);
        assert_eq!(matches.len(), 1);

        // Should not match: [1, 2] - wrong length
        let handle3 = Handle::new();
        let value3 = IOValue::new(vec![IOValue::new(1), IOValue::new(2)]);
        let matches = engine.eval_assert(&handle3, &value3);
        assert_eq!(matches.len(), 0);

        // Should not match: [1, 2, 4] - third element doesn't match
        let handle4 = Handle::new();
        let value4 = IOValue::new(vec![IOValue::new(1), IOValue::new(2), IOValue::new(4)]);
        let matches = engine.eval_assert(&handle4, &value4);
        assert_eq!(matches.len(), 0);
    }

    #[test]
    fn test_retraction() {
        let mut engine = PatternEngine::new();
        let pattern_id = Uuid::new_v4();
        let pattern = Pattern {
            id: pattern_id,
            pattern: IOValue::symbol("<_>"),
            facet: FacetId::new(),
        };

        engine.register(pattern);

        // Assert a value
        let handle = Handle::new();
        let matches = engine.eval_assert(&handle, &IOValue::symbol("test"));
        assert_eq!(matches.len(), 1);

        // Verify the match is stored
        let current_matches = engine.get_matches(&pattern_id);
        assert_eq!(current_matches.len(), 1);

        // Retract the assertion
        let affected = engine.eval_retract(&handle);
        assert_eq!(affected.len(), 1);
        assert_eq!(affected[0], pattern_id);

        // Verify the match is removed
        let current_matches = engine.get_matches(&pattern_id);
        assert_eq!(current_matches.len(), 0);
    }

    #[test]
    fn test_multiple_patterns_same_value() {
        let mut engine = PatternEngine::new();

        // Register two patterns
        let pattern1_id = Uuid::new_v4();
        let pattern1 = Pattern {
            id: pattern1_id,
            pattern: IOValue::symbol("<_>"), // Matches anything
            facet: FacetId::new(),
        };
        engine.register(pattern1);

        let pattern2_id = Uuid::new_v4();
        let pattern2 = Pattern {
            id: pattern2_id,
            pattern: IOValue::symbol("test"), // Matches exact symbol
            facet: FacetId::new(),
        };
        engine.register(pattern2);

        // Assert a value that matches both patterns
        let handle = Handle::new();
        let matches = engine.eval_assert(&handle, &IOValue::symbol("test"));
        assert_eq!(matches.len(), 2);

        // Both patterns should have the match
        assert_eq!(engine.get_matches(&pattern1_id).len(), 1);
        assert_eq!(engine.get_matches(&pattern2_id).len(), 1);

        // Retract - should affect both patterns
        let affected = engine.eval_retract(&handle);
        assert_eq!(affected.len(), 2);
    }

    #[test]
    fn test_unregister_pattern() {
        let mut engine = PatternEngine::new();
        let pattern_id = Uuid::new_v4();
        let pattern = Pattern {
            id: pattern_id,
            pattern: IOValue::symbol("<_>"),
            facet: FacetId::new(),
        };

        engine.register(pattern);

        // Assert some values
        let handle1 = Handle::new();
        engine.eval_assert(&handle1, &IOValue::symbol("test1"));
        let handle2 = Handle::new();
        engine.eval_assert(&handle2, &IOValue::symbol("test2"));

        assert_eq!(engine.get_matches(&pattern_id).len(), 2);

        // Unregister the pattern
        engine.unregister(pattern_id);

        // Pattern and matches should be gone
        assert!(!engine.patterns.contains_key(&pattern_id));
        assert_eq!(engine.get_matches(&pattern_id).len(), 0);

        // handle_to_patterns index should be cleaned up
        assert!(!engine.handle_to_patterns.contains_key(&handle1));
        assert!(!engine.handle_to_patterns.contains_key(&handle2));
    }
}
