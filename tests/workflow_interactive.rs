use std::io::Cursor;
use std::io::{self, Write};

use duet::PROTOCOL_VERSION;
use duet::runtime::RuntimeConfig;
use duet::runtime::control::Control;
use duet::runtime::service::Service;
use serde_json::{Value, json};
use tempfile::TempDir;

#[derive(Default)]
struct VecWriter {
    buffer: Vec<u8>,
}

impl VecWriter {
    fn into_inner(self) -> Vec<u8> {
        self.buffer
    }
}

impl Write for VecWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.buffer.extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn call_service(service: &mut Service, request: Value) -> Value {
    let command = request
        .get("command")
        .and_then(|value| value.as_str())
        .expect("request must include command name");

    let mut writer = VecWriter::default();
    let mut payload = Vec::new();

    if command != "handshake" {
        let handshake = json!({
            "id": 0,
            "command": "handshake",
            "params": {
                "client": "test",
                "protocol_version": PROTOCOL_VERSION,
            },
        });
        let mut handshake_bytes = serde_json::to_vec(&handshake).expect("serialize handshake");
        handshake_bytes.push(b'\n');
        payload.extend(handshake_bytes);
    }

    let mut request_bytes = serde_json::to_vec(&request).expect("serialize request");
    request_bytes.push(b'\n');
    payload.extend(request_bytes);

    let reader = Cursor::new(payload);
    service.handle(reader, &mut writer).expect("service call");

    let bytes = writer.into_inner();
    let text = String::from_utf8(bytes).expect("utf-8 response");
    let mut responses: Vec<Value> = text
        .lines()
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_str(line).expect("parse response"))
        .collect();

    assert!(
        !responses.is_empty(),
        "service should produce at least one response"
    );

    responses.pop().expect("response for request")
}

