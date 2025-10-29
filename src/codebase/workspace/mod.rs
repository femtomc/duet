//! Workspace catalog entity
//!
//! This module provides an entity that mirrors the local filesystem into the
//! dataspace. It publishes immutable facts about files/directories and grants
//! capabilities for controlled modification.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use chrono::{DateTime, Utc};
use preserves::ValueImpl;
use serde::{Deserialize, Serialize};
use walkdir::WalkDir;

use crate::runtime::actor::{Activation, CapabilitySpec, Entity};
use crate::runtime::error::{ActorError, ActorResult};
use crate::runtime::registry::EntityCatalog;
use crate::runtime::turn::{FacetId, Handle};
use crate::util::io_value::record_with_label;

use crate::runtime::state::{CapabilityMetadata, CapabilityTarget};
#[cfg(test)]
use crate::runtime::turn::TurnOutput;

const CAP_KIND_READ: &str = "workspace/read";
const CAP_KIND_WRITE: &str = "workspace/write";

/// Configuration accepted by the workspace catalog entity.
#[derive(Debug, Clone, Deserialize, Serialize)]
struct WorkspaceConfig {
    /// Root directory of the workspace (defaults to current directory)
    root: PathBuf,
}

impl WorkspaceConfig {
    fn from_value(config: &preserves::IOValue) -> Self {
        if let Some(path) = config.as_string() {
            Self::normalize(PathBuf::from(path.as_ref()))
        } else {
            Self::normalize(PathBuf::from("."))
        }
    }

    fn normalize(root: PathBuf) -> Self {
        if let Ok(canon) = fs::canonicalize(&root) {
            Self { root: canon }
        } else {
            Self { root }
        }
    }
}

/// Internal memoisation of discovered entries to avoid reasserting duplicates.
#[derive(Debug, Default)]
struct CatalogState {
    entries: HashMap<PathBuf, CatalogEntry>,
}

/// Representation of a single filesystem entry.
#[derive(Debug, Clone, PartialEq, Eq)]
struct FileEntry {
    kind: FileKind,
    size: u64,
    modified: Option<DateTime<Utc>>,
    digest: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FileKind {
    File,
    Directory,
    Symlink,
    Other,
}

#[derive(Debug, Clone)]
struct CatalogEntry {
    handle: Handle,
    data: FileEntry,
}

/// Workspace catalog entity implementation.
pub struct WorkspaceCatalog {
    root: PathBuf,
    state: Arc<Mutex<CatalogState>>,
}

impl WorkspaceCatalog {
    fn new(config: &WorkspaceConfig) -> Self {
        Self {
            root: config.root.clone(),
            state: Arc::new(Mutex::new(CatalogState::default())),
        }
    }

    fn relative(&self, path: &Path) -> PathBuf {
        path.strip_prefix(&self.root).unwrap_or(path).to_path_buf()
    }

    fn describe_entry(&self, path: &Path) -> FileEntry {
        let metadata = fs::symlink_metadata(path).ok();
        if let Some(metadata) = metadata {
            let kind = if metadata.is_file() {
                FileKind::File
            } else if metadata.is_dir() {
                FileKind::Directory
            } else if metadata.file_type().is_symlink() {
                FileKind::Symlink
            } else {
                FileKind::Other
            };

            let size = metadata.len();
            let modified = metadata
                .modified()
                .ok()
                .map(|mtime| DateTime::<Utc>::from(mtime));

            FileEntry {
                kind,
                size,
                modified,
                digest: None,
            }
        } else {
            FileEntry {
                kind: FileKind::Other,
                size: 0,
                modified: None,
                digest: None,
            }
        }
    }

    fn assert_entry(
        &self,
        activation: &mut Activation,
        rel_path: &Path,
        entry: &FileEntry,
        handle: Handle,
    ) -> CatalogEntry {
        let kind_symbol = match entry.kind {
            FileKind::File => "file",
            FileKind::Directory => "dir",
            FileKind::Symlink => "symlink",
            FileKind::Other => "other",
        };

        let mut fields = vec![
            preserves::IOValue::new(self.path_display(rel_path)),
            preserves::IOValue::symbol(kind_symbol),
            preserves::IOValue::new(entry.size as i64),
        ];

        if let Some(timestamp) = entry.modified {
            fields.push(preserves::IOValue::new(timestamp.to_rfc3339()));
        } else {
            fields.push(preserves::IOValue::symbol("unknown"));
        }

        if let Some(digest) = &entry.digest {
            fields.push(preserves::IOValue::new(digest.clone()));
        }

        let fact =
            preserves::IOValue::record(preserves::IOValue::symbol("workspace-entry"), fields);
        activation.assert(handle.clone(), fact);

        CatalogEntry {
            handle,
            data: entry.clone(),
        }
    }

