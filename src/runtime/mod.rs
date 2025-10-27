//! Runtime orchestrator and public API
//!
//! This module provides the main `Runtime` struct that coordinates all subsystems
//! and exposes the public interface for embedding or controlling the runtime.

use std::path::PathBuf;
use serde::{Deserialize, Serialize};

// Submodules
pub mod actor;
pub mod branch;
pub mod control;
pub mod error;
pub mod journal;
pub mod pattern;
pub mod scheduler;
pub mod schema;
pub mod snapshot;
pub mod state;
pub mod storage;
pub mod turn;

// Future module (phase 8)
// pub mod link;

/// Configuration for the Duet runtime
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeConfig {
    /// Root directory for runtime storage (default: .duet/)
    pub root: PathBuf,

    /// Number of turns between automatic snapshots
    pub snapshot_interval: u64,

    /// Maximum credit limit for flow-control accounts
    pub flow_control_limit: u64,

    /// Enable debug tracing
    pub debug: bool,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            root: PathBuf::from(".duet"),
            snapshot_interval: 50,
            flow_control_limit: 1000,
            debug: false,
        }
    }
}

use storage::Storage;
use scheduler::Scheduler;
use journal::{JournalWriter, JournalReader};
use snapshot::SnapshotManager;
use branch::BranchManager;
use schema::SchemaRegistry;
use turn::BranchId;

/// The main runtime orchestrator
///
/// Coordinates all subsystems: scheduler, journal, snapshots, branches, and control.
pub struct Runtime {
    config: RuntimeConfig,
    storage: Storage,
    scheduler: Scheduler,
    journal_writer: JournalWriter,
    snapshot_manager: SnapshotManager,
    branch_manager: BranchManager,
    current_branch: BranchId,
}

impl Runtime {
    /// Create a new runtime with the given configuration
    ///
    /// This initializes all subsystems and performs crash recovery if needed.
    pub fn new(config: RuntimeConfig) -> Result<Self> {
        // Initialize storage
        let storage = Storage::new(config.root.clone());

        // Initialize global schema registry (static singleton)
        let _schema_registry = SchemaRegistry::init();

        // Initialize scheduler with flow control limits
        let scheduler = Scheduler::new(config.flow_control_limit as i64);

        // Initialize snapshot manager
        let snapshot_manager = SnapshotManager::new(storage.clone(), config.snapshot_interval);

        // Initialize branch manager
        let branch_manager = BranchManager::new();

        // Use main branch by default
        let current_branch = BranchId::main();

        // CRITICAL: Perform crash recovery BEFORE creating JournalWriter
        // This ensures the index is clean and consistent with actual segment data
        let journal_reader = JournalReader::new(storage.clone(), current_branch.clone())
            .unwrap_or_else(|_| {
                // If index doesn't exist, create reader with empty index
                JournalReader::new_empty(storage.clone(), current_branch.clone())
            });

        // Validate and repair journal (truncates corrupted segments)
        journal_reader.validate_and_repair()
            .map_err(|e| RuntimeError::Init(format!("Journal validation failed: {}", e)))?;

        // Rebuild index from actual segment data
        let clean_index = journal_reader.rebuild_index()
            .map_err(|e| RuntimeError::Init(format!("Index rebuild failed: {}", e)))?;

        // Save the clean index to disk
        let index_path = storage.branch_meta_dir(&current_branch).join("journal.index");
        std::fs::create_dir_all(storage.branch_meta_dir(&current_branch))
            .map_err(|e| RuntimeError::Init(format!("Failed to create meta dir: {}", e)))?;
        clean_index.save(&index_path)
            .map_err(|e| RuntimeError::Init(format!("Failed to save index: {}", e)))?;

        // Now create journal writer with the clean index
        let journal_writer = JournalWriter::new_with_index(
            storage.clone(),
            current_branch.clone(),
            clean_index,
        ).map_err(|e| RuntimeError::Init(format!("Failed to create journal writer: {}", e)))?;

        Ok(Self {
            config,
            storage,
            scheduler,
            journal_writer,
            snapshot_manager,
            branch_manager,
            current_branch,
        })
    }

    /// Initialize runtime storage directories and metadata
    pub fn init(config: RuntimeConfig) -> Result<()> {
        storage::init_storage(&config.root)
            .map_err(|e| RuntimeError::Init(format!("Failed to initialize storage: {}", e)))?;
        storage::write_config(&config)
            .map_err(|e| RuntimeError::Config(format!("Failed to write config: {}", e)))?;
        Ok(())
    }

    /// Load an existing runtime from storage
    pub fn load(root: PathBuf) -> Result<Self> {
        let config = storage::load_config(&root)
            .map_err(|e| RuntimeError::Config(format!("Failed to load config: {}", e)))?;
        Self::new(config)
    }

    /// Get the current configuration
    pub fn config(&self) -> &RuntimeConfig {
        &self.config
    }

    /// Get the current branch
    pub fn current_branch(&self) -> &BranchId {
        &self.current_branch
    }

    /// Get the storage manager
    pub fn storage(&self) -> &Storage {
        &self.storage
    }

    /// Get mutable access to the scheduler
    pub fn scheduler_mut(&mut self) -> &mut Scheduler {
        &mut self.scheduler
    }

    /// Get mutable access to the journal writer
    pub fn journal_writer_mut(&mut self) -> &mut JournalWriter {
        &mut self.journal_writer
    }

    /// Get the snapshot manager
    pub fn snapshot_manager(&self) -> &SnapshotManager {
        &self.snapshot_manager
    }

    /// Get mutable access to the branch manager
    pub fn branch_manager_mut(&mut self) -> &mut BranchManager {
        &mut self.branch_manager
    }

    /// Get the global schema registry
    pub fn schema_registry() -> &'static SchemaRegistry {
        SchemaRegistry::init()
    }
}

// Re-export commonly used types
pub use control::Control;
pub use turn::{TurnId, TurnRecord};
pub use error::{RuntimeError, Result};
