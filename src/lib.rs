//! Duet Runtime – A causally consistent, time-travelable Syndicated Actor runtime
//!
//! This crate implements the Syndicated Actor model with:
//! - Deterministic, causally ordered turns that can be replayed or rewound
//! - Persistent storage of every turn plus periodic full-state snapshots
//! - Time-travel debugging: step forward/backward, jump to any turn, fork branches
//! - CRDT-based branch merging
//! - CLI + control plane for inspection and control
//! - Integration of external services as deterministic Syndicated entities

#![warn(missing_docs)]
#![warn(rust_2018_idioms)]

/// Runtime core modules implementing the Syndicated Actor model
pub mod runtime;

// Re-export key types for convenience
pub use runtime::{Runtime, RuntimeConfig};

/// Current version of the Duet runtime
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Protocol version for control plane communication
pub const PROTOCOL_VERSION: &str = "1.0.0";
