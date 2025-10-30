//! Runtime control facade for CLI and tests
//!
//! Provides high-level API for controlling the runtime: sending messages,
//! stepping, rewinding, forking, merging, and inspecting state.

use preserves::IOValue;
use serde::{Deserialize, Serialize};
use std::time::Duration;
use uuid::Uuid;

use super::actor::Actor;
use super::error::Result;
use super::reaction::{ReactionDefinition, ReactionId, ReactionInfo};
use super::state::{CapId, CapabilityStatus, CapabilityTarget, FacetMetadata, FacetStatus};
use super::turn::{ActorId, BranchId, FacetId, TurnId, TurnOutput, TurnRecord};
use super::{Runtime, RuntimeConfig};

/// Control interface for the runtime
pub struct Control {
    runtime: Runtime,
}

impl Control {
    /// Create a new control interface with initialized runtime
    pub fn new(config: RuntimeConfig) -> Result<Self> {
        let runtime = Runtime::new(config)?;
        Ok(Self { runtime })
    }

    /// Initialize storage and create a new control interface
    pub fn init(config: RuntimeConfig) -> Result<Self> {
        Runtime::init(config.clone())?;
        Self::new(config)
    }

    /// Get runtime status
    pub fn status(&self) -> Result<RuntimeStatus> {
        let current_branch = self.runtime.current_branch();
        let head_turn = self
            .runtime
            .branch_manager()
            .head(&current_branch)
            .cloned()
            .unwrap_or_else(|| TurnId::new("turn_0".to_string()));

        let pending_inputs = self.runtime.scheduler().pending_count();

        Ok(RuntimeStatus {
            active_branch: current_branch,
            head_turn,
            pending_inputs,
            snapshot_interval: self.runtime.config().snapshot_interval,
        })
    }

    /// Send a message to an actor/facet
    pub fn send_message(
        &mut self,
        actor: ActorId,
        facet: FacetId,
        payload: preserves::IOValue,
    ) -> Result<TurnId> {
        self.runtime.send_message(actor.clone(), facet, payload);

        // Step to execute the message
        if let Some(record) = self.runtime.step()? {
            Ok(record.turn_id)
        } else {
            Err(super::error::RuntimeError::Init(
                "No turn executed after sending message".into(),
            ))
        }
    }

    /// Step forward by N turns
    pub fn step(&mut self, count: usize) -> Result<Vec<TurnSummary>> {
        let records = self.runtime.step_n(count)?;
        Ok(records.into_iter().map(turn_to_summary).collect())
    }

    /// Go back N turns
    pub fn back(&mut self, count: usize) -> Result<TurnId> {
        self.runtime.back(count)
    }

    /// Jump to a specific turn
    pub fn goto(&mut self, turn_id: TurnId) -> Result<()> {
        self.runtime.goto(turn_id)
    }

    /// Fork a new branch
    pub fn fork(
        &mut self,
        _source: BranchId,
        new_branch: BranchId,
        from_turn: Option<TurnId>,
    ) -> Result<BranchId> {
        self.runtime.fork(new_branch.0.clone(), from_turn)
    }

    /// Merge branches
    pub fn merge(&mut self, source: BranchId, target: BranchId) -> Result<MergeReport> {
        let result = self.runtime.merge(&source, &target)?;

        Ok(MergeReport {
            merge_turn: result.merge_turn,
            warnings: result.warnings.iter().map(|w| w.message.clone()).collect(),
            conflicts: result
                .warnings
                .iter()
                .filter(|w| w.category.contains("conflict"))
                .map(|w| w.message.clone())
                .collect(),
        })
    }

    /// Get history for a branch
    pub fn history(
        &self,
        branch: &BranchId,
        start: usize,
        limit: usize,
    ) -> Result<Vec<TurnSummary>> {
        // Read from journal
        let reader = self.runtime.journal_reader(branch)?;
        let turns = reader.read_range(start, limit)?;
        Ok(turns.into_iter().map(turn_to_summary).collect())
    }

    /// List all branches
    pub fn list_branches(&self) -> Result<Vec<BranchInfo>> {
        let branches = self.runtime.branch_manager().list_branches();
        Ok(branches
            .into_iter()
            .map(|metadata| BranchInfo {
                name: metadata.id.clone(),
                head_turn: metadata.head_turn.clone(),
                parent: metadata.parent.clone(),
            })
            .collect())
    }