    fn grant_read_capability(&self, activation: &mut Activation, facet: FacetId, rel_path: &Path) {
        let holder_facet = facet.clone();
        let target_facet = facet.clone();
        let spec = CapabilitySpec {
            holder: activation.actor_id.clone(),
            holder_facet,
            target: Some(CapabilityTarget {
                actor: activation.actor_id.clone(),
                facet: Some(target_facet),
            }),
            kind: CAP_KIND_READ.into(),
            attenuation: vec![preserves::IOValue::new(self.path_display(rel_path))],
        };
        activation.grant_capability(spec);
    }

    fn grant_write_capability(&self, activation: &mut Activation, facet: FacetId, rel_path: &Path) {
        let holder_facet = facet.clone();
        let target_facet = facet.clone();
        let spec = CapabilitySpec {
            holder: activation.actor_id.clone(),
            holder_facet,
            target: Some(CapabilityTarget {
                actor: activation.actor_id.clone(),
                facet: Some(target_facet),
            }),
            kind: CAP_KIND_WRITE.into(),
            attenuation: vec![preserves::IOValue::new(self.path_display(rel_path))],
        };
        activation.grant_capability(spec);
    }

    fn rescan(&self, activation: &mut Activation) -> ActorResult<()> {
        let mut catalog = self.state.lock().unwrap();
        let mut previous = std::mem::take(&mut catalog.entries);
        let mut updated = HashMap::new();

        if self.root.exists() {
            for entry in WalkDir::new(&self.root).into_iter().filter_map(|e| e.ok()) {
                let abs_path = entry.path();
                let rel_path = self.relative(abs_path);
                let desc = self.describe_entry(abs_path);

                if let Some(prev) = previous.remove(&rel_path) {
                    if prev.data == desc {
                        updated.insert(rel_path.clone(), prev);
                        continue;
                    } else {
                        activation.retract(prev.handle.clone());
                    }
                }

                let handle = Handle::new();
                let catalog_entry = self.assert_entry(activation, &rel_path, &desc, handle);
                updated.insert(rel_path.clone(), catalog_entry);
            }
        }

        // Retract entries that no longer exist
        for (_, entry) in previous.into_iter() {
            activation.retract(entry.handle);
        }

        catalog.entries = updated;
        Ok(())
    }

    fn path_display(&self, rel_path: &Path) -> String {
        if rel_path.as_os_str().is_empty() {
            String::from(".")
        } else {
            rel_path.to_string_lossy().to_string()
        }
    }

    fn parse_path(&self, payload: &preserves::IOValue, label: &str) -> ActorResult<PathBuf> {
        let record = record_with_label(payload, label).ok_or_else(|| {
            ActorError::InvalidActivation(format!("expected '{label}' payload"))
        })?;

        if record.len() == 0 {
            return Err(ActorError::InvalidActivation(format!(
                "missing path argument for {label}"
            )));
        }

        let path_str = record.field_string(0).ok_or_else(|| {
            ActorError::InvalidActivation(format!("expected string path for {label}"))
        })?;

        Ok(PathBuf::from(path_str))
    }

    fn authorize(&self, metadata: &CapabilityMetadata, rel_path: &Path) -> ActorResult<()> {
        if let Some(first) = metadata.attenuation.first() {
            let base = first.as_string().ok_or_else(|| {
                ActorError::InvalidActivation("capability attenuation must be a string path".into())
            })?;
            let base_path = PathBuf::from(base.as_ref());
            let allowed_root = self.root.join(&base_path);
            let requested = self.root.join(rel_path);

            if !requested.starts_with(&allowed_root) {
                return Err(ActorError::InvalidActivation(format!(
                    "path '{:?}' outside capability scope",
                    rel_path
                )));
            }
        }

        Ok(())
    }

    fn handle_read(
        &self,
        capability: &CapabilityMetadata,
        payload: &preserves::IOValue,
    ) -> ActorResult<preserves::IOValue> {
        let rel_path = self.parse_path(payload, "workspace-read")?;
        self.authorize(capability, &rel_path)?;

        let abs_path = self.root.join(&rel_path);
        let contents = fs::read_to_string(&abs_path).map_err(|err| {
            ActorError::InvalidActivation(format!(
                "failed to read '{}': {}",
                rel_path.display(),
                err
            ))
        })?;

        Ok(preserves::IOValue::new(contents))
    }

