//! Append-only turn log writer and reader
//!
//! Manages journal segments, provides read iterators, and handles
//! crash recovery with partial write detection.

use std::path::PathBuf;
use anyhow::Result;

use super::turn::{TurnId, TurnRecord, BranchId};
use super::storage::Storage;

/// Journal writer for appending turn records
pub struct JournalWriter {
    storage: Storage,
    branch: BranchId,
    current_segment: u64,
}

impl JournalWriter {
    /// Create a new journal writer
    pub fn new(storage: Storage, branch: BranchId) -> Self {
        Self {
            storage,
            branch,
            current_segment: 0,
        }
    }

    /// Append a turn record to the journal
    pub fn append(&mut self, record: &TurnRecord) -> Result<()> {
        let segment_path = self.segment_path(self.current_segment);
        let encoded = record.encode()?;

        // Append to current segment
        // TODO: Implement actual append with segment rotation
        self.storage.write_atomic(&segment_path, &encoded)?;

        Ok(())
    }

    /// Get the path for a segment
    fn segment_path(&self, segment: u64) -> PathBuf {
        self.storage
            .branch_journal_dir(&self.branch)
            .join(format!("segment-{:06}.turnlog", segment))
    }
}

/// Journal reader for iterating over turn records
pub struct JournalReader {
    storage: Storage,
    branch: BranchId,
}

impl JournalReader {
    /// Create a new journal reader
    pub fn new(storage: Storage, branch: BranchId) -> Self {
        Self { storage, branch }
    }

    /// Read a specific turn record
    pub fn read(&self, _turn_id: &TurnId) -> Result<TurnRecord> {
        // TODO: Implement index lookup and read
        unimplemented!("Journal read not yet implemented")
    }

    /// Iterate from a specific turn
    pub fn iter_from(&self, _turn_id: &TurnId) -> Result<JournalIterator> {
        // TODO: Implement iterator
        unimplemented!("Journal iteration not yet implemented")
    }
}

/// Iterator over journal entries
pub struct JournalIterator {
    // TODO: Implement iterator state
}

impl Iterator for JournalIterator {
    type Item = Result<TurnRecord>;

    fn next(&mut self) -> Option<Self::Item> {
        // TODO: Implement iteration
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_journal_writer_creation() {
        let temp = TempDir::new().unwrap();
        let storage = Storage::new(temp.path().to_path_buf());
        let branch = BranchId::main();

        let _writer = JournalWriter::new(storage, branch);
        // Basic smoke test
    }
}
