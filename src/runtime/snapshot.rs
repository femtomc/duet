//! Snapshot creation, loading, and interval policy
//!
//! Creates periodic snapshots of full runtime state for faster recovery
//! and time-travel operations.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use super::error::{SnapshotError, SnapshotResult};
use super::state::{AssertionSet, CapabilityMap, FacetMap};
use super::storage::Storage;
use super::turn::{ActorId, BranchId, FacetId, TurnId};

/// Snapshot of entity private state (for HydratableEntity implementations)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EntityStateSnapshot {
    /// Entity instance ID
    pub entity_id: uuid::Uuid,

    /// Actor this entity belongs to
    pub actor: ActorId,

    /// Facet this entity is attached to
    pub facet: FacetId,

    /// Entity type name
    pub entity_type: String,

    /// Private state blob (from HydratableEntity::snapshot_state)
    #[serde(with = "super::registry::preserves_text_serde")]
    pub state: preserves::IOValue,
}

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

    /// Entity private state (for HydratableEntity implementations)
    pub entity_states: Vec<EntityStateSnapshot>,

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

/// Snapshot index entry mapping turn_id to turn_count
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotIndexEntry {
    /// Turn ID
    pub turn_id: TurnId,
    /// Turn count (for ordering)
    pub turn_count: u64,
}

/// Snapshot index for fast lookups
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SnapshotIndex {
    /// Map of branch -> list of snapshot entries (sorted by turn_count)
    pub snapshots: HashMap<String, Vec<SnapshotIndexEntry>>,
}

impl SnapshotIndex {
    /// Create a new empty index
    pub fn new() -> Self {
        Self::default()
    }

    /// Add a snapshot entry
    pub fn add(&mut self, branch: &BranchId, turn_id: TurnId, turn_count: u64) {
        let entry = SnapshotIndexEntry { turn_id, turn_count };

        self.snapshots
            .entry(branch.0.clone())
            .or_insert_with(Vec::new)
            .push(entry);

        // Keep sorted by turn_count
        if let Some(entries) = self.snapshots.get_mut(&branch.0) {
            entries.sort_by_key(|e| e.turn_count);
        }
    }

    /// Find the nearest snapshot <= target turn_id
    pub fn find_nearest(&self, branch: &BranchId, target: &TurnId) -> Option<u64> {
        let entries = self.snapshots.get(&branch.0)?;

        // Find the latest snapshot whose turn_id <= target
        entries.iter()
            .rev()
            .find(|e| e.turn_id <= *target)
            .map(|e| e.turn_count)
    }

    /// Save index to JSON
    pub fn save(&self, path: &std::path::Path) -> SnapshotResult<()> {
        let data = serde_json::to_vec_pretty(self)
            .map_err(|e| SnapshotError::InvalidFormat(e.to_string()))?;

        std::fs::write(path, data)
            .map_err(|e| SnapshotError::Storage(
                super::error::StorageError::Io(e)
            ))?;

        Ok(())
    }

    /// Load index from JSON
    pub fn load(path: &std::path::Path) -> SnapshotResult<Self> {
        if !path.exists() {
            return Ok(Self::new());
        }

        let data = std::fs::read(path)
            .map_err(|e| SnapshotError::Storage(
                super::error::StorageError::Io(e)
            ))?;

        let index = serde_json::from_slice(&data)
            .map_err(|e| SnapshotError::InvalidFormat(e.to_string()))?;

        Ok(index)
    }
}

/// Snapshot manager
pub struct SnapshotManager {
    storage: Storage,
    interval: u64,
    index: std::sync::Arc<parking_lot::RwLock<SnapshotIndex>>,
}

impl SnapshotManager {
    /// Create a new snapshot manager
    pub fn new(storage: Storage, interval: u64) -> Self {
        // Load snapshot index
        let index_path = storage.meta_dir().join("snapshots.json");
        let index = SnapshotIndex::load(&index_path).unwrap_or_default();

        Self {
            storage,
            interval,
            index: std::sync::Arc::new(parking_lot::RwLock::new(index)),
        }
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

        // Update snapshot index
        {
            let mut index = self.index.write();
            index.add(
                &snapshot.branch,
                snapshot.turn_id.clone(),
                snapshot.metadata.turn_count,
            );

            // Persist index
            let index_path = self.storage.meta_dir().join("snapshots.json");
            index.save(&index_path)?;
        }

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
    /// Uses the snapshot index for fast lookup. Falls back to scanning
    /// if index is unavailable or incomplete.
    /// Returns the turn_count of the best snapshot, or None if no snapshots exist.
    pub fn nearest_snapshot(
        &self,
        branch: &BranchId,
        turn_id: &TurnId,
    ) -> SnapshotResult<Option<u64>> {
        // Try index first
        {
            let index = self.index.read();
            if let Some(turn_count) = index.find_nearest(branch, turn_id) {
                return Ok(Some(turn_count));
            }
        }

        // Fallback: scan directory and load snapshots
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

        // Sort by turn count (oldest first)
        snapshot_counts.sort_unstable();

        // Find the latest snapshot whose turn_id <= target by loading metadata
        let mut best_count = None;

        for count in snapshot_counts.iter().rev() {
            match self.load_by_count(branch, *count) {
                Ok(snapshot) => {
                    if snapshot.metadata.turn_id <= *turn_id {
                        best_count = Some(*count);
                        break;
                    }
                }
                Err(_) => {
                    continue;
                }
            }
        }

        Ok(best_count)
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

    #[test]
    fn test_snapshot_index() {
        let mut index = SnapshotIndex::new();

        let branch = BranchId::main();
        let turn1 = TurnId::new("turn_00000001".to_string());
        let turn2 = TurnId::new("turn_00000002".to_string());
        let turn3 = TurnId::new("turn_00000003".to_string());

        // Add snapshots
        index.add(&branch, turn1.clone(), 10);
        index.add(&branch, turn2.clone(), 20);
        index.add(&branch, turn3.clone(), 30);

        // Find nearest to turn2
        assert_eq!(index.find_nearest(&branch, &turn2), Some(20));

        // Find nearest to turn between 2 and 3
        let turn_between = TurnId::new("turn_00000002a".to_string());
        assert_eq!(index.find_nearest(&branch, &turn_between), Some(20));

        // Find nearest beyond all snapshots
        let turn_future = TurnId::new("turn_00000999".to_string());
        assert_eq!(index.find_nearest(&branch, &turn_future), Some(30));

        // Find nearest before all snapshots
        let turn_past = TurnId::new("turn_00000000".to_string());
        assert_eq!(index.find_nearest(&branch, &turn_past), None);
    }

    #[test]
    fn test_snapshot_index_persistence() {
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let index_path = temp.path().join("snapshots.json");

        let mut index = SnapshotIndex::new();
        let branch = BranchId::main();
        let turn = TurnId::new("turn_00000010".to_string());

        index.add(&branch, turn.clone(), 10);

        // Save
        index.save(&index_path).unwrap();

        // Load
        let loaded = SnapshotIndex::load(&index_path).unwrap();
        assert_eq!(loaded.find_nearest(&branch, &turn), Some(10));
    }
}