    fn handle_write(
        &self,
        activation: &mut Activation,
        capability: &CapabilityMetadata,
        payload: &preserves::IOValue,
    ) -> ActorResult<preserves::IOValue> {
        if !payload.is_record() {
            return Err(ActorError::InvalidActivation(
                "expected record payload for workspace-write".into(),
            ));
        }

        let rel_path = self.parse_path(payload, "workspace-write")?;
        self.authorize(capability, &rel_path)?;

        if payload.len() < 2 {
            return Err(ActorError::InvalidActivation(
                "workspace-write requires content argument".into(),
            ));
        }

        let content_value = payload.index(1);
        let content = content_value.as_string().ok_or_else(|| {
            ActorError::InvalidActivation("workspace-write content must be a string".into())
        })?;

        let abs_path = self.root.join(&rel_path);
        if let Some(parent) = abs_path.parent() {
            fs::create_dir_all(parent).map_err(|err| {
                ActorError::InvalidActivation(format!(
                    "failed to create directories for '{}': {}",
                    rel_path.display(),
                    err
                ))
            })?;
        }

        fs::write(&abs_path, content.as_ref()).map_err(|err| {
            ActorError::InvalidActivation(format!(
                "failed to write '{}': {}",
                rel_path.display(),
                err
            ))
        })?;

        // Update catalog assertions deterministically
        self.rescan(activation)?;

        Ok(preserves::IOValue::symbol("ok"))
    }
}

impl Entity for WorkspaceCatalog {
    fn on_message(
        &self,
        activation: &mut Activation,
        payload: &preserves::IOValue,
    ) -> ActorResult<()> {
        if let Some(symbol) = payload.as_symbol() {
            if symbol.as_ref() == "workspace-rescan" {
                self.rescan(activation)?;
            }
            return Ok(());
        }

        if record_with_label(payload, "workspace-rescan").is_some() {
            self.rescan(activation)?;
            return Ok(());
        }

        if let Some(record) = record_with_label(payload, "workspace-read") {
            if let Some(path) = record.field_string(0) {
                self.grant_read_capability(
                    activation,
                    activation.current_facet.clone(),
                    Path::new(&path),
                );
            }
            return Ok(());
        }

        if let Some(record) = record_with_label(payload, "workspace-write") {
            if let Some(path) = record.field_string(0) {
                self.grant_write_capability(
                    activation,
                    activation.current_facet.clone(),
                    Path::new(&path),
                );
            }
            return Ok(());
        }

        Ok(())
    }

    fn on_capability_invoke(
        &self,
        activation: &mut Activation,
        capability: &CapabilityMetadata,
        payload: &preserves::IOValue,
    ) -> ActorResult<preserves::IOValue> {
        match capability.kind.as_str() {
            CAP_KIND_READ => self.handle_read(capability, payload),
            CAP_KIND_WRITE => self.handle_write(activation, capability, payload),
            other => Err(ActorError::InvalidActivation(format!(
                "unsupported capability kind: {}",
                other
            ))),
        }
    }
}

/// Register the workspace catalog entity with the global registry.
pub fn register(catalog: &EntityCatalog) {
    catalog.register("workspace", |config| {
        let cfg = WorkspaceConfig::from_value(config);
        Ok(Box::new(WorkspaceCatalog::new(&cfg)))
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime::actor::{Actor, Entity};
    use crate::runtime::turn::ActorId;
    use tempfile::tempdir;

    #[test]
    fn rescan_emits_assertions() {
        let temp = tempdir().unwrap();
        let file_path = temp.path().join("hello.txt");
        fs::write(&file_path, b"hello world").unwrap();

        let config = WorkspaceConfig {
            root: temp.path().to_path_buf(),
        };
        let catalog = WorkspaceCatalog::new(&config);

        let actor = Actor::new(ActorId::new());
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        catalog.rescan(&mut activation).unwrap();

        assert!(
            activation
                .outputs
                .iter()
                .any(|output| matches!(output, TurnOutput::Assert { .. })),
            "rescan should emit workspace-entry assertions"
        );
    }

    #[test]
    fn command_grants_capabilities() {
        let temp = tempdir().unwrap();
        let config = WorkspaceConfig {
            root: temp.path().to_path_buf(),
        };
        let catalog = WorkspaceCatalog::new(&config);

        let actor = Actor::new(ActorId::new());
        let facet = actor.root_facet.clone();
        let mut activation = Activation::new(actor.id.clone(), facet.clone(), None);
        activation.set_current_entity(Some(uuid::Uuid::new_v4()));

        let payload = preserves::IOValue::record(
            preserves::IOValue::symbol("workspace-read"),
            vec![preserves::IOValue::new("foo.txt".to_string())],
        );

        Entity::on_message(&catalog, &mut activation, &payload).unwrap();

        assert!(activation
            .outputs
            .iter()
            .any(|output| matches!(output, TurnOutput::CapabilityGranted { kind, .. } if kind == "workspace/read")));
    }
}
