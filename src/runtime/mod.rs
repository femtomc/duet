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

    /// Last turn ID for each actor (for causality tracking)
    last_turn_per_actor: HashMap<turn::ActorId, turn::TurnId>,
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

        // Load branch state (or initialize default)
        let branch_state = match storage::load_branch_state(&storage) {
            Ok(Some(state)) => state,
            Ok(None) => {
                let state = BranchManager::default_state();
                storage::save_branch_state(&storage, &state).map_err(|e| {
                    RuntimeError::Init(format!("Failed to write branch state: {}", e))
                })?;
                state
            }
            Err(e) => {
                return Err(RuntimeError::Init(format!(
                    "Failed to load branch state: {}",
                    e
                )));
            }
        };

        let branch_manager = BranchManager::from_state(branch_state.clone());

        // Use active branch from state
        let current_branch = branch_state.active.clone();

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
            last_turn_per_actor: HashMap::new(),
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
        let actor = self
            .actors
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

        // Build turn record with parent turn tracking
        let parent = self.last_turn_per_actor.get(&actor_id).cloned();
        let turn_record = TurnRecord::new(
            actor_id.clone(),
            self.current_branch.clone(),
            clock,
            parent,
            inputs,
            outputs,
            delta,
        );
        let turn_id = turn_record.turn_id.clone();

        // Update last turn tracker for this actor
        self.last_turn_per_actor
            .insert(actor_id.clone(), turn_id.clone());

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

        self.branch_manager
            .update_head(&self.current_branch, turn_id.clone())
            .map_err(|e| RuntimeError::Branch(e))?;
        self.persist_branch_state()?;

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
        use state::{AssertionSet, CapabilityMap, FacetMap};

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

        self.scheduler
            .enqueue(target_actor, input, ScheduleCause::External);
    }

    /// Fork a new branch from the current branch
    pub fn fork(
        &mut self,
        new_branch_name: impl Into<String>,
        at_turn: Option<TurnId>,
    ) -> Result<BranchId> {
        let current = self.current_branch.clone();
        let new_branch = BranchId::new(new_branch_name);

        // Use current head if no specific turn specified
        let base_turn = at_turn.unwrap_or_else(|| {
            // TODO: Track actual last turn ID
            TurnId::new(format!("turn_{:08}", self.turn_count))
        });

        // Create the fork in branch manager
        self.branch_manager
            .fork(&current, new_branch.clone(), base_turn.clone())
            .map_err(|e| RuntimeError::Branch(e))?;

        // Create journal and snapshot directories for new branch
        let new_journal_dir = self.storage.branch_journal_dir(&new_branch);
        let new_snapshot_dir = self.storage.branch_snapshot_dir(&new_branch);
        std::fs::create_dir_all(&new_journal_dir).map_err(|e| {
            RuntimeError::Init(format!("Failed to create branch journal dir: {}", e))
        })?;
        std::fs::create_dir_all(&new_snapshot_dir).map_err(|e| {
            RuntimeError::Init(format!("Failed to create branch snapshot dir: {}", e))
        })?;

        self.persist_branch_state()?;

        Ok(new_branch)
    }

    /// Switch to a different branch
    pub fn switch_branch(&mut self, branch: BranchId) -> Result<()> {
        // Verify branch exists
        self.branch_manager
            .switch_branch(branch.clone())
            .map_err(|e| RuntimeError::Branch(e))?;

        // Update runtime state
        self.current_branch = branch.clone();

        // Reinitialize journal writer for new branch
        let journal_reader = JournalReader::new(self.storage.clone(), branch.clone())
            .unwrap_or_else(|_| JournalReader::new_empty(self.storage.clone(), branch.clone()));

        journal_reader
            .validate_and_repair()
            .map_err(|e| RuntimeError::Init(format!("Journal validation failed: {}", e)))?;

        let clean_index = journal_reader
            .rebuild_index()
            .map_err(|e| RuntimeError::Init(format!("Index rebuild failed: {}", e)))?;

        let index_path = self.storage.branch_meta_dir(&branch).join("journal.index");
        std::fs::create_dir_all(self.storage.branch_meta_dir(&branch))
            .map_err(|e| RuntimeError::Init(format!("Failed to create meta dir: {}", e)))?;
        clean_index
            .save(&index_path)
            .map_err(|e| RuntimeError::Init(format!("Failed to save index: {}", e)))?;

        self.journal_writer =
            JournalWriter::new_with_index(self.storage.clone(), branch.clone(), clean_index)
                .map_err(|e| {
                    RuntimeError::Init(format!("Failed to create journal writer: {}", e))
                })?;

        self.persist_branch_state()?;

        Ok(())
    }

    /// Go to a specific turn (time travel)
    ///
    /// Loads the nearest snapshot before the target turn, then replays
    /// journal entries up to the target.
    pub fn goto(&mut self, target_turn: TurnId) -> Result<()> {
        // Find nearest snapshot at or before target turn
        let snapshot_turn = self
            .snapshot_manager
            .nearest_snapshot(&self.current_branch, &target_turn)
            .map_err(|e| RuntimeError::Snapshot(e))?;

        // Reset runtime state
        self.actors.clear();
        self.scheduler = Scheduler::new(self.config.flow_control_limit as i64);
        self.turn_count = 0;
        self.last_turn_per_actor.clear();

        let start_turn_id = if let Some(snap_turn) = snapshot_turn.clone() {
            let snapshot = self
                .snapshot_manager
                .load(&self.current_branch, &snap_turn)
                .map_err(|e| RuntimeError::Snapshot(e))?;

            // Restore state from snapshot by recreating actors with snapshot data
            // For now, we'll replay from the beginning or snapshot point
            self.turn_count = snapshot.metadata.turn_count;

            // We'd need to reconstruct actors from the snapshot state
            // This is complex as it requires recreating actor objects with the right state
            // For the initial implementation, we'll just track the turn_count
            // and replay from the snapshot point

            snap_turn
        } else {
            // No snapshot, replay from the beginning
            TurnId::new("turn_00000000".to_string())
        };

        // Replay journal from snapshot point to target
        let journal_reader = JournalReader::new(self.storage.clone(), self.current_branch.clone())
            .map_err(|e| RuntimeError::Journal(e))?;

        // Iterate through all turns and replay them
        let mut iter = journal_reader.iter_all().map_err(|e| RuntimeError::Journal(e))?;

        while let Some(result) = iter.next() {
            let record = result.map_err(|e| RuntimeError::Journal(e))?;

            // Stop when we reach the target turn
            if record.turn_id == target_turn {
                self.turn_count += 1;
                self.last_turn_per_actor.insert(record.actor.clone(), record.turn_id.clone());
                break;
            }

            // Skip turns before the snapshot
            if start_turn_id != TurnId::new("turn_00000000".to_string())
                && record.turn_id <= start_turn_id {
                continue;
            }

            // Apply the turn's state delta to runtime
            // Get or create actor
            let _actor = self.actors.entry(record.actor.clone())
                .or_insert_with(|| Actor::new(record.actor.clone()));

            // Apply state delta (simplified - in full implementation would apply all changes)
            // For now, we just track that this actor exists and the turn was processed

            self.turn_count += 1;
            self.last_turn_per_actor.insert(record.actor.clone(), record.turn_id.clone());

            // Re-enqueue any pending outputs as inputs for future turns
            // This is a simplified replay - full implementation would be more sophisticated
        }

        // Update branch head
        self.branch_manager
            .update_head(&self.current_branch, target_turn)
            .map_err(|e| RuntimeError::Branch(e))?;

        Ok(())
    }

    /// Rewind by N turns
    pub fn back(&mut self, count: usize) -> Result<TurnId> {
        // Get current head
        let current_head = self.branch_manager
            .head(&self.current_branch)
            .cloned()
            .ok_or_else(|| RuntimeError::Branch(
                error::BranchError::NotFound("No head turn found".into())
            ))?;

        // Read journal to find the turn N steps back
        let journal_reader = JournalReader::new(self.storage.clone(), self.current_branch.clone())
            .map_err(|e| RuntimeError::Journal(e))?;

        let mut turns = Vec::new();
        let mut iter = journal_reader.iter_all().map_err(|e| RuntimeError::Journal(e))?;

        while let Some(result) = iter.next() {
            let record = result.map_err(|e| RuntimeError::Journal(e))?;
            turns.push(record.turn_id.clone());

            if record.turn_id == current_head {
                break;
            }
        }

        // Calculate target turn (go back N turns)
        if count >= turns.len() {
            // Go to the beginning
            if let Some(first_turn) = turns.first() {
                self.goto(first_turn.clone())?;
                Ok(first_turn.clone())
            } else {
                Err(RuntimeError::Journal(
                    error::JournalError::TurnNotFound("No turns in journal".into())
                ))
            }
        } else {
            let target_idx = turns.len() - count - 1;
            let target_turn = turns[target_idx].clone();
            self.goto(target_turn.clone())?;
            Ok(target_turn)
        }
    }

    /// Initialize runtime storage directories and metadata
    pub fn init(config: RuntimeConfig) -> Result<()> {
        storage::init_storage(&config.root)
            .map_err(|e| RuntimeError::Init(format!("Failed to initialize storage: {}", e)))?;
        storage::write_config(&config)
            .map_err(|e| RuntimeError::Config(format!("Failed to write config: {}", e)))?;

        let storage = Storage::new(config.root.clone());
        let branch_state = BranchManager::default_state();
        storage::save_branch_state(&storage, &branch_state)
            .map_err(|e| RuntimeError::Config(format!("Failed to write branch state: {}", e)))?;
        Ok(())
    }

    /// Load an existing runtime from storage
    pub fn load(root: PathBuf) -> Result<Self> {
        let config = storage::load_config(&root)
            .map_err(|e| RuntimeError::Config(format!("Failed to load config: {}", e)))?;
        Self::new(config)
    }

    fn persist_branch_state(&self) -> Result<()> {
        let state = self.branch_manager.state();
        storage::save_branch_state(&self.storage, &state)
            .map_err(|e| RuntimeError::Config(format!("Failed to persist branch state: {}", e)))
    }

    /// Get the current configuration
    pub fn config(&self) -> &RuntimeConfig {
        &self.config
    }

    /// Get the current branch
    pub fn current_branch(&self) -> BranchId {
        self.current_branch.clone()
    }

    /// Get the storage manager
    pub fn storage(&self) -> &Storage {
        &self.storage
    }

    /// Get reference to scheduler
    pub fn scheduler(&self) -> &Scheduler {
        &self.scheduler
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

    /// Get reference to branch manager
    pub fn branch_manager(&self) -> &BranchManager {
        &self.branch_manager
    }

    /// Get mutable access to the branch manager
    pub fn branch_manager_mut(&mut self) -> &mut BranchManager {
        &mut self.branch_manager
    }

    /// Create a journal reader for a specific branch
    pub fn journal_reader(&self, branch: &BranchId) -> Result<JournalReader> {
        JournalReader::new(self.storage.clone(), branch.clone())
            .map_err(|e| RuntimeError::Journal(e))
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
