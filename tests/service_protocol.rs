use duet::runtime::RuntimeConfig;
use duet::runtime::control::Control;
use duet::runtime::registry::EntityRegistry;
use duet::runtime::service::Service;
use preserves::IOValue;
use serde_json::{Value, json};
use std::cell::RefCell;
use std::io::{self, Cursor, Write};
use std::rc::Rc;
use tempfile::TempDir;
use uuid::Uuid;

use duet::runtime::actor::{Activation, Entity};
use duet::runtime::error::ActorResult;

#[test]
fn service_handles_basic_commands() {
    // Register a simple entity type for the registry.
    EntityRegistry::global().register("service-test", |_config| Ok(Box::new(SimpleEntity)));

    let temp = TempDir::new().unwrap();
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    // Initialise storage
    Control::init(config.clone()).unwrap();
    let control = Control::new(config).unwrap();

    let sink = Rc::new(RefCell::new(Vec::<u8>::new()));
    let writer = SharedWriter(sink.clone());
    let mut service = Service::new(control, writer);

    let actor = Uuid::new_v4();
    let facet = Uuid::new_v4();

    let requests = vec![
        json!({"id": 1, "command": "status", "params": {}}),
        json!({"id": 2, "command": "handshake", "params": {"client": "test", "protocol_version": duet::PROTOCOL_VERSION}}),
        json!({"id": 3, "command": "status", "params": {}}),
        json!({"id": 4, "command": "register_entity", "params": {
            "actor": actor.to_string(),
            "facet": facet.to_string(),
            "entity_type": "service-test",
            "config": "nil"
        }}),
        json!({"id": 5, "command": "list_entities", "params": {}}),
        json!({"id": 6, "command": "list_entities", "params": {"actor": actor.to_string()}}),
        json!({"id": 7, "command": "list_capabilities", "params": {}}),
        json!({"id": 8, "command": "send_message", "params": {
            "target": {"actor": actor.to_string(), "facet": facet.to_string()},
            "payload": "nil"
        }}),
        json!({"id": 9, "command": "history", "params": {"branch": "main", "start": 0, "limit": 10}}),
        json!({"id": 10, "command": "noop", "params": {}}),
    ];

    let input_data = requests
        .into_iter()
        .map(|req| serde_json::to_string(&req).unwrap())
        .collect::<Vec<_>>()
        .join("\n");

    let reader = Cursor::new(format!("{}\n", input_data));
    service.run(reader).unwrap();

    let output = sink.borrow();
    let lines: Vec<_> = output
        .split(|b| *b == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice::<Value>(line).unwrap())
        .collect();

    assert_eq!(lines.len(), 10);

    assert_eq!(lines[0]["error"]["code"], "protocol_error");
    assert!(lines[1]["result"].is_object());
    assert!(lines[2]["result"].is_object());
    assert!(lines[3]["result"].get("entity_id").is_some());
    assert_eq!(lines[4]["result"]["entities"].as_array().unwrap().len(), 1);
    assert_eq!(lines[5]["result"]["entities"].as_array().unwrap().len(), 1);
    assert!(lines[6]["result"]["capabilities"].is_array());
    assert!(
        lines[6]["result"]["capabilities"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(lines[7]["result"].get("queued_turn").is_some());
    assert!(lines[8]["result"]["turns"].as_array().unwrap().len() >= 1);
    assert_eq!(lines[9]["error"]["code"], "unsupported_command");
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