#[test]
fn workflow_user_input_roundtrip() {
    let temp = TempDir::new().expect("temp dir");
    let config = RuntimeConfig {
        root: temp.path().to_path_buf(),
        snapshot_interval: 5,
        flow_control_limit: 100,
        debug: false,
    };

    Control::init(config.clone()).expect("initialize control storage");
    let control = Control::new(config).expect("create control");
    let mut service = Service::new(control);

    let handshake = call_service(
        &mut service,
        json!({
            "id": 1,
            "command": "handshake",
            "params": {
                "client": "test",
                "protocol_version": PROTOCOL_VERSION,
            },
        }),
    );
    let handshake_result = handshake["result"].as_object().expect("handshake success");
    assert_eq!(
        handshake_result["protocol_version"].as_str(),
        Some(PROTOCOL_VERSION)
    );

    let definition = r#"(workflow user-input-demo)
(state start
  (await (user-input :prompt (record prompt "Say something")))
  (action (assert (record captured (last-wait-field 3))))
  (goto done))
(state done (terminal))
"#;

    let start = call_service(
        &mut service,
        json!({
            "id": 2,
            "command": "workflow_start",
            "params": {
                "definition": definition,
                "definition_path": "user-input-demo.duet",
            },
        }),
    );
    let start_result = start
        .get("result")
        .and_then(|value| value.as_object())
        .unwrap_or_else(|| panic!("workflow start failed: {start:?}"));
    assert_eq!(
        start_result.get("status").and_then(|value| value.as_str()),
        Some("started"),
        "workflow start should report started status"
    );
    let instance_id = start_result
        .get("instance")
        .and_then(|value| value.as_object())
        .and_then(|instance| instance.get("id"))
        .and_then(|value| value.as_str())
        .map(|id| id.to_string())
        .or_else(|| {
            let list = call_service(
                &mut service,
                json!({
                    "id": 3,
                    "command": "workflow_list",
                    "params": {},
                }),
            );
            let instances = list["result"]["instances"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            instances
                .iter()
                .find_map(|instance| {
                    instance
                        .get("program_name")
                        .and_then(|value| value.as_str())
                        .filter(|name| *name == "user-input-demo")
                        .and_then(|_| instance.get("id"))
                        .and_then(|value| value.as_str())
                        .map(|id| id.to_string())
                })
                .or_else(|| {
                    instances.first().and_then(|instance| {
                        instance
                            .get("id")
                            .and_then(|value| value.as_str())
                            .map(|id| id.to_string())
                    })
                })
        })
        .expect("workflow instance id to be discoverable");

    let follow_waiting = call_service(
        &mut service,
        json!({
            "id": 4,
            "command": "workflow_follow",
            "params": { "instance_id": instance_id.clone() },
        }),
    );
    let follow_result = follow_waiting["result"].as_object().expect("follow result");
    assert_eq!(
        follow_result["instance"]["status"]["state"].as_str(),
        Some("waiting")
    );
    let prompts = follow_result["prompts"].as_array().expect("prompts array");
    assert_eq!(prompts.len(), 1, "expected a single prompt entry");
    let prompt = prompts[0].as_object().expect("prompt object");
    let request_id = prompt["request_id"]
        .as_str()
        .expect("request id")
        .to_string();
    let prompt_payload = prompt["prompt"]
        .as_object()
        .expect("structured prompt payload");
    let prompt_fields = prompt_payload["fields"]
        .as_array()
        .expect("prompt fields array");
    assert!(
        prompt_fields
            .first()
            .and_then(|field| field.get("value"))
            .and_then(|value| value.as_str())
            .map(|text| text.contains("Say something"))
            .unwrap_or(false),
        "prompt payload should include prompt text"
    );

    let response_text = "hello runtime";
    let submit = call_service(
        &mut service,
        json!({
            "id": 5,
            "command": "workflow_input",
            "params": {
                "instance_id": instance_id.clone(),
                "request_id": request_id.clone(),
                "response": response_text,
            },
        }),
    );
    assert_eq!(
        submit["result"]["status"].as_str(),
        Some("ok"),
        "input submission should succeed"
    );

    let follow_completed = call_service(
        &mut service,
        json!({
            "id": 6,
            "command": "workflow_follow",
            "params": { "instance_id": instance_id.clone() },
        }),
    );
    let completed_result = follow_completed["result"]
        .as_object()
        .expect("follow result");
    assert_eq!(
        completed_result["instance"]["status"]["state"].as_str(),
        Some("completed"),
        "workflow should complete after receiving input"
    );
    assert!(
        completed_result["prompts"]
            .as_array()
            .map(|arr| arr.is_empty())
            .unwrap_or(false),
        "prompts should be empty after response is consumed"
    );

    let prompt_assertions = call_service(
        &mut service,
        json!({
            "id": 7,
            "command": "dataspace_assertions",
            "params": {
                "label": "captured",
                "limit": 1,
            },
        }),
    );
    let prompt_entries = prompt_assertions["result"]["assertions"]
        .as_array()
        .expect("assertions array");
    assert!(
        !prompt_entries.is_empty(),
        "captured assertion should exist in dataspace"
    );

    let response_assertions = call_service(
        &mut service,
        json!({
            "id": 8,
            "command": "dataspace_assertions",
            "params": {
                "label": "interpreter-input-response",
                "request_id": request_id,
                "limit": 1,
            },
        }),
    );
    let response_entries = response_assertions["result"]["assertions"]
        .as_array()
        .expect("response assertion array");
    assert!(
        !response_entries.is_empty(),
        "response record should be present for the request"
    );
    let response_structured = response_entries[0]["value_structured"]
        .as_object()
        .expect("structured response payload");
    let response_fields = response_structured["fields"]
        .as_array()
        .expect("response fields");
    let response_value = response_fields
        .get(3)
        .and_then(|field| field.get("value"))
        .and_then(|value| value.as_str());
    assert_eq!(
        response_value,
        Some(response_text),
        "response record should include submitted text"
    );
}
