//! Runtime control facade for CLI and tests
//!
//! Provides high-level API for controlling the runtime: sending messages,
//! stepping, rewinding, forking, merging, and inspecting state.

use serde::{Deserialize, Serialize};

use super::RuntimeConfig;
use super::error::Result;
use super::turn::{ActorId, BranchId, FacetId, TurnId};

/// Control interface for the runtime
pub struct Control {
    config: RuntimeConfig,
    // Additional fields will be added as subsystems are integrated
}

impl Control {
    /// Create a new control interface
    pub fn new(config: RuntimeConfig) -> Self {
        Self { config }
    }

    /// Get runtime status
    pub fn status(&self) -> Result<RuntimeStatus> {
        Ok(RuntimeStatus {
            active_branch: BranchId::main(),
            head_turn: TurnId::new("turn_0".to_string()),
            pending_inputs: 0,
            snapshot_interval: self.config.snapshot_interval,
        })
    }

    /// Send a message to an actor/facet
    pub fn send_message(
        &mut self,
        _actor: ActorId,
        _facet: FacetId,
        _payload: preserves::IOValue,
    ) -> Result<TurnId> {
        // TODO: Implement message sending
        unimplemented!("send_message not yet implemented")
    }

    /// Step forward by N turns
    pub fn step(&mut self, _count: usize) -> Result<Vec<TurnSummary>> {
        // TODO: Implement stepping
        unimplemented!("step not yet implemented")
    }

    /// Go back N turns
    pub fn back(&mut self, _count: usize) -> Result<TurnId> {
        // TODO: Implement rewinding
        unimplemented!("back not yet implemented")
    }

    /// Jump to a specific turn
    pub fn goto(&mut self, _turn_id: TurnId) -> Result<()> {
        // TODO: Implement goto
        unimplemented!("goto not yet implemented")
    }

    /// Fork a new branch
    pub fn fork(
        &mut self,
        _source: BranchId,
        _new_branch: BranchId,
        _from_turn: Option<TurnId>,
    ) -> Result<BranchId> {
        // TODO: Implement forking
        unimplemented!("fork not yet implemented")
    }

    /// Merge branches
    pub fn merge(&mut self, _source: BranchId, _target: BranchId) -> Result<MergeReport> {
        // TODO: Implement merging
        unimplemented!("merge not yet implemented")
    }

    /// Get history for a branch
    pub fn history(
        &self,
        _branch: &BranchId,
        _start: usize,
        _limit: usize,
    ) -> Result<Vec<TurnSummary>> {
        // TODO: Implement history retrieval
        Ok(Vec::new())
    }

    /// List all branches
    pub fn list_branches(&self) -> Result<Vec<BranchInfo>> {
        // TODO: Implement branch listing
        Ok(vec![BranchInfo {
            name: BranchId::main(),
            head_turn: TurnId::new("turn_0".to_string()),
            parent: None,
        }])
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

    #[test]
    fn test_control_status() {
        let config = RuntimeConfig::default();
        let control = Control::new(config);

        let status = control.status().unwrap();
        assert_eq!(status.active_branch, BranchId::main());
    }
}
