//! Branch DAG, time travel, and CRDT merge orchestration
//!
//! Tracks branch relationships, implements fork/rewind/goto operations,
//! and orchestrates CRDT-based merges.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use super::error::{BranchError, BranchResult};
use super::turn::{BranchId, TurnId};

/// Branch metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BranchMetadata {
    /// Branch ID
    pub id: BranchId,

    /// Parent branch (if any)
    pub parent: Option<BranchId>,

    /// Turn at which this branch was forked
    pub base_turn: Option<TurnId>,

    /// Current head turn
    pub head_turn: TurnId,

    /// Current snapshot (if any)
    pub snapshot: Option<TurnId>,
}

/// Branch manager
pub struct BranchManager {
    /// All branches
    branches: HashMap<BranchId, BranchMetadata>,

    /// Active branch
    active_branch: BranchId,
}

impl BranchManager {
    /// Create a new branch manager with main branch
    pub fn new() -> Self {
        let mut branches = HashMap::new();
        let main_branch = BranchId::main();

        branches.insert(
            main_branch.clone(),
            BranchMetadata {
                id: main_branch.clone(),
                parent: None,
                base_turn: None,
                head_turn: TurnId::new("turn_0".to_string()),
                snapshot: None,
            },
        );

        Self {
            branches,
            active_branch: main_branch,
        }
    }

    /// Get the active branch
    pub fn active_branch(&self) -> &BranchId {
        &self.active_branch
    }

    /// Get metadata for a branch
    pub fn get_branch(&self, id: &BranchId) -> Option<&BranchMetadata> {
        self.branches.get(id)
    }

    /// Create a new branch forked from another
    pub fn fork(
        &mut self,
        source: &BranchId,
        new_branch: BranchId,
        base_turn: TurnId,
    ) -> BranchResult<()> {
        if self.branches.contains_key(&new_branch) {
            return Err(BranchError::AlreadyExists(new_branch.0.clone()));
        }

        let source_metadata = self
            .branches
            .get(source)
            .ok_or_else(|| BranchError::NotFound(source.0.clone()))?;

        let metadata = BranchMetadata {
            id: new_branch.clone(),
            parent: Some(source.clone()),
            base_turn: Some(base_turn.clone()),
            head_turn: base_turn,
            snapshot: source_metadata.snapshot.clone(),
        };

        self.branches.insert(new_branch, metadata);

        Ok(())
    }

    /// Switch to a different branch
    pub fn switch_branch(&mut self, branch: BranchId) -> BranchResult<()> {
        if !self.branches.contains_key(&branch) {
            return Err(BranchError::NotFound(branch.0.clone()));
        }

        self.active_branch = branch;
        Ok(())
    }

    /// Update the head turn for a branch
    pub fn update_head(&mut self, branch: &BranchId, turn: TurnId) -> BranchResult<()> {
        let metadata = self
            .branches
            .get_mut(branch)
            .ok_or_else(|| BranchError::NotFound(branch.0.clone()))?;

        metadata.head_turn = turn;
        Ok(())
    }

    /// Find the lowest common ancestor of two branches
    pub fn find_lca(&self, _branch_a: &BranchId, _branch_b: &BranchId) -> Option<TurnId> {
        // TODO: Implement LCA search
        None
    }

    /// Merge two branches using CRDT join
    pub fn merge(&mut self, _source: &BranchId, _target: &BranchId) -> BranchResult<MergeResult> {
        // TODO: Implement CRDT merge
        unimplemented!("Branch merge not yet implemented")
    }

    /// List all branches
    pub fn list_branches(&self) -> Vec<&BranchMetadata> {
        self.branches.values().collect()
    }
}

impl Default for BranchManager {
    fn default() -> Self {
        Self::new()
    }
}

/// Result of a merge operation
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MergeResult {
    /// Merge turn ID
    pub merge_turn: TurnId,

    /// Warnings/conflicts encountered
    pub warnings: Vec<MergeWarning>,
}

/// Warning about a merge conflict or issue
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MergeWarning {
    /// Warning category
    pub category: String,

    /// Human-readable message
    pub message: String,

    /// Affected handles/capabilities
    pub affected: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_branch_manager_creation() {
        let manager = BranchManager::new();
        assert_eq!(manager.active_branch(), &BranchId::main());
    }

    #[test]
    fn test_branch_fork() {
        let mut manager = BranchManager::new();
        let main = BranchId::main();
        let experiment = BranchId::new("experiment");
        let base_turn = TurnId::new("turn_10".to_string());

        manager
            .fork(&main, experiment.clone(), base_turn.clone())
            .unwrap();

        let metadata = manager.get_branch(&experiment).unwrap();
        assert_eq!(metadata.parent, Some(main));
        assert_eq!(metadata.base_turn, Some(base_turn));
    }
}
