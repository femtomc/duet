use duet::runtime::RuntimeConfig;
use duet::runtime::control::Control;
use duet::runtime::registry::EntityCatalog;
use duet::service::Service;
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
    let sink = Rc::new(RefCell::new(Vec::<u8>::new()));
    let mut service = Service::new(control);

    let requests = vec![
        json!({"id": 1, "command": "status", "params": {}}),
        json!({"id": 2, "command": "handshake", "params": {"client": "test", "protocol_version": duet::PROTOCOL_VERSION}}),
        json!({"id": 3, "command": "status", "params": {}}),
        json!({"id": 4, "command": "list_entities", "params": {}}),
        json!({"id": 5, "command": "list_entities", "params": {"actor": actor_str}}),
        json!({"id": 6, "command": "list_capabilities", "params": {}}),
        json!({"id": 7, "command": "list_branches", "params": {}}),
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
    let all_entities = lines[3]["result"]["entities"].as_array().unwrap();
    assert!(
        all_entities
            .iter()
            .any(|entity| entity["entity_type"].as_str() == Some("service-test")),
        "expected service-test entity in listing"
    );
    assert_eq!(lines[4]["result"]["entities"].as_array().unwrap().len(), 1);
    assert!(lines[5]["result"]["capabilities"].is_array());
    assert!(
        lines[5]["result"]["capabilities"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(lines[6]["result"]["branches"].is_array());
    assert!(lines[7]["result"]["turns"].is_array());
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

    let file_path = temp.path().join("note.txt");
    fs::write(&file_path, "hello world").unwrap();

    Control::init(config.clone()).unwrap();
    let mut control = Control::new(config).unwrap();

    duet::codebase::ensure_workspace_entity(&mut control, temp.path()).unwrap();

    let sink = Rc::new(RefCell::new(Vec::<u8>::new()));
    let mut service = Service::new(control);

    let requests = vec![
        json!({"id": 1, "command": "handshake", "params": {"client": "test", "protocol_version": duet::PROTOCOL_VERSION}}),
        json!({"id": 2, "command": "workspace_entries", "params": {}}),
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

    assert_eq!(lines.len(), 2);
    assert!(lines[0]["result"].is_object());
    let entries = lines[1]["result"]["entries"].as_array().unwrap();
    assert!(!entries.is_empty());
    let paths: Vec<_> = entries
        .iter()
        .filter_map(|entry| entry["path"].as_str())
        .collect();
    assert!(paths.iter().any(|path| path.contains("note.txt")));
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
