//! Runtime orchestrator and public API
//!
//! This module provides the main `Runtime` struct that coordinates all subsystems
//! and exposes the public interface for embedding or controlling the runtime.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

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

use branch::BranchManager;
use journal::{JournalReader, JournalWriter};
use scheduler::Scheduler;
use schema::SchemaRegistry;
use snapshot::SnapshotManager;
use storage::Storage;
use turn::BranchId;

use actor::Actor;
use std::collections::HashMap;

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

    /// Active actors in this runtime
    actors: HashMap<turn::ActorId, Actor>,

    /// Turn counter for snapshot interval
    turn_count: u64,
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
        journal_reader
            .validate_and_repair()
            .map_err(|e| RuntimeError::Init(format!("Journal validation failed: {}", e)))?;

        // Rebuild index from actual segment data
        let clean_index = journal_reader
            .rebuild_index()
            .map_err(|e| RuntimeError::Init(format!("Index rebuild failed: {}", e)))?;

        // Save the clean index to disk
        let index_path = storage
            .branch_meta_dir(&current_branch)
            .join("journal.index");
        std::fs::create_dir_all(storage.branch_meta_dir(&current_branch))
            .map_err(|e| RuntimeError::Init(format!("Failed to create meta dir: {}", e)))?;
        clean_index
            .save(&index_path)
            .map_err(|e| RuntimeError::Init(format!("Failed to save index: {}", e)))?;

        // Now create journal writer with the clean index
        let journal_writer =
            JournalWriter::new_with_index(storage.clone(), current_branch.clone(), clean_index)
                .map_err(|e| {
                    RuntimeError::Init(format!("Failed to create journal writer: {}", e))
                })?;

        Ok(Self {
            config,
            storage,
            scheduler,
            journal_writer,
            snapshot_manager,
            branch_manager,
            current_branch,
            actors: HashMap::new(),
            turn_count: 0,
        })
    }

    /// Execute a single turn
    ///
    /// Takes the next ready turn from the scheduler, executes it,
    /// records it to the journal, and updates state.
    pub fn execute_turn(&mut self) -> Result<Option<TurnRecord>> {
        // Get next ready turn from scheduler
        let scheduled_turn = match self.scheduler.next_turn() {
            Some(turn) => turn,
            None => return Ok(None), // No turns ready
        };

        let actor_id = scheduled_turn.actor.clone();
        let clock = scheduled_turn.clock;
        let inputs = scheduled_turn.inputs;

        // Get or create actor
        let actor = self.actors
            .entry(actor_id.clone())
            .or_insert_with(|| Actor::new(actor_id.clone()));

        // Execute the turn
        let (outputs, delta) = actor
            .execute_turn(inputs.clone())
            .map_err(|e| RuntimeError::Actor(e))?;

        // Update flow control in scheduler (before consuming delta)
        let borrowed = delta.accounts.borrowed;
        let repaid = delta.accounts.repaid;
        self.scheduler.update_account(&actor_id, borrowed, repaid);

        // Build turn record
        let parent = None; // TODO: Track parent turn ID for causality
        let turn_record = TurnRecord::new(
            actor_id.clone(),
            self.current_branch.clone(),
            clock,
            parent,
            inputs,
            outputs,
            delta,
        );

        // Append to journal
        self.journal_writer
            .append(&turn_record)
            .map_err(|e| RuntimeError::Journal(e))?;

        // Update turn count
        self.turn_count += 1;

        // Check if we should create a snapshot
        if self.snapshot_manager.should_snapshot(self.turn_count) {
            self.create_snapshot()?;
        }

        Ok(Some(turn_record))
    }

    /// Step the runtime forward by one turn
    pub fn step(&mut self) -> Result<Option<TurnRecord>> {
        self.execute_turn()
    }

    /// Step the runtime forward by N turns
    pub fn step_n(&mut self, count: usize) -> Result<Vec<TurnRecord>> {
        let mut records = Vec::new();

        for _ in 0..count {
            match self.execute_turn()? {
                Some(record) => records.push(record),
                None => break, // No more ready turns
            }
        }

        Ok(records)
    }

    /// Create a snapshot of current runtime state
    fn create_snapshot(&mut self) -> Result<()> {
        use snapshot::RuntimeSnapshot;
        use state::{AssertionSet, FacetMap, CapabilityMap};

        // Collect current state from all actors
        let mut all_assertions = AssertionSet::new();
        let mut all_facets = FacetMap::new();
        let mut all_capabilities = CapabilityMap::new();

        for actor in self.actors.values() {
            // Merge actor state into snapshot
            let actor_assertions = actor.assertions.read();
            all_assertions = all_assertions.join(&actor_assertions);

            let actor_facets = actor.facets.read();
            all_facets = all_facets.join(&actor_facets);

            let actor_caps = actor.capabilities.read();
            all_capabilities = all_capabilities.join(&actor_caps);
        }

        // TODO: Get the actual turn ID of the last executed turn
        let turn_id = TurnId::new(format!("turn_{:08}", self.turn_count));

        let snapshot = RuntimeSnapshot {
            branch: self.current_branch.clone(),
            turn_id,
            assertions: all_assertions,
            facets: all_facets,
            capabilities: all_capabilities,
            metadata: snapshot::SnapshotMetadata {
                created_at: chrono::Utc::now(),
                turn_count: self.turn_count,
            },
        };

        self.snapshot_manager
            .save(&snapshot)
            .map_err(|e| RuntimeError::Snapshot(e))?;

        Ok(())
    }

    /// Enqueue a message to an actor
    pub fn send_message(
        &mut self,
        target_actor: turn::ActorId,
        target_facet: turn::FacetId,
        payload: preserves::IOValue,
    ) {
        use scheduler::ScheduleCause;

        let input = turn::TurnInput::ExternalMessage {
            actor: target_actor.clone(),
            facet: target_facet,
            payload,
        };

        self.scheduler.enqueue(target_actor, input, ScheduleCause::External);
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
pub use error::{Result, RuntimeError};
pub use turn::{TurnId, TurnRecord};
