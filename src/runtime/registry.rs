//! Entity type registry and factory system
//!
//! Provides a global registry for entity types that can be instantiated from
//! configuration. Registered at runtime startup by application code.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use parking_lot::RwLock;

use super::actor::Entity;
use super::error::{ActorResult, Result};
use super::turn::{ActorId, FacetId};

/// Entity type name (e.g., "llm-assistant", "timer-manager")
pub type EntityTypeName = &'static str;

/// Entity configuration (preserves value, application-specific)
pub type EntityConfig = preserves::IOValue;

/// Factory function that creates an entity from configuration
pub type EntityFactory = Arc<dyn Fn(&EntityConfig) -> ActorResult<Box<dyn Entity>> + Send + Sync>;

/// Global entity type registry
///
/// Maps entity type names to factory functions. Types must be registered
/// at runtime startup before they can be instantiated.
pub struct EntityRegistry {
    factories: Arc<RwLock<HashMap<String, EntityFactory>>>,
}

// Global singleton instance
static REGISTRY: once_cell::sync::Lazy<EntityRegistry> = once_cell::sync::Lazy::new(|| {
    EntityRegistry::new()
});

impl EntityRegistry {
    /// Create a new empty registry
    fn new() -> Self {
        Self {
            factories: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Get the global registry instance
    pub fn global() -> &'static EntityRegistry {
        &REGISTRY
    }

    /// Register an entity type with a factory function
    pub fn register<F>(&self, type_name: EntityTypeName, factory: F)
    where
        F: Fn(&EntityConfig) -> ActorResult<Box<dyn Entity>> + Send + Sync + 'static,
    {
        let mut factories = self.factories.write();
        factories.insert(type_name.to_string(), Arc::new(factory));
    }

    /// Register an entity type that implements Default
    pub fn register_default<T>(&self, type_name: EntityTypeName)
    where
        T: Entity + Default + 'static,
    {
        self.register(type_name, |_config| {
            Ok(Box::new(T::default()))
        });
    }

    /// Create an entity instance from type name and config
    pub fn create(
        &self,
        type_name: &str,
        config: &EntityConfig,
    ) -> ActorResult<Box<dyn Entity>> {
        let factories = self.factories.read();

        let factory = factories.get(type_name).ok_or_else(|| {
            super::error::ActorError::InvalidActivation(
                format!("Unknown entity type: {}", type_name)
            )
        })?;

        factory(config)
    }

    /// Check if a type is registered
    pub fn has_type(&self, type_name: &str) -> bool {
        let factories = self.factories.read();
        factories.contains_key(type_name)
    }

    /// List all registered entity types
    pub fn list_types(&self) -> Vec<String> {
        let factories = self.factories.read();
        factories.keys().cloned().collect()
    }
}

/// Metadata for a registered entity instance (serializable)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EntityMetadata {
    /// Unique entity instance ID
    pub id: uuid::Uuid,

    /// Actor this entity belongs to
    pub actor: ActorId,

    /// Facet this entity is attached to
    pub facet: FacetId,

    /// Entity type identifier
    pub entity_type: String,

    /// Entity configuration (as preserves text)
    #[serde(with = "preserves_text_serde")]
    pub config: EntityConfig,

    /// Pattern subscriptions registered by this entity
    pub patterns: Vec<uuid::Uuid>,
}

/// Custom serde module for preserves::IOValue (serialize as text)
pub mod preserves_text_serde {
    use serde::{Deserialize, Deserializer, Serializer};

    /// Serialize preserves::IOValue as text
    pub fn serialize<S>(value: &preserves::IOValue, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let text = format!("{:?}", value);
        serializer.serialize_str(&text)
    }

    /// Deserialize preserves::IOValue from text
    pub fn deserialize<'de, D>(deserializer: D) -> Result<preserves::IOValue, D::Error>
    where
        D: Deserializer<'de>,
    {
        let text = String::deserialize(deserializer)?;
        text.parse().map_err(serde::de::Error::custom)
    }
}

/// Manager for entity instance metadata and persistence
pub struct EntityManager {
    /// Registered entities by instance ID
    pub(crate) entities: HashMap<uuid::Uuid, EntityMetadata>,
}

impl EntityManager {
    /// Create a new entity manager
    pub fn new() -> Self {
        Self {
            entities: HashMap::new(),
        }
    }

    /// Register a new entity instance
    pub fn register(&mut self, metadata: EntityMetadata) -> uuid::Uuid {
        let id = metadata.id;
        self.entities.insert(id, metadata);
        id
    }

    /// Unregister an entity instance
    pub fn unregister(&mut self, id: &uuid::Uuid) -> Option<EntityMetadata> {
        self.entities.remove(id)
    }

    /// Get entity metadata
    pub fn get(&self, id: &uuid::Uuid) -> Option<&EntityMetadata> {
        self.entities.get(id)
    }

    /// List all registered entities
    pub fn list(&self) -> Vec<&EntityMetadata> {
        self.entities.values().collect()
    }

    /// List entities for a specific actor
    pub fn list_for_actor(&self, actor: &ActorId) -> Vec<&EntityMetadata> {
        self.entities
            .values()
            .filter(|e| &e.actor == actor)
            .collect()
    }

