use duet::runtime::service_client::{
    DataspaceEventsRequest, HistoryRequest, ServiceClient, StatusRequest, TranscriptTailRequest,
};
use duet::runtime::turn::BranchId;
use std::ffi::OsString;
use std::path::PathBuf;
use tempfile::TempDir;

#[test]
fn service_client_handshake_and_status() {
    let temp = TempDir::new().expect("tempdir");
    let mut exe_dir = std::env::current_exe().expect("current exe");
    exe_dir.pop(); // deps/
    exe_dir.pop(); // debug/
    let binary_name = binary_name();
    let codebased_path = exe_dir.join(binary_name);
    assert!(
        codebased_path.exists(),
        "codebased binary not found at {}",
        codebased_path.display()
    );

    let mut command: Vec<OsString> = vec![OsString::from(codebased_path)];
    command.push(OsString::from("--root"));
    command.push(temp.path().as_os_str().to_owned());
    command.push(OsString::from("--stdio"));

    let mut client =
        ServiceClient::connect_stdio(command.into_iter(), "service-client-test").expect("connect");

    let branches = client.list_branches().expect("branches");
    assert!(
        branches
            .iter()
            .any(|branch| branch.name == BranchId::main()),
        "main branch should be present"
    );

    let handshake = client.handshake().cloned().expect("handshake");
    assert_eq!(handshake.protocol_version, duet::PROTOCOL_VERSION);
    assert_eq!(handshake.client_name, "service-client-test");
    assert!(
        handshake.features.iter().any(|f| f == "status"),
        "expected status feature"
    );
    let control_locator = handshake
        .control_interpreter
        .as_ref()
        .expect("control interpreter advertised");
    assert!(
        !control_locator.actor.is_empty(),
        "control interpreter should include actor id"
    );
    assert!(
        !control_locator.facet.is_empty(),
        "control interpreter should include facet id"
    );

    let status = client
        .status(StatusRequest::default())
        .expect("status response");
    assert_eq!(status.active_branch, BranchId::main());

    let history = client
        .history(HistoryRequest::default())
        .expect("history response");
    assert!(
        history.len() <= 20,
        "default history limit should be respected"
    );

    let dataspace = client
        .dataspace_events(DataspaceEventsRequest::default())
        .expect("dataspace events");
    for batch in &dataspace.events {
        assert!(
            !batch.turn.is_empty(),
            "dataspace event batch turn should not be empty"
        );
        assert!(
            !batch.actor.is_empty(),
            "dataspace event batch actor should not be empty"
        );
    }

    let transcript = client
        .transcript_tail(TranscriptTailRequest {
            request_id: "test-request".to_string(),
            ..Default::default()
        })
        .expect("transcript tail");
    assert_eq!(transcript.request_id, "test-request");
    assert_eq!(transcript.branch, BranchId::main());

    drop(client);
}

fn binary_name() -> PathBuf {
    #[cfg(windows)]
    {
        PathBuf::from("codebased.exe")
    }
    #[cfg(not(windows))]
    {
        PathBuf::from("codebased")
    }
}
