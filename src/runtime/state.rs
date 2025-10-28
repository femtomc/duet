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

    /// Join two state deltas (CRDT merge)
    ///
    /// Combines deltas from two diverged branches. This is used during branch merges
    /// to produce a unified state that incorporates changes from both branches.
    pub fn join(&self, other: &StateDelta) -> StateDelta {
        StateDelta {
            assertions: self.assertions.join(&other.assertions),
            facets: self.facets.join(&other.facets),
            capabilities: self.capabilities.join(&other.capabilities),
            timers: self.timers.join(&other.timers),
            accounts: self.accounts.join(&other.accounts),
        }
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
pub type AssertionValue = preserves::IOValue;

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

    /// Join two assertion deltas (CRDT merge)
    ///
    /// Combines additions and retractions from both deltas.
    /// Deduplicates by version to handle concurrent operations.
    pub fn join(&self, other: &AssertionDelta) -> AssertionDelta {
        let mut result = AssertionDelta::default();

        // Union of all additions (deduplicate by version)
        let mut seen_versions = HashSet::new();
        for item in self.added.iter().chain(other.added.iter()) {
            if seen_versions.insert(item.3) {
                result.added.push(item.clone());
            }
        }

        // Union of all retractions (deduplicate by version)
        let mut seen_retractions = HashSet::new();
        for item in self.retracted.iter().chain(other.retracted.iter()) {
            if seen_retractions.insert(item.2) {
                result.retracted.push(item.clone());
            }
        }

        result
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
            if !self
                .tombstones
                .contains(&(actor.clone(), handle.clone(), *version))
            {
                self.active.insert(key, (value.clone(), *version));
            }
        }

        for (actor, handle, version) in &delta.retracted {
            let key = (actor.clone(), handle.clone());
            self.active.remove(&key);
            self.tombstones
                .insert((actor.clone(), handle.clone(), *version));
        }
    }

    /// Join two assertion sets (CRDT merge)
    pub fn join(&self, other: &AssertionSet) -> AssertionSet {
        let mut result = AssertionSet::new();

        // Union of tombstones
        result.tombstones = self.tombstones.union(&other.tombstones).cloned().collect();

        // Union of active assertions, minus tombstones
        for (key, (value, version)) in self.active.iter().chain(other.active.iter()) {
            if !result
                .tombstones
                .contains(&(key.0.clone(), key.1.clone(), *version))
            {
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

    /// Join two facet deltas (CRDT merge)
    ///
    /// Combines spawned and terminated facets from both deltas.
    /// Deduplicates by facet ID.
    pub fn join(&self, other: &FacetDelta) -> FacetDelta {
        let mut result = FacetDelta::default();

        // Union of spawned facets (deduplicate by ID)
        let mut seen_spawned = HashSet::new();
        for metadata in self.spawned.iter().chain(other.spawned.iter()) {
            if seen_spawned.insert(metadata.id.clone()) {
                result.spawned.push(metadata.clone());
            }
        }

        // Union of terminated facets (deduplicate by ID)
        let mut seen_terminated = HashSet::new();
        for facet_id in self.terminated.iter().chain(other.terminated.iter()) {
            if seen_terminated.insert(facet_id.clone()) {
                result.terminated.push(facet_id.clone());
            }
        }

        result
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
            result
                .facets
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

/// Target of a capability (actor/facet to which it applies)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CapabilityTarget {
    /// Actor affected when this capability is exercised
    pub actor: ActorId,
    /// Optional facet for scoping (None means actor-wide)
    pub facet: Option<FacetId>,
}

/// Capability metadata recorded in the CRDT
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CapabilityMetadata {
    /// Capability ID
    pub id: CapId,
    /// Actor that issued the capability
    pub issuer: ActorId,
    /// Facet from which the capability was minted
    #[serde(default = "default_facet_id")]
    pub issuer_facet: FacetId,
    /// Entity that granted the capability
    #[serde(default = "default_option_uuid")]
    pub issuer_entity: Option<uuid::Uuid>,
    /// Actor currently holding the capability
    pub holder: ActorId,
    /// Facet within the holder that received the capability
    pub holder_facet: FacetId,
    /// Target scope of the capability (if applicable)
    pub target: Option<CapabilityTarget>,
    /// Semantic kind (e.g., "workspace/edit", "agent/run")
    pub kind: String,
    /// Attenuation caveats applied to this capability
    pub attenuation: Vec<preserves::IOValue>,
    /// Status
    pub status: CapabilityStatus,
}

impl CapabilityMetadata {
    /// Create a revoked metadata placeholder (used when only a revoke is observed)
    pub fn revoked_placeholder(id: CapId) -> Self {
        Self {
            id,
            issuer: ActorId::from_uuid(Uuid::nil()),
            issuer_facet: FacetId::from_uuid(Uuid::nil()),
            issuer_entity: None,
            holder: ActorId::from_uuid(Uuid::nil()),
            holder_facet: FacetId::from_uuid(Uuid::nil()),
            target: None,
            kind: String::from("unknown"),
            attenuation: Vec::new(),
            status: CapabilityStatus::Revoked,
        }
    }
}

fn default_facet_id() -> FacetId {
    FacetId::from_uuid(Uuid::nil())
}

fn default_option_uuid() -> Option<uuid::Uuid> {
    None
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

    /// Join two capability deltas (CRDT merge)
    ///
    /// Combines granted and revoked capabilities from both deltas.
    /// Revoked status dominates (once revoked, stays revoked).
    pub fn join(&self, other: &CapabilityDelta) -> CapabilityDelta {
        let mut granted = HashMap::new();

        for metadata in self.granted.iter().chain(other.granted.iter()) {
            granted.insert(metadata.id, metadata.clone());
        }

        let mut revoked = HashSet::new();
        for cap_id in self.revoked.iter().chain(other.revoked.iter()) {
            revoked.insert(*cap_id);
        }

        CapabilityDelta {
            granted: granted.into_values().collect(),
            revoked: revoked.into_iter().collect(),
        }
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
            self.capabilities
                .entry(metadata.id)
                .and_modify(|existing| *existing = metadata.clone())
                .or_insert_with(|| metadata.clone());
        }

        for cap_id in &delta.revoked {
            let entry = self
                .capabilities
                .entry(*cap_id)
                .or_insert_with(|| CapabilityMetadata::revoked_placeholder(*cap_id));
            entry.status = CapabilityStatus::Revoked;
        }
    }

    /// Join two capability maps (CRDT merge)
    /// Revoked status dominates Active
    pub fn join(&self, other: &CapabilityMap) -> CapabilityMap {
        let mut result = CapabilityMap::new();

        for (id, metadata) in self.capabilities.iter().chain(other.capabilities.iter()) {
            result
                .capabilities
                .entry(*id)
                .and_modify(|existing| {
                    // Prefer the latest metadata from the other map
                    *existing = metadata.clone();
                })
                .or_insert_with(|| metadata.clone());
        }

        // Ensure revoked status dominates
        for (id, metadata) in self.capabilities.iter().chain(other.capabilities.iter()) {
            if metadata.status == CapabilityStatus::Revoked {
                if let Some(existing) = result.capabilities.get_mut(id) {
                    existing.status = CapabilityStatus::Revoked;
                }
            }
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

    /// Join two timer deltas (CRDT merge)
    ///
    /// Combines registered and fired timers from both deltas.
    /// Deduplicates by timer ID.
    pub fn join(&self, other: &TimerDelta) -> TimerDelta {
        let mut result = TimerDelta::default();

        // Union of registered timers
        let mut seen_registered = HashSet::new();
        for timer_id in self.registered.iter().chain(other.registered.iter()) {
            if seen_registered.insert(*timer_id) {
                result.registered.push(*timer_id);
            }
        }

        // Union of fired timers
        let mut seen_fired = HashSet::new();
        for timer_id in self.fired.iter().chain(other.fired.iter()) {
            if seen_fired.insert(*timer_id) {
                result.fired.push(*timer_id);
            }
        }

        result
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

    /// Apply an account delta
    pub fn apply(&mut self, delta: &AccountDelta) {
        self.increments += delta.repaid;
        self.decrements += delta.borrowed;
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

    /// Join two account deltas (CRDT merge)
    ///
    /// Sums borrowed and repaid tokens from both deltas (PN-counter semantics).
    pub fn join(&self, other: &AccountDelta) -> AccountDelta {
        AccountDelta {
            borrowed: self.borrowed + other.borrowed,
            repaid: self.repaid + other.repaid,
        }
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
        let value: preserves::IOValue = preserves::IOValue::symbol("test-value");
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
        let value: preserves::IOValue = preserves::IOValue::symbol("test-value");
        let v1 = Uuid::new_v4();
        let v2 = Uuid::new_v4();

        let mut set1 = AssertionSet::new();
        set1.active
            .insert((actor.clone(), handle1.clone()), (value.clone(), v1));

        let mut set2 = AssertionSet::new();
        set2.active
            .insert((actor.clone(), handle2.clone()), (value.clone(), v2));

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
        assert_eq!(
            joined.facets.get(&facet_id).unwrap().status,
            FacetStatus::Terminated
        );
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

    #[test]
    fn test_state_delta_join() {
        let actor = ActorId::new();
        let handle1 = Handle::new();
        let handle2 = Handle::new();
        let v1 = Uuid::new_v4();
        let v2 = Uuid::new_v4();

        // Delta A: adds handle1
        let delta_a = StateDelta {
            assertions: AssertionDelta {
                added: vec![(
                    actor.clone(),
                    handle1.clone(),
                    preserves::IOValue::symbol("value-a"),
                    v1,
                )],
                retracted: vec![],
            },
            facets: FacetDelta::default(),
            capabilities: CapabilityDelta::default(),
            timers: TimerDelta::default(),
            accounts: AccountDelta {
                borrowed: 10,
                repaid: 5,
            },
        };

        // Delta B: adds handle2
        let delta_b = StateDelta {
            assertions: AssertionDelta {
                added: vec![(
                    actor.clone(),
                    handle2.clone(),
                    preserves::IOValue::symbol("value-b"),
                    v2,
                )],
                retracted: vec![],
            },
            facets: FacetDelta::default(),
            capabilities: CapabilityDelta::default(),
            timers: TimerDelta::default(),
            accounts: AccountDelta {
                borrowed: 3,
                repaid: 7,
            },
        };

        // Join should combine both
        let joined = delta_a.join(&delta_b);

        assert_eq!(joined.assertions.added.len(), 2);
        assert_eq!(joined.accounts.borrowed, 13); // 10 + 3
        assert_eq!(joined.accounts.repaid, 12); // 5 + 7
    }

    #[test]
    fn test_assertion_delta_join_deduplicates() {
        let actor = ActorId::new();
        let handle = Handle::new();
        let version = Uuid::new_v4();

        // Both deltas add the same assertion (same version)
        let delta_a = AssertionDelta {
            added: vec![(
                actor.clone(),
                handle.clone(),
                preserves::IOValue::symbol("value"),
                version,
            )],
            retracted: vec![],
        };

        let delta_b = AssertionDelta {
            added: vec![(
                actor.clone(),
                handle.clone(),
                preserves::IOValue::symbol("value"),
                version,
            )],
            retracted: vec![],
        };

        let joined = delta_a.join(&delta_b);

        // Should deduplicate by version
        assert_eq!(joined.added.len(), 1, "Should deduplicate same version");
    }

    #[test]
    fn test_facet_delta_join() {
        let facet1 = FacetId::new();
        let facet2 = FacetId::new();
        let actor = ActorId::new();

        let delta_a = FacetDelta {
            spawned: vec![FacetMetadata {
                id: facet1.clone(),
                parent: None,
                status: FacetStatus::Alive,
                actor: actor.clone(),
            }],
            terminated: vec![],
        };

        let delta_b = FacetDelta {
            spawned: vec![FacetMetadata {
                id: facet2.clone(),
                parent: None,
                status: FacetStatus::Alive,
                actor: actor.clone(),
            }],
            terminated: vec![facet1.clone()],
        };

        let joined = delta_a.join(&delta_b);

        assert_eq!(joined.spawned.len(), 2, "Should combine spawned facets");
        assert_eq!(joined.terminated.len(), 1, "Should include terminations");
    }

    #[test]
    fn test_capability_delta_join_and_map_apply() {
        let issuer = ActorId::new();
        let holder = ActorId::new();
        let holder_facet = FacetId::new();
        let issuer_facet = FacetId::new();
        let cap_id = Uuid::new_v4();

        let metadata = CapabilityMetadata {
            id: cap_id,
            issuer: issuer.clone(),
            issuer_facet: issuer_facet.clone(),
            issuer_entity: None,
            holder: holder.clone(),
            holder_facet: holder_facet.clone(),
            target: Some(CapabilityTarget {
                actor: holder.clone(),
                facet: Some(holder_facet.clone()),
            }),
            kind: "test/edit".into(),
            attenuation: vec![preserves::IOValue::symbol("caveat")],
            status: CapabilityStatus::Active,
        };

        let grant = CapabilityDelta {
            granted: vec![metadata.clone()],
            revoked: vec![],
        };

        let revoke = CapabilityDelta {
            granted: vec![],
            revoked: vec![cap_id],
        };

        let joined = grant.join(&revoke);

        assert_eq!(joined.granted.len(), 1, "Grant metadata should persist");
        assert_eq!(joined.revoked.len(), 1, "Revocation should be recorded");

        let mut map = CapabilityMap::new();
        map.apply(&joined);

        let entry = map.capabilities.get(&cap_id).expect("capability present");
        assert_eq!(entry.status, CapabilityStatus::Revoked);
        assert_eq!(entry.kind, "test/edit");
        assert_eq!(entry.issuer, issuer);
    }

    #[test]
    fn test_capability_map_join_prefers_latest_metadata() {
        let cap_id = Uuid::new_v4();
        let issuer = ActorId::new();
        let holder = ActorId::new();
        let facet = FacetId::new();
        let issuer_facet = FacetId::new();

        let mut map_a = CapabilityMap::new();
        map_a.capabilities.insert(
            cap_id,
            CapabilityMetadata {
                id: cap_id,
                issuer: issuer.clone(),
                issuer_facet: issuer_facet.clone(),
                issuer_entity: None,
                holder: holder.clone(),
                holder_facet: facet.clone(),
                target: None,
                kind: "test/edit".into(),
                attenuation: vec![preserves::IOValue::symbol("old")],
                status: CapabilityStatus::Active,
            },
        );

        let mut map_b = CapabilityMap::new();
        map_b.capabilities.insert(
            cap_id,
            CapabilityMetadata {
                id: cap_id,
                issuer: issuer.clone(),
                issuer_facet: issuer_facet.clone(),
                issuer_entity: None,
                holder: holder.clone(),
                holder_facet: facet.clone(),
                target: Some(CapabilityTarget {
                    actor: holder.clone(),
                    facet: Some(facet.clone()),
                }),
                kind: "test/edit".into(),
                attenuation: vec![preserves::IOValue::symbol("new")],
                status: CapabilityStatus::Active,
            },
        );

        let joined = map_a.join(&map_b);
        let meta = joined.capabilities.get(&cap_id).unwrap();

        assert_eq!(meta.attenuation, vec![preserves::IOValue::symbol("new")]);
        assert!(meta.target.is_some());
    }
}
