use duet::codebase;
use duet::runtime::RuntimeConfig;
use duet::runtime::control::Control;
use duet::runtime::registry::EntityCatalog;
use duet::runtime::service::Service;
use preserves::IOValue;
use serde_json::{Value, json};
use std::cell::RefCell;
use std::fs;
use std::io::{self, Cursor, Write};
use std::rc::Rc;
use tempfile::TempDir;

use duet::runtime::actor::{Activation, Entity};
use duet::runtime::error::ActorResult;

#[test]
fn service_handles_basic_commands() {
    // Register a simple entity type for the registry.
    EntityCatalog::global().register("service-test", |_config| Ok(Box::new(SimpleEntity)));

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    // Initialise storage
    Control::init(config.clone()).unwrap();
    let mut control = Control::new(config).unwrap();

    let actor = duet::runtime::turn::ActorId::new();
    let facet = duet::runtime::turn::FacetId::new();
    control
        .register_entity(
            actor.clone(),
            facet.clone(),
            "service-test".to_string(),
            IOValue::symbol("nil"),
        )
        .unwrap();

    let actor_str = actor.to_string();
    let facet_str = facet.0.to_string();

    let sink = Rc::new(RefCell::new(Vec::<u8>::new()));
    let mut service = Service::new(control);

    let requests = vec![
        json!({"id": 1, "command": "status", "params": {}}),
        json!({"id": 2, "command": "handshake", "params": {"client": "test", "protocol_version": duet::PROTOCOL_VERSION}}),
        json!({"id": 3, "command": "status", "params": {}}),
        json!({"id": 4, "command": "list_entities", "params": {}}),
        json!({"id": 5, "command": "list_entities", "params": {"actor": actor_str}}),
        json!({"id": 6, "command": "list_capabilities", "params": {}}),
        json!({"id": 7, "command": "send_message", "params": {
            "target": {"actor": actor.to_string(), "facet": facet_str},
            "payload": "nil"
        }}),
        json!({"id": 8, "command": "history", "params": {"branch": "main", "start": 0, "limit": 10}}),
        json!({"id": 9, "command": "noop", "params": {}}),
    ];

    let input_data = requests
        .into_iter()
        .map(|req| serde_json::to_string(&req).unwrap())
        .collect::<Vec<_>>()
        .join("\n");

    let reader = Cursor::new(format!("{}\n", input_data));
    service.handle(reader, SharedWriter(sink.clone())).unwrap();

    let output = sink.borrow();
    let lines: Vec<_> = output
        .split(|b| *b == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice::<Value>(line).unwrap())
        .collect();

    assert_eq!(lines.len(), 9);

    assert_eq!(lines[0]["error"]["code"], "protocol_error");
    assert!(lines[1]["result"].is_object());
    assert!(lines[2]["result"].is_object());
    assert_eq!(lines[3]["result"]["entities"].as_array().unwrap().len(), 1);
    assert_eq!(lines[4]["result"]["entities"].as_array().unwrap().len(), 1);
    assert!(lines[5]["result"]["capabilities"].is_array());
    assert!(
        lines[5]["result"]["capabilities"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(lines[6]["result"].get("queued_turn").is_some());
    assert!(lines[7]["result"]["turns"].as_array().unwrap().len() >= 1);
    assert_eq!(lines[8]["error"]["code"], "unsupported_command");
}

#[test]
fn workspace_commands_expose_entries() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    Control::init(config.clone()).unwrap();
    let mut control = Control::new(config).unwrap();

    duet::codebase::ensure_workspace_entity(&mut control, temp.path()).unwrap();

    let file_path = temp.path().join("note.txt");
    fs::write(&file_path, "hello world").unwrap();

    let sink = Rc::new(RefCell::new(Vec::<u8>::new()));
    let mut service = Service::new(control);

    let requests = vec![
        json!({"id": 1, "command": "handshake", "params": {"client": "test", "protocol_version": duet::PROTOCOL_VERSION}}),
        json!({"id": 2, "command": "workspace_rescan", "params": {}}),
        json!({"id": 3, "command": "workspace_entries", "params": {}}),
        json!({"id": 4, "command": "workspace_read", "params": {"path": "note.txt"}}),
        json!({"id": 5, "command": "workspace_write", "params": {"path": "note.txt", "content": "updated"}}),
        json!({"id": 6, "command": "workspace_read", "params": {"path": "note.txt"}}),
    ];

    let input_data = requests
        .into_iter()
        .map(|req| serde_json::to_string(&req).unwrap())
        .collect::<Vec<_>>()
        .join("\n");

    let reader = Cursor::new(format!("{}\n", input_data));
    service.handle(reader, SharedWriter(sink.clone())).unwrap();

    let output = sink.borrow();
    let lines: Vec<_> = output
        .split(|b| *b == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice::<Value>(line).unwrap())
        .collect();

    assert_eq!(lines.len(), 6);
    assert!(lines[1]["result"].is_object());
    let entries = lines[2]["result"]["entries"].as_array().unwrap();
    assert!(!entries.is_empty());
    let paths: Vec<_> = entries
        .iter()
        .filter_map(|entry| entry["path"].as_str())
        .collect();
    assert!(paths.iter().any(|path| path.contains("note.txt")));

    assert_eq!(
        lines[3]["result"]["content"].as_str().unwrap(),
        "hello world"
    );
    assert_eq!(lines[4]["result"], json!({"status": "ok"}));
    assert_eq!(lines[5]["result"]["content"].as_str().unwrap(), "updated");
}

#[test]
fn agent_commands_roundtrip() {
    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    Control::init(config.clone()).unwrap();
    duet::codebase::agent::claude::set_external_command(Some("cat".to_string()), vec![]);
    let mut control = Control::new(config).unwrap();
    codebase::ensure_claude_agent(&mut control).unwrap();

    let sink = Rc::new(RefCell::new(Vec::<u8>::new()));
    let mut service = Service::new(control);

    let initial_requests = vec![
        json!({"id": 1, "command": "handshake", "params": {"client": "test", "protocol_version": duet::PROTOCOL_VERSION}}),
        json!({"id": 2, "command": "agent_invoke", "params": {"prompt": "Explain quicksort in Rust"}}),
    ];

    let input_data = initial_requests
        .into_iter()
        .map(|req| serde_json::to_string(&req).unwrap())
        .collect::<Vec<_>>()
        .join("\n");

    service
        .handle(
            Cursor::new(format!("{}\n", input_data)),
            SharedWriter(sink.clone()),
        )
        .unwrap();

    let (request_id, queued_turn, _branch) = {
        let output = sink.borrow();
        let lines: Vec<Value> = output
            .split(|b| *b == b'\n')
            .filter(|line| !line.is_empty())
            .map(|line| serde_json::from_slice::<Value>(line).unwrap())
            .collect();

        assert_eq!(lines.len(), 2);
        let request_id = lines[1]["result"]["request_id"]
            .as_str()
            .unwrap()
            .to_string();
        let queued_turn = lines[1]["result"]["queued_turn"]
            .as_str()
            .map(|s| s.to_string());
        let branch = lines[1]["result"]["branch"].as_str().unwrap().to_string();
        assert_eq!(branch, "main");
        (request_id, queued_turn, branch)
    };

    sink.borrow_mut().clear();

    let mut follow_requests = vec![
        json!({"id": 3, "command": "handshake", "params": {"client": "test", "protocol_version": duet::PROTOCOL_VERSION}}),
        json!({"id": 4, "command": "agent_responses", "params": {}}),
        json!({"id": 5, "command": "dataspace_assertions", "params": {"label": "agent-response", "request_id": request_id}}),
        json!({"id": 6, "command": "dataspace_events", "params": {"label": "agent-response", "limit": 5}}),
    ];

    if let Some(turn) = queued_turn.clone() {
        follow_requests.push(json!({"id": 7, "command": "dataspace_events", "params": {"label": "agent-response", "since": turn, "limit": 5}}));
    }

    follow_requests
        .push(json!({"id": 8, "command": "transcript_show", "params": {"request_id": request_id}}));
    follow_requests.push(json!({"id": 9, "command": "transcript_tail", "params": {"request_id": request_id, "branch": "main", "limit": 5}}));

    let follow_input = follow_requests
        .into_iter()
        .map(|req| serde_json::to_string(&req).unwrap())
        .collect::<Vec<_>>()
        .join("\n");

    service
        .handle(
            Cursor::new(format!("{}\n", follow_input)),
            SharedWriter(sink.clone()),
        )
        .unwrap();

    let lines: Vec<Value> = {
        let output = sink.borrow();
        output
            .split(|b| *b == b'\n')
            .filter(|line| !line.is_empty())
            .map(|line| serde_json::from_slice::<Value>(line).unwrap())
            .collect()
    };

    assert!(lines.len() >= 6);
    let responses = lines[1]["result"]["responses"].as_array().unwrap();
    assert_eq!(responses.len(), 1);
    assert!(
        responses[0]["prompt"]
            .as_str()
            .unwrap()
            .contains("quicksort")
    );

    let assertions = lines[2]["result"]["assertions"].as_array().unwrap();
    assert_eq!(assertions.len(), 1);
    let first_assertion = &assertions[0];
    assert!(first_assertion.get("actor_info").is_some());
    assert!(first_assertion.get("value_structured").is_some());
    assert!(first_assertion.get("summary").is_some());

    let first_events = lines[3]["result"]["events"].as_array().unwrap();
    assert!(!first_events.is_empty());
    let first_event_group = &first_events[0]["events"];
    if let Some(events) = first_event_group.as_array() {
        if let Some(event) = events.first() {
            assert!(event.get("summary").is_some());
            if let Some(structured) = event.get("value_structured") {
                assert!(structured.get("type").is_some());
                assert!(structured.get("summary").is_some());
            }
        }
    }

    if let Some(_turn) = queued_turn {
        if let Some(next_cursor) = lines[3]["result"]["next_cursor"].as_str() {
            assert_ne!(next_cursor, "");
        }
        if lines.len() >= 5 {
            let has_more = lines[4]["result"]["has_more"].as_bool().unwrap();
            assert!(!has_more);
        }
    }

    let transcript_show_idx = lines.len() - 2;
    let transcript_tail_idx = lines.len() - 1;

    let transcript_entries = lines[transcript_show_idx]["result"]["entries"]
        .as_array()
        .unwrap();
    assert!(!transcript_entries.is_empty());
    assert!(
        transcript_entries[0]
            .get("timestamp")
            .and_then(Value::as_str)
            .is_some()
    );
    assert_eq!(
        transcript_entries[0].get("role").and_then(Value::as_str),
        Some("assistant")
    );

    let transcript_events = lines[transcript_tail_idx]["result"]["events"]
        .as_array()
        .unwrap();
    if let Some(first_batch) = transcript_events.get(0) {
        let first_event_set = first_batch["events"].as_array().unwrap();
        if let Some(first_event) = first_event_set.get(0) {
            assert!(first_event.get("transcript").is_some());
            assert!(
                first_event["transcript"]["response_timestamp"]
                    .as_str()
                    .is_some()
            );
            assert_eq!(
                first_event["transcript"]
                    .get("role")
                    .and_then(Value::as_str),
                Some("assistant")
            );
        }
    }

    duet::codebase::agent::claude::set_external_command(None, vec![]);
}

struct SharedWriter(Rc<RefCell<Vec<u8>>>);

impl Write for SharedWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.borrow_mut().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

struct SimpleEntity;

impl Entity for SimpleEntity {
    fn on_message(&self, _activation: &mut Activation, _payload: &IOValue) -> ActorResult<()> {
        Ok(())
    }
}
