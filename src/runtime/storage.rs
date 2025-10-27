//! Filesystem layout helpers and atomic write operations
//!
//! Manages the .duet/ directory structure, ensures atomic writes via
//! temp files and renames, and provides utilities for persistence.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use anyhow::{Context, Result};

use super::RuntimeConfig;
use super::turn::BranchId;

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

    /// Write data atomically to a file
    ///
    /// Creates a temporary file, writes the data, syncs, then renames
    pub fn write_atomic(&self, path: &Path, data: &[u8]) -> Result<()> {
        let temp_path = path.with_extension("tmp");

        // Write to temporary file
        let mut file = File::create(&temp_path)
            .with_context(|| format!("Failed to create temp file: {:?}", temp_path))?;

        file.write_all(data)
            .context("Failed to write data")?;

        file.sync_all()
            .context("Failed to sync file")?;

        drop(file);

        // Rename atomically
        fs::rename(&temp_path, path)
            .with_context(|| format!("Failed to rename {:?} to {:?}", temp_path, path))?;

        // Sync parent directory
        if let Some(parent) = path.parent() {
            let dir = OpenOptions::new()
                .read(true)
                .open(parent)
                .with_context(|| format!("Failed to open directory: {:?}", parent))?;

            dir.sync_all()
                .context("Failed to sync directory")?;
        }

        Ok(())
    }

    /// Read a file
    pub fn read_file(&self, path: &Path) -> Result<Vec<u8>> {
        fs::read(path)
            .with_context(|| format!("Failed to read file: {:?}", path))
    }

    /// Check if a path exists
    pub fn exists(&self, path: &Path) -> bool {
        path.exists()
    }

    /// Create a directory and all parent directories
    pub fn create_dir_all(&self, path: &Path) -> Result<()> {
        fs::create_dir_all(path)
            .with_context(|| format!("Failed to create directory: {:?}", path))
    }

    /// List files in a directory
    pub fn list_dir(&self, path: &Path) -> Result<Vec<PathBuf>> {
        let mut entries = Vec::new();

        for entry in fs::read_dir(path)
            .with_context(|| format!("Failed to read directory: {:?}", path))?
        {
            let entry = entry?;
            entries.push(entry.path());
        }

        Ok(entries)
    }
}

/// Initialize storage directories for a new runtime
pub fn init_storage(root: &Path) -> Result<()> {
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

    Ok(())
}

/// Write runtime configuration
pub fn write_config(config: &RuntimeConfig) -> Result<()> {
    let storage = Storage::new(config.root.clone());
    let config_path = storage.config_path();

    let json = serde_json::to_vec_pretty(config)
        .context("Failed to serialize config")?;

    storage.write_atomic(&config_path, &json)?;

    Ok(())
}

/// Load runtime configuration
pub fn load_config(root: &Path) -> Result<RuntimeConfig> {
    let storage = Storage::new(root.to_path_buf());
    let config_path = storage.config_path();

    let data = storage.read_file(&config_path)?;
    let config: RuntimeConfig = serde_json::from_slice(&data)
        .context("Failed to deserialize config")?;

    Ok(config)
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
