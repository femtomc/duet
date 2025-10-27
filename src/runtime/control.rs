//! Runtime control facade for CLI and tests
//!
//! Provides high-level API for controlling the runtime: sending messages,
//! stepping, rewinding, forking, merging, and inspecting state.

use serde::{Deserialize, Serialize};

use super::{Runtime, RuntimeConfig};
use super::error::Result;
use super::turn::{ActorId, BranchId, FacetId, TurnId, TurnRecord};

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
        let head_turn = self.runtime.branch_manager()
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
                "No turn executed after sending message".into()
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

    /// Merge branches (placeholder - full implementation in task 7)
    pub fn merge(&mut self, _source: BranchId, _target: BranchId) -> Result<MergeReport> {
        // TODO: Implement in merge task
        Err(super::error::RuntimeError::Init("merge not yet implemented".into()))
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
        let result = control.fork(BranchId::main(), new_branch.clone(), None).unwrap();
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
            let turn_id = control.send_message(actor_id.clone(), facet_id.clone(), payload).unwrap();
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
}