    /// List entities for a specific facet
    pub fn list_for_facet(&self, facet: &FacetId) -> Vec<&EntityMetadata> {
        self.entities
            .values()
            .filter(|e| &e.facet == facet)
            .collect()
    }

    /// Save entity metadata to JSON file (atomic write)
    pub fn save(&self, storage: &super::storage::Storage, path: &std::path::Path) -> Result<()> {
        let data = serde_json::to_vec_pretty(&self.entities)
            .map_err(super::error::StorageError::Json)?;

        storage.write_atomic(path, &data)?;

        Ok(())
    }

    /// Load entity metadata from JSON file
    pub fn load(path: &std::path::Path) -> Result<Self> {
        if !path.exists() {
            return Ok(Self::new());
        }

        let data = std::fs::read(path)
            .map_err(super::error::StorageError::Io)?;

        let entities = serde_json::from_slice(&data)
            .map_err(super::error::StorageError::Json)?;

        Ok(Self { entities })
    }
}

impl Default for EntityManager {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::actor::Activation;
    use super::super::storage::Storage;

    struct TestEntity {
        name: String,
    }

    impl Entity for TestEntity {
        fn on_message(
            &self,
            _activation: &mut Activation,
            _payload: &preserves::IOValue,
        ) -> ActorResult<()> {
            Ok(())
        }
    }

    impl Default for TestEntity {
        fn default() -> Self {
            Self {
                name: "default".to_string(),
            }
        }
    }

    #[test]
    fn test_entity_registry_registration() {
        let registry = EntityRegistry::new();

        // Register with custom factory
        registry.register("test-entity", |config| {
            let name = config.as_string()
                .map(|s| s.to_string())
                .unwrap_or_else(|| "default".to_string());

            Ok(Box::new(TestEntity { name }))
        });

        assert!(registry.has_type("test-entity"));
        assert!(!registry.has_type("unknown"));

        let types = registry.list_types();
        assert_eq!(types.len(), 1);
        assert_eq!(types[0], "test-entity");
    }

    #[test]
    fn test_entity_creation() {
        use preserves::ValueImpl;

        let registry = EntityRegistry::new();

        registry.register("test-entity", |config| {
            let name = config.as_string()
                .map(|s| s.to_string())
                .unwrap_or_else(|| "default".to_string());

            Ok(Box::new(TestEntity { name }))
        });

        let config = preserves::IOValue::new("test-name".to_string());
        let entity = registry.create("test-entity", &config);

        assert!(entity.is_ok());
    }

    #[test]
    fn test_entity_registry_default_helper() {
        let registry = EntityRegistry::new();

        registry.register_default::<TestEntity>("test-default");

        assert!(registry.has_type("test-default"));

        let config = preserves::IOValue::symbol("ignored");
        let entity = registry.create("test-default", &config);
        assert!(entity.is_ok());
    }

    #[test]
    fn test_global_registry() {
        // Get global instance
        let registry = EntityRegistry::global();

        // Register a type
        registry.register("global-test", |_| {
            Ok(Box::new(TestEntity::default()))
        });

        // Should be accessible
        assert!(registry.has_type("global-test"));
    }

    #[test]
    fn test_entity_manager_persistence() {
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let storage = Storage::new(temp.path().to_path_buf());
        let meta_path = temp.path().join("entities.json");

        let mut manager = EntityManager::new();

        let metadata = EntityMetadata {
            id: uuid::Uuid::new_v4(),
            actor: ActorId::new(),
            facet: FacetId::new(),
            entity_type: "test-entity".into(),
            config: preserves::IOValue::symbol("test-config"),
            patterns: vec![],
        };

        let id = metadata.id;
        manager.register(metadata);

        // Save to disk (atomic)
        manager.save(&storage, &meta_path).unwrap();

        // Load from disk
        let loaded = EntityManager::load(&meta_path).unwrap();
        assert!(loaded.get(&id).is_some());
        assert_eq!(loaded.get(&id).unwrap().entity_type, "test-entity");
    }

    #[test]
    fn test_entity_manager_filtering() {
        let mut manager = EntityManager::new();

        let actor1 = ActorId::new();
        let actor2 = ActorId::new();
        let facet1 = FacetId::new();
        let facet2 = FacetId::new();

        // Register entities for different actors/facets
        manager.register(EntityMetadata {
            id: uuid::Uuid::new_v4(),
            actor: actor1.clone(),
            facet: facet1.clone(),
            entity_type: "type-a".into(),
            config: preserves::IOValue::symbol("config"),
            patterns: vec![],
        });

        manager.register(EntityMetadata {
            id: uuid::Uuid::new_v4(),
            actor: actor1.clone(),
            facet: facet2.clone(),
            entity_type: "type-b".into(),
            config: preserves::IOValue::symbol("config"),
            patterns: vec![],
        });

        manager.register(EntityMetadata {
            id: uuid::Uuid::new_v4(),
            actor: actor2.clone(),
            facet: facet1.clone(),
            entity_type: "type-c".into(),
            config: preserves::IOValue::symbol("config"),
            patterns: vec![],
        });

        // Filter by actor
        let actor1_entities = manager.list_for_actor(&actor1);
        assert_eq!(actor1_entities.len(), 2);

        // Filter by facet
        let facet1_entities = manager.list_for_facet(&facet1);
        assert_eq!(facet1_entities.len(), 2);
    }
}
