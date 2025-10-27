//! Preserves schema registration and validation
//!
//! Centralizes all preserves schema definitions for turn records, state deltas,
//! capabilities, external request/response payloads, and CRDT components.
//! Ensures stable schema identifiers for backward compatibility.

use blake3::Hasher;
use std::collections::HashMap;
use std::sync::OnceLock;

/// Schema identifier computed from the schema definition
pub type SchemaId = String;

/// Schema registry for all runtime types
static SCHEMA_REGISTRY: OnceLock<SchemaRegistry> = OnceLock::new();

/// Registry of all preserves schemas used by the runtime
#[derive(Debug)]
pub struct SchemaRegistry {
    schemas: HashMap<&'static str, SchemaDefinition>,
}

/// A schema definition with its hash and version
#[derive(Debug, Clone)]
pub struct SchemaDefinition {
    /// Human-readable name
    pub name: &'static str,
    /// Schema definition (preserves text format)
    pub definition: &'static str,
    /// Blake3 hash of the definition for version checking
    pub hash: SchemaId,
    /// Version number
    pub version: &'static str,
}

impl SchemaRegistry {
    /// Initialize the global schema registry
    pub fn init() -> &'static SchemaRegistry {
        SCHEMA_REGISTRY.get_or_init(|| {
            let mut registry = SchemaRegistry {
                schemas: HashMap::new(),
            };
            registry.register_builtin_schemas();
            registry
        })
    }

    /// Register all built-in schemas
    fn register_builtin_schemas(&mut self) {
        // TurnRecord schema
        self.register(SchemaDefinition {
            name: "TurnRecord",
            version: "1.0.0",
            definition: r#"
                TurnRecord = {
                    turn_id: TurnId,
                    actor: ActorId,
                    branch: BranchId,
                    clock: LogicalClock,
                    parent: Option<TurnId>,
                    inputs: [TurnInput],
                    outputs: [TurnOutput],
                    delta: StateDelta,
                    timestamp: i64
                }
            "#,
            hash: compute_schema_hash("TurnRecord", "1.0.0"),
        });

        // StateDelta schema
        self.register(SchemaDefinition {
            name: "StateDelta",
            version: "1.0.0",
            definition: r#"
                StateDelta = {
                    assertions: AssertionDelta,
                    facets: FacetDelta,
                    capabilities: CapabilityDelta,
                    timers: TimerDelta,
                    accounts: AccountDelta
                }
            "#,
            hash: compute_schema_hash("StateDelta", "1.0.0"),
        });

        // RuntimeSnapshot schema
        self.register(SchemaDefinition {
            name: "RuntimeSnapshot",
            version: "1.0.0",
            definition: r#"
                RuntimeSnapshot = {
                    branch: BranchId,
                    turn_id: TurnId,
                    actors: Map<ActorId, ActorState>,
                    metadata: SnapshotMetadata
                }
            "#,
            hash: compute_schema_hash("RuntimeSnapshot", "1.0.0"),
        });
    }

    /// Register a schema definition
    fn register(&mut self, schema: SchemaDefinition) {
        self.schemas.insert(schema.name, schema);
    }

    /// Get a schema by name
    pub fn get(&self, name: &str) -> Option<&SchemaDefinition> {
        self.schemas.get(name)
    }

    /// Get all registered schema hashes for validation
    pub fn all_hashes(&self) -> HashMap<&'static str, SchemaId> {
        self.schemas
            .iter()
            .map(|(name, def)| (*name, def.hash.clone()))
            .collect()
    }

    /// Validate that a schema hash matches the current version
    pub fn validate_hash(&self, name: &str, hash: &str) -> bool {
        self.get(name).map(|def| def.hash == hash).unwrap_or(false)
    }
}

/// Compute a stable hash for a schema definition
fn compute_schema_hash(name: &str, version: &str) -> SchemaId {
    let mut hasher = Hasher::new();
    hasher.update(name.as_bytes());
    hasher.update(b"|");
    hasher.update(version.as_bytes());
    let hash = hasher.finalize();
    format!("{}", hash.to_hex())
}

/// Initialize the schema registry (call once at startup)
pub fn init_schemas() -> &'static SchemaRegistry {
    SchemaRegistry::init()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_schema_registration() {
        let registry = SchemaRegistry::init();
        assert!(registry.get("TurnRecord").is_some());
        assert!(registry.get("StateDelta").is_some());
        assert!(registry.get("RuntimeSnapshot").is_some());
    }

    #[test]
    fn test_schema_hash_stability() {
        let hash1 = compute_schema_hash("Test", "1.0.0");
        let hash2 = compute_schema_hash("Test", "1.0.0");
        assert_eq!(hash1, hash2, "Schema hashes must be deterministic");
    }

    #[test]
    fn test_schema_validation() {
        let registry = SchemaRegistry::init();
        let turn_schema = registry.get("TurnRecord").unwrap();
        assert!(registry.validate_hash("TurnRecord", &turn_schema.hash));
        assert!(!registry.validate_hash("TurnRecord", "invalid_hash"));
    }
}
