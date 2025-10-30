use duet::util::io_value::io_value_to_json;
use preserves::IOValue;

#[test]
fn record_conversion_includes_structure_and_summary() {
    let value = IOValue::record(
        IOValue::symbol("agent-response"),
        vec![
            IOValue::new("request-123".to_string()),
            IOValue::new("prompt".to_string()),
            IOValue::new("response".to_string()),
        ],
    );

    let json = io_value_to_json(&value);
    let obj = json.as_object().expect("record renders as object");

    assert_eq!(obj.get("type").and_then(|v| v.as_str()), Some("record"));
    assert_eq!(
        obj.get("label").and_then(|v| v.as_str()),
        Some("agent-response")
    );
    assert_eq!(obj.get("field_count").and_then(|v| v.as_u64()), Some(3));
    assert!(
        obj.get("summary")
            .and_then(|v| v.as_str())
            .expect("summary present")
            .contains("agent-response")
    );

    let fields = obj
        .get("fields")
        .and_then(|v| v.as_array())
        .expect("fields array present");
    assert_eq!(fields.len(), 3);
    assert_eq!(
        fields[0]
            .as_object()
            .unwrap()
            .get("type")
            .and_then(|v| v.as_str()),
        Some("string")
    );
}

#[test]
fn atomic_string_includes_summary() {
    let value = IOValue::new("hello".to_string());
    let json = io_value_to_json(&value);
    let obj = json.as_object().expect("string renders as object");
    assert_eq!(obj.get("type").and_then(|v| v.as_str()), Some("string"));
    assert_eq!(obj.get("value").and_then(|v| v.as_str()), Some("hello"));
    assert_eq!(obj.get("summary").and_then(|v| v.as_str()), Some("hello"));
}
