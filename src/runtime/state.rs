//! CRDT components and state delta representation
//!
//! All persistent state is modeled as CRDTs (Conflict-free Replicated Data Types)
//! to support deterministic merging across branches. Provides OR-sets for assertions,
//! lattices for facets and capabilities, and PN-counters for flow control.

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

use super::turn::{ActorId, FacetId, Handle};

/// Complete state delta produced by a turn
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StateDelta {
    /// Changes to assertions
    pub assertions: AssertionDelta,
    /// Changes to facets
    pub facets: FacetDelta,
    /// Changes to capabilities
    pub capabilities: CapabilityDelta,
    /// Changes to timers
    pub timers: TimerDelta,
    /// Changes to flow-control accounts
    pub accounts: AccountDelta,
}

impl StateDelta {
    /// Create an empty delta
    pub fn empty() -> Self {
        Self {
            assertions: AssertionDelta::default(),
            facets: FacetDelta::default(),
            capabilities: CapabilityDelta::default(),
            timers: TimerDelta::default(),
            accounts: AccountDelta::default(),
        }
    }

    /// Check if this delta is empty (no changes)
    pub fn is_empty(&self) -> bool {
        self.assertions.is_empty()
            && self.facets.is_empty()
            && self.capabilities.is_empty()
            && self.timers.is_empty()
            && self.accounts.is_empty()
    }
}

// ========== Assertion CRDT (Observed-Remove Set) ==========

/// OR-Set for assertions with tombstones for retractions
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AssertionSet {
    /// Active assertions: (actor, handle) -> (value, version)
    pub active: HashMap<(ActorId, Handle), (AssertionValue, Uuid)>,
    /// Tombstones for retracted assertions
    pub tombstones: HashSet<(ActorId, Handle, Uuid)>,
}

/// Assertion value (preserves value)
pub type AssertionValue = preserves::value::IOValue;

/// Delta for assertion changes
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AssertionDelta {
    /// Assertions added
    pub added: Vec<(ActorId, Handle, AssertionValue, Uuid)>,
    /// Assertions retracted
    pub retracted: Vec<(ActorId, Handle, Uuid)>,
}

impl AssertionDelta {
    /// Check if empty
    pub fn is_empty(&self) -> bool {
        self.added.is_empty() && self.retracted.is_empty()
    }
}

impl AssertionSet {
    /// Create a new empty assertion set
    pub fn new() -> Self {
        Self::default()
    }

    /// Apply a delta to this set
    pub fn apply(&mut self, delta: &AssertionDelta) {
        for (actor, handle, value, version) in &delta.added {
            let key = (actor.clone(), handle.clone());
            // Only add if not tombstoned
            if !self.tombstones.contains(&(actor.clone(), handle.clone(), *version)) {
                self.active.insert(key, (value.clone(), *version));
            }
        }

        for (actor, handle, version) in &delta.retracted {
            let key = (actor.clone(), handle.clone());
            self.active.remove(&key);
            self.tombstones.insert((actor.clone(), handle.clone(), *version));
        }
    }

    /// Join two assertion sets (CRDT merge)
    pub fn join(&self, other: &AssertionSet) -> AssertionSet {
        let mut result = AssertionSet::new();

        // Union of tombstones
        result.tombstones = self.tombstones.union(&other.tombstones).cloned().collect();

        // Union of active assertions, minus tombstones
        for (key, (value, version)) in self.active.iter().chain(other.active.iter()) {
            if !result.tombstones.contains(&(key.0.clone(), key.1.clone(), *version)) {
                result.active.insert(key.clone(), (value.clone(), *version));
            }
        }

        result
    }
}

// ========== Facet Lifecycle CRDT ==========

/// Status of a facet
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum FacetStatus {
    /// Facet is alive and active
    Alive,
    /// Facet has been terminated
    Terminated,
    /// Facet has been removed (garbage collected)
    Removed,
}

/// Facet metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FacetMetadata {
    /// Facet ID
    pub id: FacetId,
    /// Parent facet (if any)
    pub parent: Option<FacetId>,
    /// Current status
    pub status: FacetStatus,
    /// Actor that owns this facet
    pub actor: ActorId,
}

