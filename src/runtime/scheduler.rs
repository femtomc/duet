//! Deterministic turn scheduler and flow control
//!
//! Maintains ready queues per actor, enforces causal ordering,
//! and integrates flow-control account limits.

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};

use super::turn::{ActorId, LogicalClock, TurnInput};

/// Scheduled turn ready for execution
#[derive(Debug, Clone)]
pub struct ScheduledTurn {
    /// Actor ID
    pub actor: ActorId,
    /// Logical clock
    pub clock: LogicalClock,
    /// Inputs for this turn
    pub inputs: Vec<TurnInput>,
    /// Scheduling cause (for observability)
    pub cause: ScheduleCause,
}

impl PartialEq for ScheduledTurn {
    fn eq(&self, other: &Self) -> bool {
        self.clock == other.clock
    }
}

impl Eq for ScheduledTurn {}

impl PartialOrd for ScheduledTurn {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for ScheduledTurn {
    fn cmp(&self, other: &Self) -> Ordering {
        // Reverse ordering for min-heap (earliest clock first)
        other.clock.cmp(&self.clock)
    }
}

/// Reason a turn was scheduled
#[derive(Debug, Clone)]
pub enum ScheduleCause {
    /// External input (CLI, timer, etc.)
    External,
    /// Message from another actor
    Message,
    /// Timer expiration
    Timer,
    /// Sync completion
    Sync,
}

/// Deterministic turn scheduler
pub struct Scheduler {
    /// Ready queue (min-heap by logical clock)
    ready_queue: BinaryHeap<ScheduledTurn>,

    /// Per-actor logical clocks
    actor_clocks: HashMap<ActorId, LogicalClock>,

    /// Per-actor flow-control account balances
    account_balances: HashMap<ActorId, i64>,

    /// Flow-control credit limit
    credit_limit: i64,
}

impl Scheduler {
    /// Create a new scheduler
    pub fn new(credit_limit: i64) -> Self {
        Self {
            ready_queue: BinaryHeap::new(),
            actor_clocks: HashMap::new(),
            account_balances: HashMap::new(),
            credit_limit,
        }
    }

    /// Enqueue a turn input
    pub fn enqueue(&mut self, actor: ActorId, input: TurnInput, cause: ScheduleCause) {
        // Get or initialize actor clock
        let clock = self
            .actor_clocks
            .entry(actor.clone())
            .or_insert(LogicalClock::zero());
        let next_clock = clock.next();

        let turn = ScheduledTurn {
            actor: actor.clone(),
            clock: next_clock,
            inputs: vec![input],
            cause,
        };

        self.ready_queue.push(turn);
        *clock = next_clock;
    }

    /// Get the next ready turn (if any)
    ///
    /// Returns None if no turns are ready or if flow-control limits prevent execution
    pub fn next_turn(&mut self) -> Option<ScheduledTurn> {
        if let Some(turn) = self.ready_queue.peek() {
            // Check flow-control limit
            let balance = self.account_balances.get(&turn.actor).copied().unwrap_or(0);
            if balance >= self.credit_limit {
                // Actor is blocked by flow control
                return None;
            }

            self.ready_queue.pop()
        } else {
            None
        }
    }

    /// Update flow-control account balance
    pub fn update_account(&mut self, actor: &ActorId, borrowed: i64, repaid: i64) {
        let balance = self.account_balances.entry(actor.clone()).or_insert(0);
        *balance += borrowed - repaid;
    }

    /// Check if any turns are ready
    pub fn has_ready_turns(&self) -> bool {
        !self.ready_queue.is_empty()
    }

    /// Get the number of pending turns
    pub fn pending_count(&self) -> usize {
        self.ready_queue.len()
    }
}

#[cfg(test)]
mod tests {
    use super::super::turn::FacetId;
    use super::*;

    #[test]
    fn test_scheduler_enqueue() {
        let mut scheduler = Scheduler::new(1000);
        let actor = ActorId::new();

        let input = TurnInput::ExternalMessage {
            actor: actor.clone(),
            facet: FacetId::new(),
            payload: preserves::IOValue::symbol("empty"),
        };

        scheduler.enqueue(actor, input, ScheduleCause::External);
        assert_eq!(scheduler.pending_count(), 1);
    }

    #[test]
    fn test_scheduler_ordering() {
        let mut scheduler = Scheduler::new(1000);
        let actor = ActorId::new();

        for i in 0..5 {
            let input = TurnInput::ExternalMessage {
                actor: actor.clone(),
                facet: FacetId::new(),
                payload: preserves::IOValue::new(preserves::SignedInteger::from(i)),
            };
            scheduler.enqueue(actor.clone(), input, ScheduleCause::External);
        }

        // Should execute in order
        let first = scheduler.next_turn().unwrap();
        let second = scheduler.next_turn().unwrap();

        assert!(first.clock < second.clock);
    }

    #[test]
    fn test_flow_control_blocking() {
        let mut scheduler = Scheduler::new(10);
        let actor = ActorId::new();

        let input = TurnInput::ExternalMessage {
            actor: actor.clone(),
            facet: FacetId::new(),
            payload: preserves::IOValue::symbol("empty"),
        };

        scheduler.enqueue(actor.clone(), input, ScheduleCause::External);

        // Set account balance to exceed limit
        scheduler.update_account(&actor, 15, 0);

        // Should be blocked
        assert!(scheduler.next_turn().is_none());
    }
}
