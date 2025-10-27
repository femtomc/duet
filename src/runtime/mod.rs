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
    pub fn new(config: RuntimeConfig) -> anyhow::Result<Self> {
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

        // Initialize journal writer for main branch
        let journal_writer = JournalWriter::new(storage.clone(), current_branch.clone())?;

        // Perform crash recovery on the journal
        let journal_reader = JournalReader::new(storage.clone(), current_branch.clone())?;
        journal_reader.validate_and_repair()?;

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
    pub fn init(config: RuntimeConfig) -> anyhow::Result<()> {
        storage::init_storage(&config.root)?;
        storage::write_config(&config)?;
        Ok(())
    }

    /// Load an existing runtime from storage
    pub fn load(root: PathBuf) -> anyhow::Result<Self> {
        let config = storage::load_config(&root)?;
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