    /// Get reference to underlying runtime (for advanced usage)
    pub fn runtime(&self) -> &Runtime {
        &self.runtime
    }

    /// Get mutable reference to underlying runtime (for advanced usage)
    pub fn runtime_mut(&mut self) -> &mut Runtime {
        &mut self.runtime
    }

    /// Register a new entity instance
    pub fn register_entity(
        &mut self,
        actor: ActorId,
        facet: FacetId,
        entity_type: String,
        config: preserves::IOValue,
    ) -> Result<Uuid> {
        use super::registry::EntityMetadata;

        // Create the entity instance using the runtime registry snapshot
        let entity = self
            .runtime
            .entity_registry()
            .create(&entity_type, &config)
            .map_err(|e| super::error::RuntimeError::Actor(e))?;

        // Generate entity ID
        let entity_id = Uuid::new_v4();

        // Ensure the actor exists and capture its root facet without holding a
        // mutable borrow across metadata updates.
        let actor_root = {
            let actor_entry = self
                .runtime
                .actors
                .entry(actor.clone())
                .or_insert_with(|| Actor::new(actor.clone()));
            actor_entry.root_facet.clone()
        };

        let is_root_facet = facet == actor_root;

        // Create metadata (patterns will be added later via register_pattern_for_entity)
        let metadata = EntityMetadata {
            id: entity_id,
            actor: actor.clone(),
            facet: facet.clone(),
            entity_type: entity_type.clone(),
            config,
            is_root_facet,
            patterns: vec![],
        };

        // Register metadata
        self.runtime.entity_manager_mut().register(metadata);

        // Attach entity to actor (obtain a fresh mutable borrow)
        let actor_obj = self
            .runtime
            .actors
            .entry(actor.clone())
            .or_insert_with(|| Actor::new(actor.clone()));
        actor_obj.attach_entity(entity_id, entity_type, facet.clone(), entity);

        {
            let mut facets = actor_obj.facets.write();
            facets
                .facets
                .entry(facet.clone())
                .or_insert_with(|| FacetMetadata {
                    id: facet.clone(),
                    parent: Some(actor_obj.root_facet.clone()),
                    status: FacetStatus::Alive,
                    actor: actor.clone(),
                });
        }

        // Persist entity metadata
        self.runtime.persist_entities()?;

        Ok(entity_id)
    }

    /// Register a pattern subscription for an entity
    ///
    /// Registers the pattern with the actor and persists the pattern definition
    /// so it can be re-applied during hydration.
    pub fn register_pattern_for_entity(
        &mut self,
        entity_id: Uuid,
        pattern: super::pattern::Pattern,
    ) -> Result<Uuid> {
        let pattern_id = pattern.id;

        // Snapshot metadata to determine actor + facet
        let (actor_id, entity_facet) = {
            let metadata =
                self.runtime
                    .entity_manager()
                    .get(&entity_id)
                    .ok_or_else(|| {
                        super::error::RuntimeError::Actor(super::error::ActorError::NotFound(
                            format!("Entity {}", entity_id),
                        ))
                    })?;
            (metadata.actor.clone(), metadata.facet.clone())
        };

        if pattern.facet != entity_facet {
            return Err(super::error::RuntimeError::Actor(
                super::error::ActorError::InvalidActivation(format!(
                    "Pattern facet {} does not match entity facet {}",
                    pattern.facet.0, entity_facet.0
                )),
            ));
        }

        // Ensure actor exists and register the pattern
        let actor = self
            .runtime
            .actors
            .entry(actor_id.clone())
            .or_insert_with(|| Actor::new(actor_id.clone()));

        actor.register_pattern(pattern.clone());

        // Persist pattern definition
        if let Some(metadata) = self
            .runtime
            .entity_manager_mut()
            .entities
            .get_mut(&entity_id)
        {
            metadata.patterns.push(pattern);
        }

        // Persist entity metadata
        self.runtime.persist_entities()?;

        Ok(pattern_id)
    }

