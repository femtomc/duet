//! Runtime orchestrator and public API
//!
//! This module provides the main `Runtime` struct that coordinates all subsystems
//! and exposes the public interface for embedding or controlling the runtime.

use std::path::PathBuf;
use serde::{Deserialize, Serialize};

// Submodules
pub mod actor;
pub mod branch;
pub mod control;
pub mod journal;
pub mod pattern;
pub mod scheduler;
pub mod schema;
pub mod snapshot;
pub mod state;
pub mod storage;
pub mod turn;

// Future module (phase 8)
// pub mod link;

/// Configuration for the Duet runtime
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeConfig {
    /// Root directory for runtime storage (default: .duet/)
    pub root: PathBuf,

    /// Number of turns between automatic snapshots
    pub snapshot_interval: u64,

    /// Maximum credit limit for flow-control accounts
    pub flow_control_limit: u64,

    /// Enable debug tracing
    pub debug: bool,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            root: PathBuf::from(".duet"),
            snapshot_interval: 50,
            flow_control_limit: 1000,
            debug: false,
        }
    }
}

/// The main runtime orchestrator
///
/// Coordinates all subsystems: scheduler, journal, snapshots, branches, and control.
pub struct Runtime {
    config: RuntimeConfig,
    // Additional fields will be added as we implement subsystems
}

impl Runtime {
    /// Create a new runtime with the given configuration
    pub fn new(config: RuntimeConfig) -> anyhow::Result<Self> {
        // TODO: Initialize all subsystems
        Ok(Self { config })
    }

    /// Initialize runtime storage directories and metadata
    pub fn init(config: RuntimeConfig) -> anyhow::Result<()> {
        storage::init_storage(&config.root)?;
        storage::write_config(&config)?;
        Ok(())
    }

    /// Load an existing runtime from storage
    pub fn load(root: PathBuf) -> anyhow::Result<Self> {
        let config = storage::load_config(&root)?;
        Self::new(config)
    }
}

// Re-export commonly used types
pub use control::Control;
pub use turn::{TurnId, TurnRecord};