/// Delta for facet changes
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct FacetDelta {
    /// Facets spawned
    pub spawned: Vec<FacetMetadata>,
    /// Facets terminated
    pub terminated: Vec<FacetId>,
}

impl FacetDelta {
    /// Check if empty
    pub fn is_empty(&self) -> bool {
        self.spawned.is_empty() && self.terminated.is_empty()
    }
}

/// Map of facet IDs to their metadata
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct FacetMap {
    /// Facets by ID
    pub facets: HashMap<FacetId, FacetMetadata>,
}

impl FacetMap {
    /// Create a new empty facet map
    pub fn new() -> Self {
        Self::default()
    }

    /// Apply a delta
    pub fn apply(&mut self, delta: &FacetDelta) {
        for metadata in &delta.spawned {
            self.facets.insert(metadata.id.clone(), metadata.clone());
        }

        for facet_id in &delta.terminated {
            if let Some(metadata) = self.facets.get_mut(facet_id) {
                metadata.status = FacetStatus::Terminated;
            }
        }
    }

    /// Join two facet maps (CRDT merge)
    pub fn join(&self, other: &FacetMap) -> FacetMap {
        let mut result = FacetMap::new();

        for (id, metadata) in self.facets.iter().chain(other.facets.iter()) {
            result.facets
                .entry(id.clone())
                .and_modify(|existing| {
                    // Take the max status (Terminated dominates Alive)
                    if metadata.status > existing.status {
                        existing.status = metadata.status.clone();
                    }
                })
                .or_insert_with(|| metadata.clone());
        }

        result
    }
}

// ========== Capability CRDT ==========

/// Capability identifier
pub type CapId = Uuid;

/// Capability status
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum CapabilityStatus {
    /// Capability is active
    Active,
    /// Capability has been revoked
    Revoked,
}

/// Capability metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CapabilityMetadata {
    /// Capability ID
    pub id: CapId,
    /// Attenuation caveats
    pub attenuation: Vec<preserves::value::IOValue>,
    /// Status
    pub status: CapabilityStatus,
}

/// Delta for capability changes
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CapabilityDelta {
    /// Capabilities granted
    pub granted: Vec<CapabilityMetadata>,
    /// Capabilities revoked
    pub revoked: Vec<CapId>,
}

impl CapabilityDelta {
    /// Check if empty
    pub fn is_empty(&self) -> bool {
        self.granted.is_empty() && self.revoked.is_empty()
    }
}

/// Map of capabilities
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CapabilityMap {
    /// Capabilities by ID
    pub capabilities: HashMap<CapId, CapabilityMetadata>,
}

impl CapabilityMap {
    /// Create a new empty capability map
    pub fn new() -> Self {
        Self::default()
    }

    /// Apply a delta
    pub fn apply(&mut self, delta: &CapabilityDelta) {
        for metadata in &delta.granted {
            self.capabilities.insert(metadata.id, metadata.clone());
        }

        for cap_id in &delta.revoked {
            if let Some(metadata) = self.capabilities.get_mut(cap_id) {
                metadata.status = CapabilityStatus::Revoked;
            }
        }
    }

    /// Join two capability maps (CRDT merge)
    /// Revoked status dominates Active
    pub fn join(&self, other: &CapabilityMap) -> CapabilityMap {
        let mut result = CapabilityMap::new();

        for (id, metadata) in self.capabilities.iter().chain(other.capabilities.iter()) {
            result.capabilities
                .entry(*id)
                .and_modify(|existing| {
                    // Revoked dominates
                    if metadata.status == CapabilityStatus::Revoked {
                        existing.status = CapabilityStatus::Revoked;
                    }
                })
                .or_insert_with(|| metadata.clone());
        }

        result
    }
}

// ========== Timer CRDT ==========

/// Timer identifier
pub type TimerId = Uuid;

/// Timer delta
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct TimerDelta {
    /// Timers registered
    pub registered: Vec<TimerId>,
    /// Timers fired
    pub fired: Vec<TimerId>,
}

