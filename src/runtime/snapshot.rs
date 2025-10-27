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

    /// Total number of turns executed (for ordering)
    pub turn_count: u64,

    /// Turn ID captured in this snapshot (for verification)
    pub turn_id: TurnId,
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
        // Use turn_count for filename to ensure proper ordering
        let snapshot_path = self.snapshot_path_by_count(&snapshot.branch, snapshot.metadata.turn_count);

        // Serialize snapshot using preserves
        use preserves::PackedWriter;
        let mut buf = Vec::new();
        let mut writer = PackedWriter::new(&mut buf);
        preserves::serde::to_writer(&mut writer, snapshot)
            .map_err(|e| SnapshotError::InvalidFormat(e.to_string()))?;

        self.storage.write_atomic(&snapshot_path, &buf)?;

        Ok(())
    }

    /// Load a snapshot from preserves encoding by turn count
    pub fn load_by_count(&self, branch: &BranchId, turn_count: u64) -> SnapshotResult<RuntimeSnapshot> {
        let snapshot_path = self.snapshot_path_by_count(branch, turn_count);

        let data = self.storage.read_file(&snapshot_path)?;
        let snapshot: RuntimeSnapshot = preserves::serde::from_bytes(&data)
            .map_err(|e| SnapshotError::InvalidFormat(e.to_string()))?;

        Ok(snapshot)
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
    ///
    /// Searches by turn_count ordering, not turn_id hash comparison.
    /// Returns the turn_count of the best snapshot, or None if no snapshots exist.
    pub fn nearest_snapshot(
        &self,
        branch: &BranchId,
        _turn_id: &TurnId,
    ) -> SnapshotResult<Option<u64>> {
        let snapshot_dir = self.storage.branch_snapshot_dir(branch);

        if !snapshot_dir.exists() {
            return Ok(None);
        }

        // List all snapshot files and extract turn counts
        let mut snapshot_counts = Vec::new();
        if let Ok(entries) = std::fs::read_dir(&snapshot_dir) {
            for entry in entries.flatten() {
                let file_name = entry.file_name();
                let name = file_name.to_string_lossy();

                // Format: turn-NNNNNNNN.snapshot
                if let Some(count_str) = name
                    .strip_prefix("turn-")
                    .and_then(|s| s.strip_suffix(".snapshot"))
                {
                    if let Ok(count) = count_str.parse::<u64>() {
                        snapshot_counts.push(count);
                    }
                }
            }
        }

        if snapshot_counts.is_empty() {
            return Ok(None);
        }

        // Sort by turn count
        snapshot_counts.sort_unstable();

        // For now, return the latest snapshot since we don't have a way to map
        // turn_id to turn_count without loading snapshots.
        // A full implementation would maintain an index mapping turn_id -> turn_count
        // or load each snapshot's metadata to check the turn_id.
        Ok(snapshot_counts.last().copied())
    }

    /// Check if a snapshot should be created based on interval
    pub fn should_snapshot(&self, turn_count: u64) -> bool {
        turn_count % self.interval == 0
    }

    /// Get the path for a snapshot file using turn count
    fn snapshot_path(&self, branch: &BranchId, turn_id: &TurnId) -> std::path::PathBuf {
        self.storage
            .branch_snapshot_dir(branch)
            .join(format!("{}.snapshot", turn_id.as_str()))
    }

    /// Get the path for a snapshot file using turn count (for numbered snapshots)
    fn snapshot_path_by_count(&self, branch: &BranchId, turn_count: u64) -> std::path::PathBuf {
        self.storage
            .branch_snapshot_dir(branch)
            .join(format!("turn-{:08}.snapshot", turn_count))
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
