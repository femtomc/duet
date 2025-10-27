//! Entity type registry and factory system
//!
//! Provides a global registry for entity types that can be instantiated from
//! configuration. Registered at runtime startup by application code.

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use std::any::Any;
use std::collections::HashMap;
use std::sync::Arc;

use super::actor::{Entity, HydratableEntity};
use super::error::{ActorResult, Result};
use super::pattern::Pattern;
use super::turn::{ActorId, FacetId};

/// Entity type name (e.g., "llm-assistant", "timer-manager")
pub type EntityTypeName = &'static str;

/// Entity configuration (preserves value, application-specific)
pub type EntityConfig = preserves::IOValue;

/// Factory function that creates an entity from configuration
pub type EntityFactory = Arc<dyn Fn(&EntityConfig) -> ActorResult<Box<dyn Entity>> + Send + Sync>;

type SnapshotHandler = Arc<dyn Fn(&dyn Entity) -> preserves::IOValue + Send + Sync>;
type RestoreHandler =
    Arc<dyn Fn(&mut dyn Entity, &preserves::IOValue) -> ActorResult<()> + Send + Sync>;

struct EntityTypeInfo {
    factory: EntityFactory,
    snapshot: Option<SnapshotHandler>,
    restore: Option<RestoreHandler>,
}

/// Global entity type registry
///
/// Maps entity type names to factory functions. Types must be registered
/// at runtime startup before they can be instantiated.
pub struct EntityRegistry {
    types: Arc<RwLock<HashMap<String, EntityTypeInfo>>>,
}

// Global singleton instance
static REGISTRY: once_cell::sync::Lazy<EntityRegistry> =
    once_cell::sync::Lazy::new(|| EntityRegistry::new());

