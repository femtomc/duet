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

/// Serializable branch state used for persistence
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BranchState {
    /// All known branches
    pub branches: Vec<BranchMetadata>,
    /// Active branch identifier
    pub active: BranchId,
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
        Self::from_state(Self::default_state())
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

    /// Get the head turn for a branch
    pub fn head(&self, branch: &BranchId) -> Option<&TurnId> {
        self.branches.get(branch).map(|m| &m.head_turn)
    }

    /// Find the lowest common ancestor of two branches
    ///
    /// Traces ancestry back from both branches to find their common fork point.
    /// Returns the base_turn of the most recent common ancestor.
    pub fn find_lca(&self, branch_a: &BranchId, branch_b: &BranchId) -> Option<TurnId> {
        // Build ancestry path for branch A
        let mut ancestry_a = HashMap::new();
        let mut current = branch_a.clone();

        loop {
            let metadata = self.branches.get(&current)?;

            if let Some(base_turn) = &metadata.base_turn {
                ancestry_a.insert(current.clone(), base_turn.clone());
            }

            match &metadata.parent {
                Some(parent) => current = parent.clone(),
                None => break, // Reached root (main branch)
            }
        }

        // Trace branch B ancestry and find first common point
        let mut current = branch_b.clone();

        loop {
            let metadata = self.branches.get(&current)?;

            // Check if this branch is in A's ancestry
            if let Some(base_turn) = ancestry_a.get(&current) {
                return Some(base_turn.clone());
            }

            // Check if B's base turn is in A's ancestry
            if let Some(base_turn) = &metadata.base_turn {
                if ancestry_a.values().any(|turn| turn == base_turn) {
                    return Some(base_turn.clone());
                }
            }

            match &metadata.parent {
                Some(parent) => current = parent.clone(),
                None => break,
            }
        }

        // If branches share no common ancestry, they diverged from main at turn 0
        Some(TurnId::new("turn_00000000".to_string()))
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

    /// Return a serializable snapshot of branch state
    pub fn state(&self) -> BranchState {
        BranchState {
            branches: self.branches.values().cloned().collect(),
            active: self.active_branch.clone(),
        }
    }

    /// Construct a branch manager from persisted state
    pub fn from_state(state: BranchState) -> Self {
        let mut branches = HashMap::new();
        for metadata in state.branches.into_iter() {
            branches.insert(metadata.id.clone(), metadata);
        }

        let active = if branches.contains_key(&state.active) {
            state.active
        } else {
            BranchId::main()
        };

        Self {
            branches,
            active_branch: active,
        }
    }

    /// Default branch state containing only `main`
    pub fn default_state() -> BranchState {
        let main_branch = BranchId::main();
        let metadata = BranchMetadata {
            id: main_branch.clone(),
            parent: None,
            base_turn: None,
            head_turn: TurnId::new("turn_0".to_string()),
            snapshot: None,
        };

        BranchState {
            branches: vec![metadata],
            active: main_branch,
        }
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

    #[test]
    fn test_find_lca_direct_fork() {
        let mut manager = BranchManager::new();
        let main = BranchId::main();
        let branch_a = BranchId::new("branch-a");
        let branch_b = BranchId::new("branch-b");

        let lca_turn = TurnId::new("turn_10".to_string());

        // Fork both branches from main at turn 10
        manager
            .fork(&main, branch_a.clone(), lca_turn.clone())
            .unwrap();
        manager
            .fork(&main, branch_b.clone(), lca_turn.clone())
            .unwrap();

        // LCA should be turn 10
        let lca = manager.find_lca(&branch_a, &branch_b);
        assert_eq!(lca, Some(lca_turn));
    }

    #[test]
    fn test_find_lca_nested_fork() {
        let mut manager = BranchManager::new();
        let main = BranchId::main();
        let branch_a = BranchId::new("branch-a");
        let branch_b = BranchId::new("branch-b");
        let branch_c = BranchId::new("branch-c");

        let turn_10 = TurnId::new("turn_10".to_string());
        let turn_20 = TurnId::new("turn_20".to_string());

        // Fork A from main at turn 10
        manager
            .fork(&main, branch_a.clone(), turn_10.clone())
            .unwrap();

        // Fork B from A at turn 20
        manager
            .fork(&branch_a, branch_b.clone(), turn_20.clone())
            .unwrap();

        // Fork C from A at turn 20
        manager
            .fork(&branch_a, branch_c.clone(), turn_20.clone())
            .unwrap();

        // LCA of B and C should be turn 20 (both forked from A at that point)
        let lca = manager.find_lca(&branch_b, &branch_c);
        assert_eq!(lca, Some(turn_20));
    }

    #[test]
    fn test_find_lca_same_branch() {
        let mut manager = BranchManager::new();
        let main = BranchId::main();
        let branch_a = BranchId::new("branch-a");

        let turn_10 = TurnId::new("turn_10".to_string());
        manager
            .fork(&main, branch_a.clone(), turn_10.clone())
            .unwrap();

        // LCA of branch with itself should be its base turn
        let lca = manager.find_lca(&branch_a, &branch_a);
        assert_eq!(lca, Some(turn_10));
    }
}
