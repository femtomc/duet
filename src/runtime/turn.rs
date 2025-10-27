//! Turn metadata, records, and deterministic hashing
//!
//! Defines the core turn abstraction: TurnRecord contains all inputs, outputs,
//! and state deltas for a single deterministic execution step. Turn IDs are
//! computed deterministically from inputs using Blake3 hashing.

use super::state::StateDelta;
use blake3::Hasher;
use chrono::{DateTime, Utc};
use preserves::serde::Error as PreservesSerdeError;
use serde::{Deserialize, Serialize};
use std::fmt;
use uuid::Uuid;

/// Unique identifier for a turn, deterministically computed
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, PartialOrd, Ord)]
pub struct TurnId(String);

impl TurnId {
    /// Create a new TurnId from a string
    pub fn new(id: String) -> Self {
        Self(id)
    }

    /// Get the inner string
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for TurnId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Actor identifier
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct ActorId(pub Uuid);

impl ActorId {
    /// Create a new random ActorId
    pub fn new() -> Self {
        Self(Uuid::new_v4())
    }

    /// Create from a UUID
    pub fn from_uuid(uuid: Uuid) -> Self {
        Self(uuid)
    }
}

impl Default for ActorId {
    fn default() -> Self {
        Self::new()
    }
}

impl fmt::Display for ActorId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Facet identifier within an actor
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct FacetId(pub Uuid);

impl FacetId {
    /// Create a new random FacetId
    pub fn new() -> Self {
        Self(Uuid::new_v4())
    }

    /// Create from a UUID
    pub fn from_uuid(uuid: Uuid) -> Self {
        Self(uuid)
    }
}

impl Default for FacetId {
    fn default() -> Self {
        Self::new()
    }
}

/// Branch identifier
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct BranchId(pub String);

impl BranchId {
    /// Create a new branch ID
    pub fn new(name: impl Into<String>) -> Self {
        Self(name.into())
    }

    /// The main branch
    pub fn main() -> Self {
        Self("main".to_string())
    }
}

impl fmt::Display for BranchId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Logical clock value for causal ordering
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct LogicalClock(pub u64);

impl LogicalClock {
    /// Create a new logical clock at zero
    pub fn zero() -> Self {
        Self(0)
    }

    /// Increment the clock
    pub fn increment(&mut self) {
        self.0 += 1;
    }

    /// Get the next clock value
    pub fn next(&self) -> Self {
        Self(self.0 + 1)
    }
}

/// Handle for an assertion (unique per actor)
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Handle(pub Uuid);

impl Handle {
    /// Create a new random handle
    pub fn new() -> Self {
        Self(Uuid::new_v4())
    }
}

impl Default for Handle {
    fn default() -> Self {
        Self::new()
    }
}

/// Input to a turn (external event or internal message)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum TurnInput {
    /// External message injected into the system
    ExternalMessage {
        /// Target actor
        actor: ActorId,
        /// Target facet
        facet: FacetId,
        /// Message payload
        payload: preserves::IOValue,
    },

    /// Assertion added to the dataspace
    Assert {
        /// Actor making the assertion
        actor: ActorId,
        /// Unique handle for this assertion
        handle: Handle,
        /// Assertion value
        value: preserves::IOValue,
    },

    /// Retraction of a previous assertion
    Retract {
        /// Actor retracting
        actor: ActorId,
        /// Handle to retract
        handle: Handle,
    },

    /// Sync request
    Sync {
        /// Actor requesting sync
        actor: ActorId,
        /// Facet context
        facet: FacetId,
    },

    /// Timer expiration
    Timer {
        /// Actor that registered the timer
        actor: ActorId,
        /// Timer ID
        timer_id: Uuid,
        /// Deadline that was reached
        deadline: DateTime<Utc>,
    },

    /// Response from an external service
    ExternalResponse {
        /// Request ID
        request_id: Uuid,
        /// Actor that made the request
        actor: ActorId,
        /// Response payload
        response: preserves::IOValue,
    },

    /// Remote message from another node (future)
    RemoteMessage {
        /// Source node
        source_node: Uuid,
        /// Source turn ID
        source_turn: TurnId,
        /// Message payload
        payload: preserves::IOValue,
    },
}

/// Output from a turn
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum TurnOutput {
    /// Assertion made during this turn
    Assert {
        /// Handle for the assertion
        handle: Handle,
        /// Assertion value
        value: preserves::IOValue,
    },

    /// Retraction made during this turn
    Retract {
        /// Handle being retracted
        handle: Handle,
    },

    /// Message sent to another actor/facet
    Message {
        /// Target actor
        target_actor: ActorId,
        /// Target facet
        target_facet: FacetId,
        /// Message payload
        payload: preserves::IOValue,
    },

    /// Sync acknowledgment
    Synced {
        /// Facet that completed sync
        facet: FacetId,
    },

    /// Facet spawned
    FacetSpawned {
        /// New facet ID
        facet: FacetId,
        /// Parent facet
        parent: Option<FacetId>,
    },

    /// Facet terminated
    FacetTerminated {
        /// Facet ID
        facet: FacetId,
    },

    /// Timer registered
    TimerRegistered {
        /// Timer ID
        timer_id: Uuid,
        /// Deadline
        deadline: DateTime<Utc>,
    },

    /// External service request
    ExternalRequest {
        /// Request ID
        request_id: Uuid,
        /// Service endpoint
        service: String,
        /// Request payload
        request: preserves::IOValue,
    },

    /// Pattern matched event
    PatternMatched {
        /// Pattern ID that matched
        pattern_id: Uuid,
        /// Handle of assertion that matched
        handle: Handle,
    },

    /// Pattern unmatched event (assertion retracted)
    PatternUnmatched {
        /// Pattern ID that lost a match
        pattern_id: Uuid,
        /// Handle that was retracted
        handle: Handle,
    },
}

