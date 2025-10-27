//! Snapshot creation, loading, and interval policy
//!
//! Creates periodic snapshots of full runtime state for faster recovery
//! and time-travel operations.

use serde::{Deserialize, Serialize};

use super::error::{SnapshotError, SnapshotResult};
use super::state::{AssertionSet, CapabilityMap, FacetMap};
use super::storage::Storage;
use super::turn::{BranchId, TurnId};

/// Complete runtime snapshot at a specific turn
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeSnapshot {
    /// Branch this snapshot belongs to
    pub branch: BranchId,

    /// Turn ID at which this snapshot was taken
    pub turn_id: TurnId,

    /// Assertion state
    pub assertions: AssertionSet,

    /// Facet state
    pub facets: FacetMap,

    /// Capability state
    pub capabilities: CapabilityMap,

    /// Metadata
    pub metadata: SnapshotMetadata,
}

/// Snapshot metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotMetadata {
    /// When this snapshot was created (debug only)
    pub created_at: chrono::DateTime<chrono::Utc>,

    /// Number of turns since last snapshot
    pub turn_count: u64,
}

/// Snapshot manager
pub struct SnapshotManager {
    storage: Storage,
    interval: u64,
}

impl SnapshotManager {
    /// Create a new snapshot manager
    pub fn new(storage: Storage, interval: u64) -> Self {
        Self { storage, interval }
    }

    /// Save a snapshot using preserves encoding
    pub fn save(&self, snapshot: &RuntimeSnapshot) -> SnapshotResult<()> {
        let snapshot_path = self.snapshot_path(&snapshot.branch, &snapshot.turn_id);

        // Serialize snapshot using preserves
        use preserves::PackedWriter;
        let mut buf = Vec::new();
        let mut writer = PackedWriter::new(&mut buf);
        preserves::serde::to_writer(&mut writer, snapshot)
            .map_err(|e| SnapshotError::InvalidFormat(e.to_string()))?;

        self.storage.write_atomic(&snapshot_path, &buf)?;

        Ok(())
    }

    /// Load a snapshot from preserves encoding
    pub fn load(&self, branch: &BranchId, turn_id: &TurnId) -> SnapshotResult<RuntimeSnapshot> {
        let snapshot_path = self.snapshot_path(branch, turn_id);

        let data = self.storage.read_file(&snapshot_path)?;
        let snapshot: RuntimeSnapshot = preserves::serde::from_bytes(&data)
            .map_err(|e| SnapshotError::InvalidFormat(e.to_string()))?;

        Ok(snapshot)
    }

    /// Find the nearest snapshot at or before a given turn
    pub fn nearest_snapshot(
        &self,
        _branch: &BranchId,
        _turn_id: &TurnId,
    ) -> SnapshotResult<Option<TurnId>> {
        // TODO: Implement snapshot search
        Ok(None)
    }

    /// Check if a snapshot should be created based on interval
    pub fn should_snapshot(&self, turn_count: u64) -> bool {
        turn_count % self.interval == 0
    }

    /// Get the path for a snapshot file
    fn snapshot_path(&self, branch: &BranchId, turn_id: &TurnId) -> std::path::PathBuf {
        self.storage
            .branch_snapshot_dir(branch)
            .join(format!("{}.snapshot", turn_id.as_str()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_snapshot_interval() {
        let temp = TempDir::new().unwrap();
        let storage = Storage::new(temp.path().to_path_buf());
        let manager = SnapshotManager::new(storage, 50);

        assert!(!manager.should_snapshot(49));
        assert!(manager.should_snapshot(50));
        assert!(!manager.should_snapshot(51));
        assert!(manager.should_snapshot(100));
    }
}