impl TimerDelta {
    /// Check if empty
    pub fn is_empty(&self) -> bool {
        self.registered.is_empty() && self.fired.is_empty()
    }
}

// ========== Flow Control (PN-Counter) ==========

/// PN-Counter for flow control accounts
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PNCounter {
    /// Positive increments
    pub increments: i64,
    /// Negative decrements
    pub decrements: i64,
}

impl PNCounter {
    /// Create a new counter at zero
    pub fn new() -> Self {
        Self::default()
    }

    /// Get the current value
    pub fn value(&self) -> i64 {
        self.increments - self.decrements
    }

    /// Increment the counter
    pub fn increment(&mut self, amount: i64) {
        self.increments += amount;
    }

    /// Decrement the counter
    pub fn decrement(&mut self, amount: i64) {
        self.decrements += amount;
    }

    /// Join two counters (CRDT merge)
    pub fn join(&self, other: &PNCounter) -> PNCounter {
        PNCounter {
            increments: self.increments + other.increments,
            decrements: self.decrements + other.decrements,
        }
    }
}

/// Account delta
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AccountDelta {
    /// Borrowed tokens
    pub borrowed: i64,
    /// Repaid tokens
    pub repaid: i64,
}

impl AccountDelta {
    /// Check if empty
    pub fn is_empty(&self) -> bool {
        self.borrowed == 0 && self.repaid == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_assertion_set_add_and_retract() {
        let mut set = AssertionSet::new();
        let actor = ActorId::new();
        let handle = Handle::new();
        let value: preserves::value::IOValue = preserves::value::Value::symbol("test-value").wrap();
        let version = Uuid::new_v4();

        // Add assertion
        let delta = AssertionDelta {
            added: vec![(actor.clone(), handle.clone(), value.clone(), version)],
            retracted: vec![],
        };
        set.apply(&delta);

        assert_eq!(set.active.len(), 1);

        // Retract assertion
        let delta = AssertionDelta {
            added: vec![],
            retracted: vec![(actor.clone(), handle.clone(), version)],
        };
        set.apply(&delta);

        assert_eq!(set.active.len(), 0);
        assert_eq!(set.tombstones.len(), 1);
    }

    #[test]
    fn test_assertion_set_join() {
        let actor = ActorId::new();
        let handle1 = Handle::new();
        let handle2 = Handle::new();
        let value: preserves::value::IOValue = preserves::value::Value::symbol("test-value").wrap();
        let v1 = Uuid::new_v4();
        let v2 = Uuid::new_v4();

        let mut set1 = AssertionSet::new();
        set1.active.insert(
            (actor.clone(), handle1.clone()),
            (value.clone(), v1),
        );

        let mut set2 = AssertionSet::new();
        set2.active.insert(
            (actor.clone(), handle2.clone()),
            (value.clone(), v2),
        );

        let joined = set1.join(&set2);
        assert_eq!(joined.active.len(), 2);
    }

    #[test]
    fn test_facet_map_join() {
        let actor = ActorId::new();
        let facet_id = FacetId::new();

        let mut map1 = FacetMap::new();
        map1.facets.insert(
            facet_id.clone(),
            FacetMetadata {
                id: facet_id.clone(),
                parent: None,
                status: FacetStatus::Alive,
                actor: actor.clone(),
            },
        );

        let mut map2 = FacetMap::new();
        map2.facets.insert(
            facet_id.clone(),
            FacetMetadata {
                id: facet_id.clone(),
                parent: None,
                status: FacetStatus::Terminated,
                actor: actor.clone(),
            },
        );

        let joined = map1.join(&map2);
        assert_eq!(joined.facets.get(&facet_id).unwrap().status, FacetStatus::Terminated);
    }

    #[test]
    fn test_pn_counter() {
        let mut counter = PNCounter::new();
        assert_eq!(counter.value(), 0);

        counter.increment(10);
        assert_eq!(counter.value(), 10);

        counter.decrement(3);
        assert_eq!(counter.value(), 7);

        let mut counter2 = PNCounter::new();
        counter2.increment(5);

        let joined = counter.join(&counter2);
        assert_eq!(joined.value(), 12);
    }
}
