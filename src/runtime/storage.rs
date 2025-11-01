//! Filesystem layout helpers and atomic write operations
//!
//! Manages the .duet/ directory structure, ensures atomic writes via
//! temp files and renames, and provides utilities for persistence.

use super::RuntimeConfig;
use super::branch::BranchState;
use super::error::{StorageError, StorageResult};
use super::turn::BranchId;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

const EXAMPLES_DIR: &str = "examples";
const PROGRAMS_DIR: &str = "programs";

/// Storage manager for runtime persistence
#[derive(Debug, Clone)]
pub struct Storage {
    root: PathBuf,
}

impl Storage {
    /// Create a new storage manager
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    /// Get the root directory
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Get the config file path
    pub fn config_path(&self) -> PathBuf {
        self.root.join("config.json")
    }

    /// Get the meta directory path
    pub fn meta_dir(&self) -> PathBuf {
        self.root.join("meta")
    }

    /// Get the journal directory path
    pub fn journal_dir(&self) -> PathBuf {
        self.root.join("journal")
    }

    /// Get the snapshots directory path
    pub fn snapshots_dir(&self) -> PathBuf {
        self.root.join("snapshots")
    }

    /// Get branch-specific meta directory
    pub fn branch_meta_dir(&self, branch: &BranchId) -> PathBuf {
        self.meta_dir().join(&branch.0)
    }

    /// Get branch-specific journal directory
    pub fn branch_journal_dir(&self, branch: &BranchId) -> PathBuf {
        self.journal_dir().join(&branch.0)
    }

    /// Get branch-specific snapshot directory
    pub fn branch_snapshot_dir(&self, branch: &BranchId) -> PathBuf {
        self.snapshots_dir().join(&branch.0)
    }

    /// Get branch index file path
    pub fn branch_index_path(&self, branch: &BranchId) -> PathBuf {
        self.meta_dir().join(format!("{}.index", branch.0))
    }

    /// Path of the persisted branch state file
    pub fn branch_state_path(&self) -> PathBuf {
        self.meta_dir().join("branches.json")
    }

    /// Write data atomically to a file
    ///
    /// Creates a temporary file, writes the data, syncs, then renames
    pub fn write_atomic(&self, path: &Path, data: &[u8]) -> StorageResult<()> {
        let temp_path = path.with_extension("tmp");

        // Write to temporary file
        let mut file = File::create(&temp_path).map_err(|e| StorageError::AtomicWriteFailed {
            path: temp_path.clone(),
            detail: e.to_string(),
        })?;

        file.write_all(data)
            .map_err(|e| StorageError::AtomicWriteFailed {
                path: temp_path.clone(),
                detail: e.to_string(),
            })?;

        file.sync_all()
            .map_err(|e| StorageError::AtomicWriteFailed {
                path: temp_path.clone(),
                detail: e.to_string(),
            })?;

        drop(file);

        // Rename atomically
        fs::rename(&temp_path, path).map_err(|e| StorageError::AtomicWriteFailed {
            path: path.to_path_buf(),
            detail: e.to_string(),
        })?;

        // Sync parent directory
        if let Some(parent) = path.parent() {
            let dir = OpenOptions::new().read(true).open(parent).map_err(|e| {
                StorageError::AtomicWriteFailed {
                    path: parent.to_path_buf(),
                    detail: e.to_string(),
                }
            })?;

            dir.sync_all()
                .map_err(|e| StorageError::AtomicWriteFailed {
                    path: parent.to_path_buf(),
                    detail: e.to_string(),
                })?;
        }

        Ok(())
    }

    /// Read a file
    pub fn read_file(&self, path: &Path) -> StorageResult<Vec<u8>> {
        fs::read(path).map_err(StorageError::from)
    }

    /// Check if a path exists
    pub fn exists(&self, path: &Path) -> bool {
        path.exists()
    }

    /// Create a directory and all parent directories
    pub fn create_dir_all(&self, path: &Path) -> StorageResult<()> {
        fs::create_dir_all(path).map_err(StorageError::from)
    }