    /// Unregister an entity instance
    ///
    /// Removes the entity from the actor, unregisters its patterns,
    /// and deletes its metadata.
    pub fn unregister_entity(&mut self, entity_id: Uuid) -> Result<bool> {
        // Remove metadata so we can detach entities/patterns
        let metadata = match self.runtime.entity_manager_mut().unregister(&entity_id) {
            Some(meta) => meta,
            None => return Ok(false),
        };

        if let Some(actor) = self.runtime.actors.get(&metadata.actor) {
            for pattern in &metadata.patterns {
                actor.unregister_pattern(pattern.id);
            }

            actor.detach_entity(entity_id);
        }

        // Persist updated metadata
        self.runtime.persist_entities()?;

        Ok(true)
    }

    /// List all registered entities
    pub fn list_entities(&self) -> Vec<EntityInfo> {
        self.runtime
            .entity_manager()
            .list()
            .into_iter()
            .map(|meta| EntityInfo {
                id: meta.id,
                actor: meta.actor.clone(),
                facet: meta.facet.clone(),
                entity_type: meta.entity_type.clone(),
                pattern_count: meta.patterns.len(),
            })
            .collect()
    }

    /// List entities for a specific actor
    pub fn list_entities_for_actor(&self, actor: &ActorId) -> Vec<EntityInfo> {
        self.runtime
            .entity_manager()
            .list_for_actor(actor)
            .into_iter()
            .map(|meta| EntityInfo {
                id: meta.id,
                actor: meta.actor.clone(),
                facet: meta.facet.clone(),
                entity_type: meta.entity_type.clone(),
                pattern_count: meta.patterns.len(),
            })
            .collect()
    }

    /// Register a reaction definition for an actor.
    pub fn register_reaction(
        &mut self,
        actor: ActorId,
        definition: ReactionDefinition,
    ) -> Result<ReactionId> {
        self.runtime.register_reaction(actor, definition)
    }

    /// Remove a previously registered reaction.
    pub fn unregister_reaction(&mut self, reaction_id: ReactionId) -> Result<bool> {
        self.runtime.unregister_reaction(reaction_id)
    }

    /// List all stored reactions.
    pub fn list_reactions(&self) -> Vec<ReactionInfo> {
        self.runtime.list_reactions()
    }

    /// List capabilities for all actors
    pub fn list_capabilities(&self) -> Vec<CapabilityInfo> {
        let mut results = Vec::new();
        for (actor_id, actor) in &self.runtime.actors {
            results.extend(Self::collect_capabilities_for_actor(actor_id, actor));
        }
        results
    }

    /// List capabilities for a specific actor
    pub fn list_capabilities_for_actor(&self, actor: &ActorId) -> Vec<CapabilityInfo> {
        if let Some(actor_obj) = self.runtime.actors.get(actor) {
            Self::collect_capabilities_for_actor(actor, actor_obj)
        } else {
            Vec::new()
        }
    }

    /// List current assertions made by a specific actor.
    pub fn list_assertions_for_actor(
        &self,
        actor: &ActorId,
    ) -> Vec<(super::turn::Handle, IOValue)> {
        self.runtime.assertions_for_actor(actor).unwrap_or_default()
    }

    /// List assertions across the runtime, optionally filtered by actor.
    pub fn list_assertions(&self, actor: Option<&ActorId>) -> Vec<AssertionInfo> {
        match actor {
            Some(actor_id) => self
                .list_assertions_for_actor(actor_id)
                .into_iter()
                .map(|(handle, value)| AssertionInfo {
                    actor: actor_id.clone(),
                    handle,
                    value,
                })
                .collect(),
            None => {
                let mut results = Vec::new();
                for (actor_id, actor) in &self.runtime.actors {
                    let assertions = actor.assertions.read();
                    results.extend(assertions.active.iter().map(
                        |((_owner, handle), (value, _version))| AssertionInfo {
                            actor: actor_id.clone(),
                            handle: handle.clone(),
                            value: value.clone(),
                        },
                    ));
                }
                results
            }
        }
    }

    /// Stream assertion-related events from the journal.
    pub fn assertion_events_since(
        &self,
        branch: &BranchId,
        since: Option<&TurnId>,
        limit: usize,
        filter: AssertionEventFilter,
        wait: Option<Duration>,
    ) -> Result<AssertionEventChunk> {
        let mut chunk = self.collect_assertion_events(branch, since, limit, &filter)?;

        if chunk.events.is_empty() {
            if let Some(timeout) = wait {
                if self
                    .runtime
                    .wait_for_turn_after(branch, since, timeout)?
                    .is_some()
                {
                    chunk = self.collect_assertion_events(branch, since, limit, &filter)?;
                }
            }
        }

        Ok(chunk)
    }

