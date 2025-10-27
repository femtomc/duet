//! Runtime orchestrator and public API
//!
//! This module provides the main `Runtime` struct that coordinates all subsystems
//! and exposes the public interface for embedding or controlling the runtime.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use uuid::Uuid;

// Submodules
pub mod actor;
pub mod branch;
pub mod control;
pub mod error;
pub mod journal;
pub mod pattern;
pub mod registry;
pub mod scheduler;
pub mod schema;
pub mod service;
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
use registry::EntityManager;
use std::collections::{HashMap, HashSet};

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

    /// Entity metadata manager
    entity_manager: EntityManager,

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
        crate::codebase::register_codebase_entities();
        
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

        // Load entity metadata
        let entity_meta_path = storage.meta_dir().join("entities.json");
        let entity_manager =
            EntityManager::load(&entity_meta_path).unwrap_or_else(|_| EntityManager::new());

        let mut runtime = Self {
            config,
            storage,
            scheduler,
            journal_writer,
            snapshot_manager,
            branch_manager,
            current_branch,
            actors: HashMap::new(),
            entity_manager,
            turn_count: 0,
            last_turn_per_actor: HashMap::new(),
        };

        // Hydrate entities: recreate and attach them from metadata
        runtime.hydrate_entities(None)?;

        Ok(runtime)
    }

    /// Hydrate entities from persisted metadata
    ///
    /// Recreates entity instances using the global registry, attaches them to
    /// actors/facets, and re-registers pattern subscriptions.
    fn hydrate_entities(
        &mut self,
        entity_states: Option<&HashMap<uuid::Uuid, snapshot::EntityStateSnapshot>>,
    ) -> Result<()> {
        use registry::EntityRegistry;

        let registry = EntityRegistry::global();

        // Clone metadata to avoid borrow conflicts
        let entities: Vec<_> = self.entity_manager.list().into_iter().cloned().collect();

        for metadata in entities {
            // Create entity instance using registry
            let mut entity = registry
                .create(&metadata.entity_type, &metadata.config)
                .map_err(|e| RuntimeError::Actor(e))?;

            // Restore private state if available
            if let Some(state_map) = entity_states {
                if let Some(state) = state_map.get(&metadata.id) {
                    let _ = registry.restore_entity(
                        &metadata.entity_type,
                        entity.as_mut(),
                        &state.state,
                    )?;
                }
            }

            // Get or create actor
            let actor = self
                .actors
                .entry(metadata.actor.clone())
                .or_insert_with(|| Actor::new(metadata.actor.clone()));

            // Attach entity to facet
            actor.attach_entity(
                metadata.id,
                metadata.entity_type.clone(),
                metadata.facet.clone(),
                entity,
            );

            // Re-register patterns
            for pattern in &metadata.patterns {
                actor.register_pattern(pattern.clone());
            }
        }

        Ok(())
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

        // Get the actual turn ID of the last executed turn
        // Use the most recent turn ID from any actor, or generate a placeholder
        let turn_id = self
            .last_turn_per_actor
            .values()
            .max()
            .cloned()
            .unwrap_or_else(|| TurnId::new(format!("turn_{:08}", self.turn_count)));

        // Capture entity private state (for HydratableEntity implementations)
        let registry = registry::EntityRegistry::global();
        let mut entity_states = Vec::new();

        for (actor_id, actor) in self.actors.iter() {
            let entities = actor.entities.read();
            for (facet_id, entries) in entities.iter() {
                for entry in entries.iter() {
                    if let Some(state) =
                        registry.snapshot_entity(&entry.entity_type, entry.entity.as_ref())
                    {
                        entity_states.push(snapshot::EntityStateSnapshot {
                            entity_id: entry.id,
                            actor: actor_id.clone(),
                            facet: facet_id.clone(),
                            entity_type: entry.entity_type.clone(),
                            state,
                        });
                    }
                }
            }
        }

        let snapshot = RuntimeSnapshot {
            branch: self.current_branch.clone(),
            turn_id: turn_id.clone(),
            assertions: all_assertions,
            facets: all_facets,
            capabilities: all_capabilities,
            entity_states,
            metadata: snapshot::SnapshotMetadata {
                created_at: chrono::Utc::now(),
                turn_count: self.turn_count,
                turn_id,
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

        let mut entity_state_map: HashMap<uuid::Uuid, snapshot::EntityStateSnapshot> =
            HashMap::new();

        // Reset runtime state
        self.actors.clear();
        self.scheduler = Scheduler::new(self.config.flow_control_limit as i64);
        self.turn_count = 0;
        self.last_turn_per_actor.clear();

        let start_turn_id = if let Some(snap_count) = snapshot_turn {
            let snapshot = self
                .snapshot_manager
                .load_by_count(&self.current_branch, snap_count)
                .map_err(|e| RuntimeError::Snapshot(e))?;

            // Restore state from snapshot
            self.turn_count = snapshot.metadata.turn_count;

            if !snapshot.entity_states.is_empty() {
                entity_state_map = snapshot
                    .entity_states
                    .iter()
                    .cloned()
                    .map(|state| (state.entity_id, state))
                    .collect();
            }

            // The snapshot contains aggregate state for all actors
            // We need to reconstruct actors and apply the snapshot state
            // For now, we'll collect the unique actors from the snapshot data

            // Find all actors mentioned in assertions
            let mut actor_ids = HashSet::new();
            for (actor_id, _handle) in snapshot.assertions.active.keys() {
                actor_ids.insert(actor_id.clone());
            }
            for facet_meta in snapshot.facets.facets.values() {
                actor_ids.insert(facet_meta.actor.clone());
            }

            // Recreate actors and apply snapshot state
            for actor_id in actor_ids {
                let actor = Actor::new(actor_id.clone());

                // Apply assertions for this actor
                {
                    let mut assertions = actor.assertions.write();
                    *assertions = snapshot.assertions.clone();
                }
                // Apply facets
                {
                    let mut facets = actor.facets.write();
                    *facets = snapshot.facets.clone();
                }
                // Apply capabilities
                {
                    let mut capabilities = actor.capabilities.write();
                    *capabilities = snapshot.capabilities.clone();
                }

                self.actors.insert(actor_id, actor);
            }

            snapshot.metadata.turn_id.clone()
        } else {
            // No snapshot, replay from the beginning
            TurnId::new("turn_00000000".to_string())
        };

        // Replay journal from snapshot point to target
        let journal_reader = JournalReader::new(self.storage.clone(), self.current_branch.clone())
            .map_err(|e| RuntimeError::Journal(e))?;

        // Iterate through all turns and replay them
        let mut iter = journal_reader
            .iter_all()
            .map_err(|e| RuntimeError::Journal(e))?;

        while let Some(result) = iter.next() {
            let record = result.map_err(|e| RuntimeError::Journal(e))?;

            // Stop when we reach the target turn
            // Skip turns before the snapshot
            if (!start_turn_id.as_str().eq("turn_00000000")) && record.turn_id <= start_turn_id {
                // Even if this is the target turn, we shouldn't apply it yet.
                if record.turn_id == target_turn {
                    break;
                }
                continue;
            }

            // Apply the turn's state delta to runtime
            let actor = self
                .actors
                .entry(record.actor.clone())
                .or_insert_with(|| Actor::new(record.actor.clone()));

            // Apply state delta to actor state
            {
                let mut assertions = actor.assertions.write();
                assertions.apply(&record.delta.assertions);
            }
            {
                let mut facets = actor.facets.write();
                facets.apply(&record.delta.facets);
            }
            {
                let mut capabilities = actor.capabilities.write();
                capabilities.apply(&record.delta.capabilities);
            }
            {
                let mut account = actor.account.write();
                account.apply(&record.delta.accounts);
            }

            self.turn_count += 1;
            self.last_turn_per_actor
                .insert(record.actor.clone(), record.turn_id.clone());

            if record.turn_id == target_turn {
                break;
            }
        }

        // Rehydrate entities (attach + patterns) after replay
        let state_map_opt = if entity_state_map.is_empty() {
            None
        } else {
            Some(&entity_state_map)
        };

        self.hydrate_entities(state_map_opt)?;

        // Update branch head
        self.branch_manager
            .update_head(&self.current_branch, target_turn)
            .map_err(|e| RuntimeError::Branch(e))?;

        Ok(())
    }

    /// Merge source branch into target branch
    ///
    /// Following the implementation guide:
    /// 1. Find LCA turn T where branches diverged
    /// 2. Load state at T (from snapshot if available)
    /// 3. Replay from T to get state_source and state_target
    /// 4. Join states using CRDT semantics
    /// 5. Create synthetic merge turn with the joined delta
    pub fn merge(&mut self, source: &BranchId, target: &BranchId) -> Result<branch::MergeResult> {
        // Find the lowest common ancestor
        let lca_turn = self
            .branch_manager
            .find_lca(source, target)
            .ok_or_else(|| {
                RuntimeError::Branch(error::BranchError::InvalidForkPoint(
                    "No common ancestor found".into(),
                ))
            })?;

        // Get the head turns for both branches
        let source_head =
            self.branch_manager.head(source).cloned().ok_or_else(|| {
                RuntimeError::Branch(error::BranchError::NotFound(source.0.clone()))
            })?;

        let target_head =
            self.branch_manager.head(target).cloned().ok_or_else(|| {
                RuntimeError::Branch(error::BranchError::NotFound(target.0.clone()))
            })?;

        // Load state at LCA by replaying up to that turn
        let lca_state = self.load_state_at_turn(&lca_turn, source)?;

        // Load state at source head
        let source_state = self.load_state_at_turn(&source_head, source)?;

        // Load state at target head
        let target_state = self.load_state_at_turn(&target_head, target)?;

        // Compute the delta from LCA to source
        let source_delta = self.compute_delta(&lca_state, &source_state);

        // Compute the delta from LCA to target
        let target_delta = self.compute_delta(&lca_state, &target_state);

        // Join the deltas using CRDT semantics
        let joined_delta = source_delta.join(&target_delta);

        // Detect conflicts and generate warnings
        let warnings = self.detect_conflicts(&source_delta, &target_delta, &joined_delta);

        // Create a synthetic merge turn with provenance metadata
        let merge_input = turn::TurnInput::Merge {
            source_branch: source.clone(),
            target_branch: target.clone(),
            lca_turn: lca_turn.clone(),
        };

        // Use a special "merge" actor ID (deterministic)
        let merge_actor = turn::ActorId::from_uuid(Uuid::nil());
        let merge_clock = turn::LogicalClock::zero();

        let merge_record = turn::TurnRecord::new(
            merge_actor,
            target.clone(),
            merge_clock,
            Some(target_head),
            vec![merge_input],
            vec![], // No outputs for synthetic merge turn
            joined_delta,
        );

        let merge_turn_id = merge_record.turn_id.clone();

        // Record merge turn in journal
        self.journal_writer
            .append(&merge_record)
            .map_err(|e| RuntimeError::Journal(e))?;

        // Update branch metadata
        self.branch_manager
            .update_head(target, merge_turn_id.clone())
            .map_err(|e| RuntimeError::Branch(e))?;

        self.persist_branch_state()?;

        Ok(branch::MergeResult {
            merge_turn: merge_turn_id,
            warnings,
        })
    }

    /// Load complete state at a specific turn by replaying journal
    ///
    /// Accumulates all state deltas from the beginning up to (and including) the target turn.
    fn load_state_at_turn(&self, turn_id: &TurnId, branch: &BranchId) -> Result<state::StateDelta> {
        let journal_reader = JournalReader::new(self.storage.clone(), branch.clone())
            .map_err(|e| RuntimeError::Journal(e))?;

        let mut accumulated_delta = state::StateDelta::empty();
        let mut iter = journal_reader
            .iter_all()
            .map_err(|e| RuntimeError::Journal(e))?;

        while let Some(result) = iter.next() {
            let record = result.map_err(|e| RuntimeError::Journal(e))?;

            // Accumulate this turn's delta
            accumulated_delta = accumulated_delta.join(&record.delta);

            // Stop when we reach the target turn
            if record.turn_id == *turn_id {
                break;
            }
        }

        Ok(accumulated_delta)
    }

    /// Compute the delta between two states
    ///
    /// Returns a delta representing the changes from base to head.
    /// This is a simplified version - full implementation would compute precise diffs.
    fn compute_delta(
        &self,
        _base: &state::StateDelta,
        head: &state::StateDelta,
    ) -> state::StateDelta {
        // For merges, we actually want to use the head state directly
        // as the "delta from LCA" since head already represents all changes since LCA
        // when we accumulate deltas during replay.
        //
        // A more sophisticated implementation would:
        // 1. Convert deltas to full state sets
        // 2. Compute set differences
        // 3. Return the minimal delta
        //
        // For now, we'll just return the head state as-is
        head.clone()
    }

    /// Detect conflicts between two deltas
    fn detect_conflicts(
        &self,
        source: &state::StateDelta,
        target: &state::StateDelta,
        _joined: &state::StateDelta,
    ) -> Vec<branch::MergeWarning> {
        let mut warnings = Vec::new();

        // Check for concurrent assertions with different values on same handle
        let mut source_handles = HashSet::new();
        for (actor, handle, _value, _version) in &source.assertions.added {
            source_handles.insert((actor.clone(), handle.clone()));
        }

        for (actor, handle, value, _version) in &target.assertions.added {
            if source_handles.contains(&(actor.clone(), handle.clone())) {
                // Find the source value for comparison
                if let Some(source_item) = source
                    .assertions
                    .added
                    .iter()
                    .find(|(a, h, _, _)| a == actor && h == handle)
                {
                    if &source_item.2 != value {
                        warnings.push(branch::MergeWarning {
                            category: "concurrent-assertion".into(),
                            message: format!(
                                "Concurrent different-valued assertions on handle {}",
                                handle.0
                            ),
                            affected: vec![format!("{}:{}", actor.0, handle.0)],
                        });
                    }
                }
            }
        }

        // Check for concurrent facet terminations
        let mut source_terminated = HashSet::new();
        for facet_id in &source.facets.terminated {
            source_terminated.insert(facet_id.clone());
        }

        for facet_id in &target.facets.terminated {
            if source_terminated.contains(facet_id) {
                warnings.push(branch::MergeWarning {
                    category: "concurrent-termination".into(),
                    message: format!("Facet {} terminated in both branches", facet_id.0),
                    affected: vec![facet_id.0.to_string()],
                });
            }
        }

        warnings
    }

    /// Rewind by N turns
    pub fn back(&mut self, count: usize) -> Result<TurnId> {
        // Get current head
        let current_head = self
            .branch_manager
            .head(&self.current_branch)
            .cloned()
            .ok_or_else(|| {
                RuntimeError::Branch(error::BranchError::NotFound("No head turn found".into()))
            })?;

        // Read journal to find the turn N steps back
        let journal_reader = JournalReader::new(self.storage.clone(), self.current_branch.clone())
            .map_err(|e| RuntimeError::Journal(e))?;

        let mut turns = Vec::new();
        let mut iter = journal_reader
            .iter_all()
            .map_err(|e| RuntimeError::Journal(e))?;

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
                Err(RuntimeError::Journal(error::JournalError::TurnNotFound(
                    "No turns in journal".into(),
                )))
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

    /// Get reference to entity manager
    pub fn entity_manager(&self) -> &EntityManager {
        &self.entity_manager
    }

    /// Get mutable reference to entity manager
    pub fn entity_manager_mut(&mut self) -> &mut EntityManager {
        &mut self.entity_manager
    }

    /// Persist entity metadata to disk (atomic write)
    pub fn persist_entities(&self) -> Result<()> {
        let entity_meta_path = self.storage.meta_dir().join("entities.json");
        self.entity_manager.save(&self.storage, &entity_meta_path)
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