/// Complete record of a turn's execution
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TurnRecord {
    /// Deterministic turn ID
    pub turn_id: TurnId,

    /// Actor executing this turn
    pub actor: ActorId,

    /// Branch this turn belongs to
    pub branch: BranchId,

    /// Logical clock value
    pub clock: LogicalClock,

    /// Parent turn (causal predecessor)
    pub parent: Option<TurnId>,

    /// Inputs that triggered this turn
    pub inputs: Vec<TurnInput>,

    /// Outputs produced by this turn
    pub outputs: Vec<TurnOutput>,

    /// State delta (CRDT changes)
    pub delta: StateDelta,

    /// Debug timestamp (not used for determinism)
    pub timestamp: DateTime<Utc>,
}

impl TurnRecord {
    /// Create a new turn record
    pub fn new(
        actor: ActorId,
        branch: BranchId,
        clock: LogicalClock,
        parent: Option<TurnId>,
        inputs: Vec<TurnInput>,
        outputs: Vec<TurnOutput>,
        delta: StateDelta,
    ) -> Self {
        let turn_id = compute_turn_id(&actor, &clock, &inputs);
        Self {
            turn_id,
            actor,
            branch,
            clock,
            parent,
            inputs,
            outputs,
            delta,
            timestamp: Utc::now(),
        }
    }

    /// Encode this turn record to bytes using preserves
    ///
    /// Format: [4-byte length prefix (little-endian)] + [preserves-packed data]
    pub fn encode(&self) -> Result<Vec<u8>, PreservesSerdeError> {
        use preserves::PackedWriter;
        let mut data_buf = Vec::new();
        let mut writer = PackedWriter::new(&mut data_buf);
        preserves::serde::to_writer(&mut writer, self)?;

        // Prepend length
        let len = data_buf.len() as u32;
        let mut result = Vec::with_capacity(4 + data_buf.len());
        result.extend_from_slice(&len.to_le_bytes());
        result.extend_from_slice(&data_buf);

        Ok(result)
    }

    /// Decode a turn record from bytes
    pub fn decode(bytes: &[u8]) -> Result<Self, PreservesSerdeError> {
        preserves::serde::from_bytes(bytes)
    }
}

/// Compute a deterministic turn ID from inputs
///
/// Uses Blake3 to hash the canonical representation of (actor, clock, inputs)
pub fn compute_turn_id(actor: &ActorId, clock: &LogicalClock, inputs: &[TurnInput]) -> TurnId {
    use preserves::PackedWriter;

    let mut hasher = Hasher::new();

    // Hash actor ID
    hasher.update(actor.0.as_bytes());

    // Hash clock
    hasher.update(&clock.0.to_le_bytes());

    // Hash inputs (using preserves canonical encoding)
    for input in inputs {
        let mut buf = Vec::new();
        let mut writer = PackedWriter::new(&mut buf);
        if preserves::serde::to_writer(&mut writer, input).is_ok() {
            hasher.update(&buf);
        }
    }

    let hash = hasher.finalize();
    TurnId::new(format!("turn_{}", hash.to_hex()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_turn_id_deterministic() {
        let actor = ActorId::new();
        let clock = LogicalClock(1);
        let inputs = vec![TurnInput::ExternalMessage {
            actor: actor.clone(),
            facet: FacetId::new(),
            payload: preserves::IOValue::symbol("test-data"),
        }];

        let id1 = compute_turn_id(&actor, &clock, &inputs);
        let id2 = compute_turn_id(&actor, &clock, &inputs);

        assert_eq!(id1, id2, "Turn IDs must be deterministic");
    }

    #[test]
    fn test_turn_id_different_inputs() {
        let actor = ActorId::new();
        let clock = LogicalClock(1);
        let inputs1 = vec![TurnInput::ExternalMessage {
            actor: actor.clone(),
            facet: FacetId::new(),
            payload: preserves::IOValue::symbol("test-data1"),
        }];
        let inputs2 = vec![TurnInput::ExternalMessage {
            actor: actor.clone(),
            facet: FacetId::new(),
            payload: preserves::IOValue::symbol("test-data2"),
        }];

        let id1 = compute_turn_id(&actor, &clock, &inputs1);
        let id2 = compute_turn_id(&actor, &clock, &inputs2);

        assert_ne!(id1, id2, "Different inputs must produce different turn IDs");
    }

    #[test]
    fn test_turn_record_encoding_roundtrip() {
        let actor = ActorId::new();
        let branch = BranchId::main();
        let clock = LogicalClock(1);
        let inputs = vec![];
        let outputs = vec![];
        let delta = StateDelta::empty();

        let record = TurnRecord::new(actor, branch, clock, None, inputs, outputs, delta);
        let encoded = record.encode().unwrap();

        // Verify encoding round-trip
        // decode() expects the full format with length prefix
        let decoded = TurnRecord::decode(&encoded).unwrap();

        assert_eq!(record.turn_id, decoded.turn_id);
        assert_eq!(record.clock, decoded.clock);
    }
}