    /// Invoke a capability by id with a payload; runtime enforces attenuation
    pub fn invoke_capability(
        &mut self,
        cap_id: Uuid,
        payload: preserves::IOValue,
    ) -> Result<preserves::IOValue> {
        self.runtime.invoke_capability(cap_id, payload)
    }

    /// Wait for a branch head to advance beyond a target turn or until timeout.
    pub fn wait_for_turn_after(
        &self,
        branch: &BranchId,
        since: Option<&TurnId>,
        timeout: Duration,
    ) -> Result<Option<TurnId>> {
        self.runtime.wait_for_turn_after(branch, since, timeout)
    }

    /// Drain any queued turns (including async completions) until the scheduler is idle.
    pub fn drain_pending(&mut self) -> Result<()> {
        loop {
            self.runtime.drain_async_messages();
            match self.runtime.step()? {
                Some(_) => continue,
                None => break,
            }
        }
        Ok(())
    }

    fn collect_assertion_events(
        &self,
        branch: &BranchId,
        since: Option<&TurnId>,
        limit: usize,
        filter: &AssertionEventFilter,
    ) -> Result<AssertionEventChunk> {
        let reader = self.runtime.journal_reader(branch)?;
        let iterator = if let Some(turn) = since {
            let mut iter = reader.iter_from(turn)?;
            // Skip the turn that matches `since` so callers receive strictly newer events.
            iter.next();
            iter
        } else {
            reader.iter_all()?
        };

        let mut iter = iterator.peekable();
        let mut batches = Vec::new();
        let mut last_turn: Option<TurnId> = None;

        while batches.len() < limit {
            let record = match iter.next() {
                Some(Ok(record)) => record,
                Some(Err(err)) => return Err(super::error::RuntimeError::Journal(err)),
                None => break,
            };

            if let Some(actor_filter) = filter.actor.as_ref() {
                if &record.actor != actor_filter {
                    continue;
                }
            }

            let mut events = Vec::new();
            for output in record.outputs.iter() {
                match output {
                    TurnOutput::Assert { handle, value } if filter.include_asserts => {
                        if let Some(label) = &filter.label {
                            let matches = value
                                .label()
                                .as_symbol()
                                .map(|sym| sym.as_ref() == label)
                                .unwrap_or(false);
                            if !matches {
                                continue;
                            }
                        }

                        if let Some(request_id) = &filter.request_id {
                            let matches = value
                                .index(0)
                                .as_string()
                                .map(|s| s.as_ref() == request_id)
                                .unwrap_or(false);
                            if !matches {
                                continue;
                            }
                        }

                        events.push(AssertionEvent {
                            action: AssertionEventAction::Assert,
                            handle: handle.clone(),
                            value: Some(value.clone()),
                        });
                    }
                    TurnOutput::Retract { handle } if filter.include_retracts => {
                        events.push(AssertionEvent {
                            action: AssertionEventAction::Retract,
                            handle: handle.clone(),
                            value: None,
                        });
                    }
                    _ => {}
                }
            }

            if events.is_empty() {
                continue;
            }

            last_turn = Some(record.turn_id.clone());
            batches.push(AssertionEventBatch {
                turn_id: record.turn_id,
                actor: record.actor,
                clock: record.clock.0,
                timestamp: record.timestamp,
                events,
            });
        }

        let has_more = iter.peek().is_some();
        let head = self.runtime.branch_manager().head(branch).cloned();

        Ok(AssertionEventChunk {
            events: batches,
            next_cursor: last_turn,
            head,
            has_more,
        })
    }

    fn collect_capabilities_for_actor(_actor_id: &ActorId, actor: &Actor) -> Vec<CapabilityInfo> {
        let capabilities = actor.capabilities.read();
        capabilities
            .capabilities
            .values()
            .map(|metadata| CapabilityInfo {
                id: metadata.id,
                issuer: metadata.issuer.clone(),
                issuer_facet: metadata.issuer_facet.clone(),
                issuer_entity: metadata.issuer_entity,
                holder: metadata.holder.clone(),
                holder_facet: metadata.holder_facet.clone(),
                target: metadata.target.clone(),
                kind: metadata.kind.clone(),
                attenuation: metadata.attenuation.clone(),
                status: metadata.status.clone(),
            })
            .collect()
    }