    /// List files in a directory
    pub fn list_dir(&self, path: &Path) -> StorageResult<Vec<PathBuf>> {
        let mut entries = Vec::new();

        for entry in fs::read_dir(path).map_err(StorageError::from)? {
            let entry = entry?;
            entries.push(entry.path());
        }

        Ok(entries)
    }
}

/// Initialize storage directories for a new runtime
pub fn init_storage(root: &Path) -> StorageResult<()> {
    let storage = Storage::new(root.to_path_buf());

    // Create all required directories
    storage.create_dir_all(root)?;
    storage.create_dir_all(&storage.meta_dir())?;
    storage.create_dir_all(&storage.journal_dir())?;
    storage.create_dir_all(&storage.snapshots_dir())?;

    // Create main branch directories
    let main_branch = BranchId::main();
    storage.create_dir_all(&storage.branch_journal_dir(&main_branch))?;
    storage.create_dir_all(&storage.branch_snapshot_dir(&main_branch))?;

    // Prepare program directory structure for future interpreter modules.
    let programs_dir = storage.root.join(PROGRAMS_DIR);
    storage.create_dir_all(&programs_dir)?;
    storage.create_dir_all(&programs_dir.join(EXAMPLES_DIR))?;

    Ok(())
}

/// Write runtime configuration
pub fn write_config(config: &RuntimeConfig) -> StorageResult<()> {
    let storage = Storage::new(config.root.clone());
    let config_path = storage.config_path();

    let json = serde_json::to_vec_pretty(config).map_err(StorageError::from)?;

    storage.write_atomic(&config_path, &json)?;

    Ok(())
}

/// Load runtime configuration
pub fn load_config(root: &Path) -> StorageResult<RuntimeConfig> {
    let storage = Storage::new(root.to_path_buf());
    let config_path = storage.config_path();

    let data = storage.read_file(&config_path)?;
    let config: RuntimeConfig = serde_json::from_slice(&data).map_err(StorageError::from)?;

    Ok(config)
}

/// Persist branch state metadata
pub fn save_branch_state(storage: &Storage, state: &BranchState) -> StorageResult<()> {
    let path = storage.branch_state_path();
    if let Some(parent) = path.parent() {
        storage.create_dir_all(parent)?;
    }
    let data = serde_json::to_vec_pretty(state).map_err(StorageError::from)?;
    storage.write_atomic(&path, &data)?;
    Ok(())
}

/// Load branch state metadata if available
pub fn load_branch_state(storage: &Storage) -> StorageResult<Option<BranchState>> {
    let path = storage.branch_state_path();
    if !path.exists() {
        return Ok(None);
    }
    let data = storage.read_file(&path)?;
    let state = serde_json::from_slice(&data).map_err(StorageError::from)?;
    Ok(Some(state))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_init_storage() {
        let temp = TempDir::new().unwrap();
        let root = temp.path();

        init_storage(root).unwrap();

        assert!(root.join("meta").exists());
        assert!(root.join("journal").exists());
        assert!(root.join("snapshots").exists());
        assert!(root.join("journal/main").exists());
        assert!(root.join("snapshots/main").exists());
        assert!(root.join(PROGRAMS_DIR).exists());
        assert!(root.join(PROGRAMS_DIR).join(EXAMPLES_DIR).exists());
    }

    #[test]
    fn test_write_and_read_config() {
        let temp = TempDir::new().unwrap();
        let root = temp.path().to_path_buf();

        init_storage(&root).unwrap();

        let config = RuntimeConfig {
            root: root.clone(),
            snapshot_interval: 100,
            flow_control_limit: 5000,
            debug: true,
        };

        write_config(&config).unwrap();
        let loaded = load_config(&root).unwrap();

        assert_eq!(loaded.snapshot_interval, 100);
        assert_eq!(loaded.flow_control_limit, 5000);
        assert_eq!(loaded.debug, true);
    }

    #[test]
    fn test_atomic_write() {
        let temp = TempDir::new().unwrap();
        let storage = Storage::new(temp.path().to_path_buf());
        let test_file = temp.path().join("test.dat");

        let data = b"Hello, world!";
        storage.write_atomic(&test_file, data).unwrap();

        let read_data = storage.read_file(&test_file).unwrap();
        assert_eq!(data, &read_data[..]);
    }
}