impl EntityRegistry {
    /// Create a new empty registry
    fn new() -> Self {
        Self {
            types: Arc::new(RwLock::new(HashMap::new())),
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
        let mut types = self.types.write();
        types.insert(
            type_name.to_string(),
            EntityTypeInfo {
                factory: Arc::new(factory),
                snapshot: None,
                restore: None,
            },
        );
    }

    /// Register an entity type that implements Default
    pub fn register_default<T>(&self, type_name: EntityTypeName)
    where
        T: Entity + Default + 'static,
    {
        self.register(type_name, |_config| Ok(Box::new(T::default())));
    }

    /// Register a hydratable entity type (supports private state snapshot/restore)
    pub fn register_hydratable<T, F>(&self, type_name: EntityTypeName, factory: F)
    where
        T: HydratableEntity + 'static,
        F: Fn(&EntityConfig) -> ActorResult<T> + Send + Sync + 'static,
    {
        let entity_factory: EntityFactory = Arc::new(move |config: &EntityConfig| {
            let entity = factory(config)?;
            Ok(Box::new(entity) as Box<dyn Entity>)
        });

        let snapshot_handler: SnapshotHandler = Arc::new(move |entity: &dyn Entity| {
            let concrete = (entity as &dyn Any)
                .downcast_ref::<T>()
                .expect("Hydratable entity type mismatch during snapshot");
            concrete.snapshot_state()
        });

        let restore_handler: RestoreHandler = Arc::new(move |entity: &mut dyn Entity, state| {
            let concrete = (entity as &mut dyn Any)
                .downcast_mut::<T>()
                .expect("Hydratable entity type mismatch during restore");
            concrete.restore_state(state)
        });

        let mut types = self.types.write();
        types.insert(
            type_name.to_string(),
            EntityTypeInfo {
                factory: entity_factory,
                snapshot: Some(snapshot_handler),
                restore: Some(restore_handler),
            },
        );
    }

    /// Create an entity instance from type name and config
    pub fn create(&self, type_name: &str, config: &EntityConfig) -> ActorResult<Box<dyn Entity>> {
        let types = self.types.read();

        let info = types.get(type_name).ok_or_else(|| {
            super::error::ActorError::InvalidActivation(format!(
                "Unknown entity type: {}",
                type_name
            ))
        })?;

        (info.factory)(config)
    }

    /// Check if a type is registered
    pub fn has_type(&self, type_name: &str) -> bool {
        let types = self.types.read();
        types.contains_key(type_name)
    }

    /// List all registered entity types
    pub fn list_types(&self) -> Vec<String> {
        let types = self.types.read();
        types.keys().cloned().collect()
    }

    /// Snapshot private entity state if supported
    pub fn snapshot_entity(
        &self,
        type_name: &str,
        entity: &dyn Entity,
    ) -> Option<preserves::IOValue> {
        let types = self.types.read();
        let info = types.get(type_name)?;
        info.snapshot.as_ref().map(|handler| handler(entity))
    }

    /// Restore private entity state if supported
    pub fn restore_entity(
        &self,
        type_name: &str,
        entity: &mut dyn Entity,
        state: &preserves::IOValue,
    ) -> Result<bool> {
        let types = self.types.read();

        let info = types.get(type_name).ok_or_else(|| {
            super::error::ActorError::InvalidActivation(format!(
                "Unknown entity type: {}",
                type_name
            ))
        })?;

        if let Some(handler) = &info.restore {
            handler(entity, state)?;
            Ok(true)
        } else {
            Ok(false)
        }
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
    pub patterns: Vec<Pattern>,
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
        let data =
            serde_json::to_vec_pretty(&self.entities).map_err(super::error::StorageError::Json)?;

        storage.write_atomic(path, &data)?;

        Ok(())
    }

    /// Load entity metadata from JSON file
    pub fn load(path: &std::path::Path) -> Result<Self> {
        if !path.exists() {
            return Ok(Self::new());
        }

        let data = std::fs::read(path).map_err(super::error::StorageError::Io)?;

        let entities = serde_json::from_slice(&data).map_err(super::error::StorageError::Json)?;

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
    use super::super::actor::Activation;
    use super::super::error::ActorError;
    use super::super::storage::Storage;
    use super::*;
    use std::any::Any;
    use std::sync::Mutex;

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
            let name = config
                .as_string()
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
            let name = config
                .as_string()
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

    #[derive(Default)]
    struct HydratableTestEntity {
        value: Mutex<i32>,
    }

    impl Entity for HydratableTestEntity {
        fn on_message(
            &self,
            _activation: &mut Activation,
            _payload: &preserves::IOValue,
        ) -> ActorResult<()> {
            Ok(())
        }
    }

    impl HydratableEntity for HydratableTestEntity {
        fn snapshot_state(&self) -> preserves::IOValue {
            let value = *self.value.lock().unwrap();
            preserves::IOValue::new(value.to_string())
        }

        fn restore_state(&mut self, state: &preserves::IOValue) -> ActorResult<()> {
            let text = match state.as_string() {
                Some(s) => s,
                None => {
                    return Err(ActorError::InvalidActivation(
                        "expected string for hydratable state".to_string(),
                    ));
                }
            };

            let value: i32 = match text.parse() {
                Ok(v) => v,
                Err(e) => return Err(ActorError::InvalidActivation(format!("{e}"))),
            };

            *self.value.lock().unwrap() = value;
            Ok(())
        }
    }

    #[test]
    fn test_register_hydratable_snapshot_restore() {
        let registry = EntityRegistry::new();

        registry.register_hydratable("hydrated", |_config| Ok(HydratableTestEntity::default()));

        assert!(registry.has_type("hydrated"));

        let mut entity = registry
            .create("hydrated", &preserves::IOValue::symbol("cfg"))
            .unwrap();

        let concrete = (&mut *entity as &mut dyn Any)
            .downcast_mut::<HydratableTestEntity>()
            .unwrap();
        *concrete.value.lock().unwrap() = 7;

        let snapshot = registry
            .snapshot_entity("hydrated", entity.as_ref())
            .unwrap();

        let mut restored = registry
            .create("hydrated", &preserves::IOValue::symbol("cfg"))
            .unwrap();

        registry
            .restore_entity("hydrated", restored.as_mut(), &snapshot)
            .unwrap();

        let restored_concrete = (&*restored as &dyn Any)
            .downcast_ref::<HydratableTestEntity>()
            .unwrap();
        assert_eq!(*restored_concrete.value.lock().unwrap(), 7);
    }

    #[test]
    fn test_global_registry() {
        // Get global instance
        let registry = EntityRegistry::global();

        // Register a type
        registry.register("global-test", |_| Ok(Box::new(TestEntity::default())));

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