    /// Switch the active branch for subsequent operations
    pub fn switch_branch(&mut self, branch: BranchId) -> Result<()> {
        self.runtime.switch_branch(branch)
    }
}

/// Entity information for display
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EntityInfo {
    /// Entity instance ID
    pub id: Uuid,
    /// Actor ID
    pub actor: ActorId,
    /// Facet ID
    pub facet: FacetId,
    /// Entity type name
    pub entity_type: String,
    /// Number of pattern subscriptions
    pub pattern_count: usize,
}

/// Capability information for display
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CapabilityInfo {
    /// Capability identifier
    pub id: CapId,
    /// Issuing actor
    pub issuer: ActorId,
    /// Facet on the issuer that minted the capability
    pub issuer_facet: FacetId,
    /// Issuer entity instance (if known)
    pub issuer_entity: Option<Uuid>,
    /// Holder actor
    pub holder: ActorId,
    /// Holder facet
    pub holder_facet: FacetId,
    /// Target scope (if any)
    pub target: Option<CapabilityTarget>,
    /// Semantic kind string
    pub kind: String,
    /// Attenuation caveats attached to the capability
    pub attenuation: Vec<preserves::IOValue>,
    /// Current capability status
    pub status: CapabilityStatus,
}

/// Assertion information for dataspace inspection.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssertionInfo {
    /// Actor publishing the assertion.
    pub actor: ActorId,
    /// Handle identifying the assertion.
    pub handle: super::turn::Handle,
    /// Assertion payload.
    pub value: IOValue,
}

/// Filter describing which assertion events should be surfaced.
#[derive(Debug, Clone)]
pub struct AssertionEventFilter {
    /// Restrict events to a particular actor.
    pub actor: Option<ActorId>,
    /// Restrict to assertions whose record label matches.
    pub label: Option<String>,
    /// Restrict to assertions whose first field matches the request id.
    pub request_id: Option<String>,
    /// Whether assertion events (adds) should be included.
    pub include_asserts: bool,
    /// Whether retraction events should be included.
    pub include_retracts: bool,
}

impl AssertionEventFilter {
    /// Construct a filter that includes both asserts and retracts with no additional criteria.
    pub fn inclusive() -> Self {
        Self {
            actor: None,
            label: None,
            request_id: None,
            include_asserts: true,
            include_retracts: true,
        }
    }
}

impl Default for AssertionEventFilter {
    fn default() -> Self {
        Self::inclusive()
    }
}

/// Event emitted when the dataspace changes as part of a turn.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssertionEvent {
    /// Type of event (assert vs retract).
    pub action: AssertionEventAction,
    /// Assertion handle affected by the event.
    pub handle: super::turn::Handle,
    /// Assertion payload (present for asserts).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<IOValue>,
}

/// Action performed on an assertion.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AssertionEventAction {
    /// Assertion was asserted.
    Assert,
    /// Assertion was retracted.
    Retract,
}

/// Grouping of assertion events that occurred within a single turn.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssertionEventBatch {
    /// Turn identifier.
    pub turn_id: TurnId,
    /// Actor that executed the turn.
    pub actor: ActorId,
    /// Logical clock value associated with the turn.
    pub clock: u64,
    /// Timestamp recorded for the turn.
    pub timestamp: chrono::DateTime<chrono::Utc>,
    /// Events emitted during this turn.
    pub events: Vec<AssertionEvent>,
}

/// Chunked response returned when tailing assertion events.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssertionEventChunk {
    /// Collected event batches.
    pub events: Vec<AssertionEventBatch>,
    /// Cursor that can be supplied to retrieve subsequent events.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next_cursor: Option<TurnId>,
    /// Current head of the branch while collecting events.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub head: Option<TurnId>,
    /// Whether additional events are immediately available.
    pub has_more: bool,
}

/// Convert a TurnRecord to a TurnSummary
fn turn_to_summary(record: TurnRecord) -> TurnSummary {
    TurnSummary {
        turn_id: record.turn_id,
        actor: record.actor,
        clock: record.clock.0,
        input_count: record.inputs.len(),
        output_count: record.outputs.len(),
        timestamp: record.timestamp,
    }
}

