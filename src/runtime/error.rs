//! Error types for the Duet runtime
//!
//! Following the implementation guide, we use thiserror for domain errors
//! and provide conversions at control boundaries.

use std::io;
use std::path::PathBuf;
use thiserror::Error;

// No longer need to import TurnId/BranchId since we use String

/// Top-level runtime error
#[derive(Debug, Error)]
pub enum RuntimeError {
    /// Journal-related errors
    #[error("Journal error: {0}")]
    Journal(#[from] JournalError),

    /// Snapshot-related errors
    #[error("Snapshot error: {0}")]
    Snapshot(#[from] SnapshotError),

    /// Storage-related errors
    #[error("Storage error: {0}")]
    Storage(#[from] StorageError),

    /// Branch-related errors
    #[error("Branch error: {0}")]
    Branch(#[from] BranchError),

    /// Actor/turn execution errors
    #[error("Actor error: {0}")]
    Actor(#[from] ActorError),

    /// Configuration errors
    #[error("Configuration error: {0}")]
    Config(String),

    /// Initialization errors
    #[error("Initialization failed: {0}")]
    Init(String),
}

/// Journal-specific errors
#[derive(Debug, Error)]
pub enum JournalError {
    /// Turn not found in journal
    #[error("Turn '{0}' not found in journal")]
    TurnNotFound(String),

    /// Segment file not found
    #[error("Segment {0} not found")]
    SegmentNotFound(u64),

    /// Corrupted journal segment
    #[error("Corrupted segment {segment} at offset {offset}: {detail}")]
    CorruptedSegment {
        segment: u64,
        offset: u64,
        detail: String,
    },

    /// Index corruption
    #[error("Index corrupted: {0}")]
    IndexCorrupted(String),

    /// Encoding error
    #[error("Turn encoding failed: {0}")]
    EncodingError(String),

    /// Decoding error
    #[error("Turn decoding failed: {0}")]
    DecodingError(String),

    /// IO error
    #[error("IO error: {0}")]
    Io(#[from] io::Error),
}

/// Snapshot-specific errors
#[derive(Debug, Error)]
pub enum SnapshotError {
    /// Snapshot not found
    #[error("Snapshot for branch '{branch}' at turn '{turn_id}' not found")]
    NotFound { branch: String, turn_id: String },

    /// Invalid snapshot format
    #[error("Invalid snapshot format: {0}")]
    InvalidFormat(String),

    /// Snapshot validation failed
    #[error("Snapshot validation failed: {0}")]
    ValidationFailed(String),

    /// IO error
    #[error("IO error: {0}")]
    Io(#[from] io::Error),
}

/// Storage-specific errors
#[derive(Debug, Error)]
pub enum StorageError {
    /// Path not found
    #[error("Path not found: {0}")]
    PathNotFound(PathBuf),

    /// Permission denied
    #[error("Permission denied: {0}")]
    PermissionDenied(PathBuf),

    /// Atomic write failed
    #[error("Atomic write failed for {path}: {detail}")]
    AtomicWriteFailed { path: PathBuf, detail: String },

    /// Config file error
    #[error("Config file error: {0}")]
    ConfigError(String),

    /// IO error
    #[error("IO error: {0}")]
    Io(#[from] io::Error),

    /// JSON error
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
}

/// Branch-specific errors
#[derive(Debug, Error)]
pub enum BranchError {
    /// Branch not found
    #[error("Branch '{0}' not found")]
    NotFound(String),

    /// Branch already exists
    #[error("Branch '{0}' already exists")]
    AlreadyExists(String),

    /// Invalid fork point
    #[error("Invalid fork point: turn '{0}' not found")]
    InvalidForkPoint(String),

    /// Merge conflict
    #[error("Merge conflict between '{source_branch}' and '{target_branch}': {detail}")]
    MergeConflict {
        source_branch: String,
        target_branch: String,
        detail: String,
    },
}

/// Actor execution errors
#[derive(Debug, Error)]
pub enum ActorError {
    /// Actor not found
    #[error("Actor {0} not found")]
    NotFound(String),

    /// Facet not found
    #[error("Facet {0} not found")]
    FacetNotFound(String),

    /// Invalid activation
    #[error("Invalid activation: {0}")]
    InvalidActivation(String),

    /// Flow control limit exceeded
    #[error("Flow control limit exceeded for actor {0}")]
    FlowControlExceeded(String),

    /// Turn execution failed
    #[error("Turn execution failed: {0}")]
    ExecutionFailed(String),
}

/// Result type using RuntimeError
pub type Result<T> = std::result::Result<T, RuntimeError>;
