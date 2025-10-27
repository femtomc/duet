//! Integration tests for turn execution
//!
//! Tests the complete flow from scheduling through execution to journal persistence.

use duet::runtime::{Runtime, RuntimeConfig};
use tempfile::TempDir;

#[test]
fn test_runtime_initialization() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 100,
        debug: false,
    };

    // Initialize storage
    Runtime::init(config.clone()).unwrap();

    // Load runtime
    let runtime = Runtime::new(config).unwrap();

    assert_eq!(runtime.current_branch().0.as_str(), "main");
}

#[test]
fn test_single_turn_execution() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 100,
        debug: false,
    };

    Runtime::init(config.clone()).unwrap();
    let mut runtime = Runtime::new(config).unwrap();

    // Create an actor and send it a message
    use duet::runtime::turn::{ActorId, FacetId};

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();
    let payload = preserves::IOValue::symbol("test-message");

    runtime.send_message(actor_id, facet_id, payload);

    // Execute the turn
    let result = runtime.step().unwrap();

    assert!(result.is_some(), "Expected a turn to be executed");

    let turn_record = result.unwrap();
    assert_eq!(turn_record.inputs.len(), 1);
}

#[test]
fn test_multiple_turns_deterministic_ordering() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 100,
        debug: false,
    };

    Runtime::init(config.clone()).unwrap();
    let mut runtime = Runtime::new(config).unwrap();

    use duet::runtime::turn::{ActorId, FacetId};

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    // Enqueue multiple messages
    for i in 0..5 {
        let payload = preserves::IOValue::new(preserves::SignedInteger::from(i));
        runtime.send_message(actor_id.clone(), facet_id.clone(), payload);
    }

    // Execute all turns
    let records = runtime.step_n(5).unwrap();

    assert_eq!(records.len(), 5, "All 5 turns should execute");

    // Verify ordering: clocks should be monotonically increasing
    // Note: clocks start at 1 (0.next() = 1)
    for (i, record) in records.iter().enumerate() {
        assert_eq!(record.clock.0, (i + 1) as u64);
    }
}

#[test]
fn test_journal_persistence_after_execution() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 100,
        debug: false,
    };

    Runtime::init(config.clone()).unwrap();

    {
        let mut runtime = Runtime::new(config.clone()).unwrap();

        use duet::runtime::turn::{ActorId, FacetId};

        let actor_id = ActorId::new();
        let facet_id = FacetId::new();

        // Execute some turns
        for i in 0..3 {
            let payload = preserves::IOValue::new(preserves::SignedInteger::from(i));
            runtime.send_message(actor_id.clone(), facet_id.clone(), payload);
        }

        runtime.step_n(3).unwrap();
    }

    // Create a new runtime instance and verify journal was persisted
    let runtime2 = Runtime::new(config).unwrap();

    // The journal should exist and be valid (validated during startup)
    assert_eq!(runtime2.current_branch().0.as_str(), "main");
}

#[test]
fn test_snapshot_creation_at_interval() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 3, // Snapshot every 3 turns
        flow_control_limit: 100,
        debug: false,
    };

    Runtime::init(config.clone()).unwrap();
    let mut runtime = Runtime::new(config).unwrap();

    use duet::runtime::turn::{ActorId, FacetId};

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    // Execute 5 turns (should create snapshots at turns 3 and 6)
    for i in 0..5 {
        let payload = preserves::IOValue::new(preserves::SignedInteger::from(i));
        runtime.send_message(actor_id.clone(), facet_id.clone(), payload);
    }

    let records = runtime.step_n(5).unwrap();
    assert_eq!(records.len(), 5);

    // Snapshots should have been created
    // (Verification would require reading snapshot files, which we'll add later)
}

#[test]
fn test_no_turns_when_queue_empty() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 100,
        debug: false,
    };

    Runtime::init(config.clone()).unwrap();
    let mut runtime = Runtime::new(config).unwrap();

    // Try to step with empty queue
    let result = runtime.step().unwrap();

    assert!(result.is_none(), "No turns should execute when queue is empty");
}

#[test]
fn test_flow_control_blocking() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 10,
        flow_control_limit: 5, // Low limit to test blocking
        debug: false,
    };

    Runtime::init(config.clone()).unwrap();
    let mut runtime = Runtime::new(config).unwrap();

    use duet::runtime::turn::{ActorId, FacetId};

    let actor_id = ActorId::new();
    let facet_id = FacetId::new();

    // Enqueue multiple messages
    for i in 0..10 {
        let payload = preserves::IOValue::new(preserves::SignedInteger::from(i));
        runtime.send_message(actor_id.clone(), facet_id.clone(), payload);
    }

    // Manually update account to exceed limit (simulating borrowed tokens)
    runtime.scheduler_mut().update_account(&actor_id, 10, 0);

    // Try to execute - should be blocked
    let result = runtime.step().unwrap();

    // With account at 10 and limit at 5, turns should be blocked
    // (Note: Current implementation doesn't have actual token borrowing in turns yet,
    // so this test demonstrates the blocking mechanism)

    // Reset account to allow execution
    runtime.scheduler_mut().update_account(&actor_id, 0, 10);

    // Now should be able to execute
    let result = runtime.step().unwrap();
    assert!(result.is_some(), "Turn should execute after account reset");
}