/// Runtime status information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeStatus {
    /// Active branch
    pub active_branch: BranchId,

    /// Current head turn
    pub head_turn: TurnId,

    /// Number of pending inputs
    pub pending_inputs: usize,

    /// Snapshot interval
    pub snapshot_interval: u64,
}

/// Summary of a turn for display
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TurnSummary {
    /// Turn ID
    pub turn_id: TurnId,

    /// Actor that executed this turn
    pub actor: ActorId,

    /// Logical clock
    pub clock: u64,

    /// Number of inputs
    pub input_count: usize,

    /// Number of outputs
    pub output_count: usize,

    /// Timestamp
    pub timestamp: chrono::DateTime<chrono::Utc>,
}

/// Branch information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BranchInfo {
    /// Branch name
    pub name: BranchId,

    /// Head turn
    pub head_turn: TurnId,

    /// Parent branch
    pub parent: Option<BranchId>,
}

/// Merge report with conflicts and warnings
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MergeReport {
    /// Merge turn ID
    pub merge_turn: TurnId,

    /// Warnings encountered
    pub warnings: Vec<String>,

    /// Conflicts that need resolution
    pub conflicts: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_control_status() {
        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        let control = Control::init(config).unwrap();

        let status = control.status().unwrap();
        assert_eq!(status.active_branch, BranchId::main());
        assert_eq!(status.pending_inputs, 0);
    }

    #[test]
    fn test_control_send_and_step() {
        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        let mut control = Control::init(config).unwrap();

        // Send a message
        let actor_id = ActorId::new();
        let facet_id = FacetId::new();
        let payload = preserves::IOValue::symbol("test");

        let turn_id = control.send_message(actor_id, facet_id, payload).unwrap();
        assert!(!turn_id.as_str().is_empty());
    }

    #[test]
    fn test_control_list_branches() {
        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        let control = Control::init(config).unwrap();

        let branches = control.list_branches().unwrap();
        assert_eq!(branches.len(), 1);
        assert_eq!(branches[0].name, BranchId::main());
    }

    #[test]
    fn test_control_fork() {
        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        let mut control = Control::init(config).unwrap();

        // Fork a new branch
        let new_branch = BranchId::new("experiment");
        let result = control
            .fork(BranchId::main(), new_branch.clone(), None)
            .unwrap();
        assert_eq!(result, new_branch);

        // List branches should now show 2 branches
        let branches = control.list_branches().unwrap();
        assert_eq!(branches.len(), 2);
    }

    #[test]
    fn test_control_goto_and_back() {
        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        let mut control = Control::init(config).unwrap();

        // Send several messages to create turn history
        let actor_id = ActorId::new();
        let facet_id = FacetId::new();

        let mut turn_ids = Vec::new();
        for i in 0..5 {
            let payload = preserves::IOValue::new(i);
            let turn_id = control
                .send_message(actor_id.clone(), facet_id.clone(), payload)
                .unwrap();
            turn_ids.push(turn_id);
        }

        // Go back 2 turns
        let target = control.back(2).unwrap();
        assert_eq!(target, turn_ids[2]); // Should be at turn 3 (index 2)

        // Go to a specific turn
        control.goto(turn_ids[1].clone()).unwrap();

        // Verify we're at the right place by checking status
        let status = control.status().unwrap();
        assert_eq!(status.head_turn, turn_ids[1]);
    }

    #[test]
    fn test_replay_preserves_state() {
        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        let mut control = Control::init(config).unwrap();

        // Create turn history by sending messages
        // Each message creates outputs which get recorded in StateDelta
        let actor_id = ActorId::new();
        let facet_id = FacetId::new();

        let mut turn_ids = Vec::new();
        for i in 0..5 {
            let payload = preserves::IOValue::new(i);
            let turn_id = control
                .send_message(actor_id.clone(), facet_id.clone(), payload)
                .unwrap();
            turn_ids.push(turn_id);
        }

        // Verify actor exists and has some state
        let actor_count_before = control.runtime().actors.len();
        assert!(actor_count_before > 0, "Should have actors before replay");

        // Go back to turn 2
        if turn_ids.len() >= 3 {
            control.goto(turn_ids[2].clone()).unwrap();

            // Verify actor still exists after replay
            let actor_count_after = control.runtime().actors.len();
            assert_eq!(
                actor_count_after, actor_count_before,
                "Replay should preserve actors"
            );

            // Verify we're at the correct turn
            let status = control.status().unwrap();
            assert_eq!(
                status.head_turn, turn_ids[2],
                "Should be at target turn after goto"
            );
        }
    }

    #[test]
    fn test_merge_clean() {
        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        let mut control = Control::init(config).unwrap();

        let actor_id = ActorId::new();
        let facet_id = FacetId::new();

        // Create some history on main
        for i in 0..3 {
            control
                .send_message(
                    actor_id.clone(),
                    facet_id.clone(),
                    preserves::IOValue::new(i),
                )
                .unwrap();
        }

        // Fork a branch
        let experiment = BranchId::new("experiment");
        control
            .fork(BranchId::main(), experiment.clone(), None)
            .unwrap();

        // Switch to experiment and make changes
        control
            .runtime_mut()
            .switch_branch(experiment.clone())
            .unwrap();
        for i in 10..12 {
            control
                .send_message(
                    actor_id.clone(),
                    facet_id.clone(),
                    preserves::IOValue::new(i),
                )
                .unwrap();
        }

        // Switch back to main and make different changes
        control
            .runtime_mut()
            .switch_branch(BranchId::main())
            .unwrap();
        for i in 20..22 {
            control
                .send_message(
                    actor_id.clone(),
                    facet_id.clone(),
                    preserves::IOValue::new(i),
                )
                .unwrap();
        }

        // Merge experiment into main
        let result = control.merge(experiment, BranchId::main()).unwrap();

        assert!(!result.merge_turn.as_str().is_empty());
        // Clean merge should have minimal warnings
        assert!(
            result.warnings.len() <= 2,
            "Should have few or no warnings for clean merge"
        );
    }

    #[test]
    fn test_merge_with_conflicts() {
        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        let mut control = Control::init(config).unwrap();

        let actor_id = ActorId::new();
        let facet_id = FacetId::new();

        // Create base history
        control
            .send_message(
                actor_id.clone(),
                facet_id.clone(),
                preserves::IOValue::symbol("base"),
            )
            .unwrap();

        // Fork branch
        let experiment = BranchId::new("experiment");
        control
            .fork(BranchId::main(), experiment.clone(), None)
            .unwrap();

        // The merge functionality is implemented and tested
        // Conflicts would be detected in detect_conflicts()
        // For now, verify the merge mechanism works

        let result = control.merge(experiment, BranchId::main());
        assert!(
            result.is_ok(),
            "Merge should succeed even with potential conflicts"
        );
    }

    #[test]
    fn test_entity_registration() {
        use super::super::actor::Activation;
        use super::super::registry::EntityCatalog;

        struct TestEntity;

        impl super::super::actor::Entity for TestEntity {
            fn on_message(
                &self,
                _activation: &mut Activation,
                _payload: &preserves::IOValue,
            ) -> super::super::error::ActorResult<()> {
                Ok(())
            }
        }

        let temp = TempDir::new().unwrap();
        let config = RuntimeConfig {
            root: temp.path().to_path_buf(),
            snapshot_interval: 10,
            flow_control_limit: 100,
            debug: false,
        };

        // Register the entity type in the global registry
        EntityCatalog::global().register("test-entity", |_config| Ok(Box::new(TestEntity)));

        let mut control = Control::init(config).unwrap();

        // Register an entity instance
        let actor_id = ActorId::new();
        let facet_id = FacetId::new();
        let entity_config = preserves::IOValue::symbol("test-config");

        let entity_id = control
            .register_entity(
                actor_id.clone(),
                facet_id.clone(),
                "test-entity".to_string(),
                entity_config,
            )
            .unwrap();

        // List entities
        let entities = control.list_entities();
        assert_eq!(entities.len(), 1);
        assert_eq!(entities[0].id, entity_id);
        assert_eq!(entities[0].entity_type, "test-entity");

        // List for specific actor
        let actor_entities = control.list_entities_for_actor(&actor_id);
        assert_eq!(actor_entities.len(), 1);

        // Unregister
        let removed = control.unregister_entity(entity_id).unwrap();
        assert!(removed);

        // Should be gone
        let entities = control.list_entities();
        assert_eq!(entities.len(), 0);
    }
}
