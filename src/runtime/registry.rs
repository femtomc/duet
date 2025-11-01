//! Entity type registry and factory system
//!
//! Provides a global catalog for registering entity factories prior to runtime
//! startup. Each runtime clones an immutable snapshot of the catalog for
//! deterministic instantiation.

use once_cell::sync::Lazy;
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use std::any::Any;
use std::collections::HashMap;
use std::sync::Arc;

use super::actor::{Entity, HydratableEntity};
use super::error::{ActorResult, Result, StorageError};
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

#[derive(Clone)]
struct EntityTypeInfo {
    factory: EntityFactory,
    snapshot: Option<SnapshotHandler>,
    restore: Option<RestoreHandler>,
}

/// Global catalog of entity definitions.
pub struct EntityCatalog {
    types: RwLock<HashMap<String, EntityTypeInfo>>,
}

static CATALOG: Lazy<EntityCatalog> = Lazy::new(|| EntityCatalog::new());

impl EntityCatalog {
    fn new() -> Self {
        Self {
            types: RwLock::new(HashMap::new()),
        }
    }

    /// Access the global catalog singleton.
    pub fn global() -> &'static Self {
        &CATALOG
    }

    /// Register an entity type with a factory function.
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

    /// Register an entity type that implements `Default`.
    pub fn register_default<T>(&self, type_name: EntityTypeName)
    where
        T: Entity + Default + 'static,
    {
        self.register(type_name, |_config| Ok(Box::new(T::default())));
    }

    /// Register a hydratable entity type (supports private state snapshot/restore).
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

    /// Produce an immutable snapshot for a runtime instance.
    pub fn snapshot(&self) -> EntityRegistry {
        let types = self.types.read();
        EntityRegistry {
            types: Arc::new(types.clone()),
        }
    }
}

/// Immutable runtime view of the entity catalog.
#[derive(Clone)]
pub struct EntityRegistry {
    types: Arc<HashMap<String, EntityTypeInfo>>,
}

impl EntityRegistry {
    /// Instantiate an entity of the given type using this runtime snapshot.
    pub fn create(&self, type_name: &str, config: &EntityConfig) -> ActorResult<Box<dyn Entity>> {
        let info = self.types.get(type_name).ok_or_else(|| {
            super::error::ActorError::InvalidActivation(format!(
                "Unknown entity type: {}",
                type_name
            ))
        })?;

        (info.factory)(config)
    }

    /// Check whether the registry snapshot contains the specified type.
    pub fn has_type(&self, type_name: &str) -> bool {
        self.types.contains_key(type_name)
    }

    /// List all entity type identifiers known to this snapshot.
    pub fn list_types(&self) -> Vec<String> {
        self.types.keys().cloned().collect()
    }

    /// Snapshot private entity state if the type supports hydration.
    pub fn snapshot_entity(
        &self,
        type_name: &str,
        entity: &dyn Entity,
    ) -> Option<preserves::IOValue> {
        let info = self.types.get(type_name)?;
        info.snapshot.as_ref().map(|handler| handler(entity))
    }

    /// Restore private entity state if the type supports hydration.
    pub fn restore_entity(
        &self,
        type_name: &str,
        entity: &mut dyn Entity,
        state: &preserves::IOValue,
    ) -> Result<bool> {
        let info = self.types.get(type_name).ok_or_else(|| {
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

    /// Whether this entry corresponds to the actor's root facet.
    #[serde(default)]
    pub is_root_facet: bool,

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

    /// Load entity metadata from JSON file
    pub fn load(path: &std::path::Path) -> Result<Self> {
        if path.exists() {
            let data = std::fs::read(path).map_err(StorageError::from)?;
            let entities: HashMap<uuid::Uuid, EntityMetadata> =
                serde_json::from_slice(&data).map_err(StorageError::from)?;
            Ok(Self { entities })
        } else {
            Ok(Self::new())
        }
    }

    /// Save entity metadata to disk
    pub fn save(&self, path: &std::path::Path) -> Result<()> {
        let data = serde_json::to_vec_pretty(&self.entities).map_err(StorageError::from)?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(StorageError::from)?;
        }
        std::fs::write(path, data).map_err(StorageError::from)?;
        Ok(())
    }

    /// Register new entity metadata
    pub fn register(&mut self, metadata: EntityMetadata) {
        self.entities.insert(metadata.id, metadata);
    }

    /// Remove entity metadata
    pub fn unregister(&mut self, entity_id: &uuid::Uuid) -> Option<EntityMetadata> {
        self.entities.remove(entity_id)
    }

    /// Retrieve metadata by entity ID
    pub fn get(&self, entity_id: &uuid::Uuid) -> Option<&EntityMetadata> {
        self.entities.get(entity_id)
    }

    /// Retrieve mutable metadata by entity ID
    pub fn get_mut(&mut self, entity_id: &uuid::Uuid) -> Option<&mut EntityMetadata> {
        self.entities.get_mut(entity_id)
    }

    /// Iterate over metadata
    pub fn iter(&self) -> impl Iterator<Item = (&uuid::Uuid, &EntityMetadata)> {
        self.entities.iter()
    }

    /// Iterate mutably over metadata entries.
    pub fn iter_mut(&mut self) -> impl Iterator<Item = (&uuid::Uuid, &mut EntityMetadata)> {
        self.entities.iter_mut()
    }

    /// List metadata entries as a vector
    pub fn list(&self) -> Vec<&EntityMetadata> {
        self.entities.values().collect()
    }

    /// List metadata entries for a specific actor
    pub fn list_for_actor(&self, actor: &ActorId) -> Vec<&EntityMetadata> {
        self.entities
            .values()
            .filter(|metadata| &metadata.actor == actor)
            .collect()
    }
}
