//! Control-plane client for the Duet NDJSON service.
//!
//! This module provides a small, synchronous client that speaks the same
//! newline-delimited JSON protocol as the `codebased` daemon. It is intended to
//! be reused by any frontend (CLI, GUI, tests) that needs to drive the runtime.

use crate::PROTOCOL_VERSION;
use crate::runtime::control::{BranchInfo, RuntimeStatus, TurnSummary};
use crate::runtime::turn::{BranchId, TurnId};
use chrono::{DateTime, FixedOffset};
use serde::Serialize;
use serde_json::{Value, json};
use std::ffi::OsStr;
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use thiserror::Error;

/// Errors produced by the [`ServiceClient`].
#[derive(Debug, Error)]
pub enum ClientError {
    /// I/O error while communicating with the runtime.
    #[error("io error: {0}")]
    Io(#[from] io::Error),
    /// JSON (de)serialisation error for envelopes.
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    /// The runtime reported a structured protocol error.
    #[error("protocol error: {0}")]
    Protocol(ProtocolError),
    /// Attempted to spawn the runtime without a command.
    #[error("runtime command is empty")]
    EmptyRuntimeCommand,
    /// Spawned runtime is missing a stdout pipe.
    #[error("spawned runtime process did not expose stdout")]
    MissingStdout,
    /// Spawned runtime is missing a stdin pipe.
    #[error("spawned runtime process did not expose stdin")]
    MissingStdin,
    /// Commands were issued before completing the handshake.
    #[error("handshake has not completed")]
    HandshakeNotCompleted,
    /// The service returned an unexpected or malformed payload.
    #[error("malformed response: {0}")]
    MalformedResponse(String),
}

/// Structured protocol error surfaced by the service.
#[derive(Debug, Clone, Error)]
#[error("{message}")]
pub struct ProtocolError {
    /// Optional service-defined error code.
    pub code: Option<String>,
    /// Human-readable error message.
    pub message: String,
    /// Arbitrary structured details.
    pub details: Value,
}

/// Response returned by the `handshake` command.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HandshakeInfo {
    /// Protocol version agreed between client and service.
    pub protocol_version: String,
    /// Runtime version reported by the service.
    pub runtime_version: String,
    /// Client identifier echoed by the service.
    pub client_name: String,
    /// List of feature flags exposed by the runtime.
    pub features: Vec<String>,
}

/// Parameters accepted by the `status` command.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StatusRequest {
    /// Branch identifier to switch to before reporting status.
    pub branch: Option<String>,
}

/// Parameters accepted by the `history` command.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HistoryRequest {
    /// Branch identifier to query (defaults to `main`).
    pub branch: Option<String>,
    /// Starting offset within the branch history.
    pub start: Option<u64>,
    /// Maximum number of turns to return.
    pub limit: Option<u64>,
}

/// Parameters accepted by the `dataspace_events` command.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DataspaceEventsRequest {
    /// Branch identifier to query (defaults to `main`).
    pub branch: Option<String>,
    /// Cursor specifying the turn to resume from (inclusive).
    pub since: Option<String>,
    /// Maximum number of batches to return.
    pub limit: Option<u64>,
    /// Restrict results to a specific actor.
    pub actor: Option<String>,
    /// Filter events by outer record label.
    pub label: Option<String>,
    /// Filter events associated with an agent request ID.
    pub request_id: Option<String>,
    /// Restrict results to the listed event kinds (`assert`/`retract`).
    pub event_types: Vec<String>,
    /// Wait for additional events up to the provided duration (milliseconds).
    pub wait_ms: Option<u64>,
}

/// Result payload returned by the `dataspace_events` command.
#[derive(Debug, Clone, PartialEq)]
pub struct DataspaceEventsResult {
    /// Event batches grouped by turn.
    pub events: Vec<DataspaceEventBatch>,
    /// Cursor pointing to the next batch, if additional events are available.
    pub next_cursor: Option<String>,
    /// Turn identifier representing the branch head after applying the query.
    pub head: Option<String>,
    /// Indicates whether more results can be fetched with the returned cursor.
    pub has_more: bool,
}

/// Batch of dataspace assertion events that occurred within a single turn.
#[derive(Debug, Clone, PartialEq)]
pub struct DataspaceEventBatch {
    /// Turn identifier for this batch.
    pub turn: String,
    /// Actor identifier that produced the events.
    pub actor: String,
    /// Summary metadata about the actor.
    pub actor_info: Value,
    /// Logical clock for the turn.
    pub clock: u64,
    /// Timestamp when the turn executed.
    pub timestamp: DateTime<FixedOffset>,
    /// Events triggered during the turn.
    pub events: Vec<DataspaceEvent>,
}

/// Action describing assertion changes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DataspaceEventAction {
    /// A new assertion entered the dataspace.
    Assert,
    /// An assertion was retracted from the dataspace.
    Retract,
}

/// Individual assertion event.
#[derive(Debug, Clone, PartialEq)]
pub struct DataspaceEvent {
    /// Action describing whether the assertion was added or removed.
    pub action: DataspaceEventAction,
    /// Dataspace handle for the assertion record.
    pub handle: String,
    /// Structured representation of the assertion value.
    pub value_structured: Option<Value>,
    /// Human-readable summary of the assertion payload.
    pub summary: Option<String>,
    /// Optional transcript metadata when the event relates to agent activity.
    pub transcript: Option<EventTranscript>,
}

/// Parsed transcript information for agent responses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventTranscript {
    /// Identifier for the originating request.
    pub request_id: String,
    /// Prompt that produced the agent response.
    pub prompt: Option<String>,
    /// Agent response text.
    pub response: Option<String>,
    /// Human-friendly agent name.
    pub agent: Option<String>,
    /// Entity identifier for the agent, if provided.
    pub agent_id: Option<String>,
    /// Timestamp when the agent completed the response, if available.
    pub response_timestamp: Option<String>,
    /// Optional agent role (e.g. planner/implementer).
    pub role: Option<String>,
    /// Optional tool name when the response used a tool capability.
    pub tool: Option<String>,
}

/// Parameters accepted by the `transcript_tail` command.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TranscriptTailRequest {
    /// Identifier for the agent request being tailed (mandatory).
    pub request_id: String,
    /// Branch identifier to follow.
    pub branch: Option<String>,
    /// Cursor specifying the turn to resume from (inclusive).
    pub since: Option<String>,
    /// Maximum number of event batches to return.
    pub limit: Option<u64>,
    /// Wait duration in milliseconds for additional events.
    pub wait_ms: Option<u64>,
}

/// Result payload returned by the `transcript_tail` command.
#[derive(Debug, Clone, PartialEq)]
pub struct TranscriptTailResult {
    /// Request identifier echoed from the service.
    pub request_id: String,
    /// Branch identifier associated with the returned events.
    pub branch: BranchId,
    /// Event batches grouped by turn.
    pub events: Vec<TranscriptEventBatch>,
    /// Cursor pointing to the next batch, if additional events are available.
    pub next_cursor: Option<TurnId>,
    /// Turn identifier representing the branch head after applying the query.
    pub head: Option<TurnId>,
    /// Indicates whether more results can be fetched with the returned cursor.
    pub has_more: bool,
}

/// Batch of transcript assertion events that occurred within a single turn.
#[derive(Debug, Clone, PartialEq)]
pub struct TranscriptEventBatch {
    /// Turn identifier for this batch.
    pub turn: TurnId,
    /// Actor identifier that produced the events.
    pub actor: String,
    /// Logical clock for the turn.
    pub clock: u64,
    /// Timestamp when the turn executed.
    pub timestamp: DateTime<FixedOffset>,
    /// Events triggered during the turn.
    pub events: Vec<TranscriptEvent>,
}

/// Individual transcript event.
#[derive(Debug, Clone, PartialEq)]
pub struct TranscriptEvent {
    /// Action describing whether the assertion was added or removed.
    pub action: DataspaceEventAction,
    /// Dataspace handle for the assertion record.
    pub handle: String,
    /// Structured representation of the assertion value.
    pub value_structured: Option<Value>,
    /// Human-readable summary of the assertion payload.
    pub summary: Option<String>,
    /// Optional transcript metadata when the event relates to agent activity.
    pub transcript: Option<EventTranscript>,
}

impl DataspaceEventsRequest {
    fn into_value(self) -> Value {
        let mut map = serde_json::Map::new();
        if let Some(branch) = self.branch {
            map.insert("branch".to_string(), Value::String(branch));
        }
        if let Some(since) = self.since {
            map.insert("since".to_string(), Value::String(since));
        }
        if let Some(limit) = self.limit {
            map.insert("limit".to_string(), Value::Number(limit.into()));
        }
        if let Some(actor) = self.actor {
            map.insert("actor".to_string(), Value::String(actor));
        }
        if let Some(label) = self.label {
            map.insert("label".to_string(), Value::String(label));
        }
        if let Some(request_id) = self.request_id {
            map.insert("request_id".to_string(), Value::String(request_id));
        }
        if !self.event_types.is_empty() {
            map.insert(
                "event_types".to_string(),
                Value::Array(self.event_types.into_iter().map(Value::String).collect()),
            );
        }
        if let Some(wait_ms) = self.wait_ms {
            map.insert("wait_ms".to_string(), Value::Number(wait_ms.into()));
        }

        Value::Object(map)
    }
}

impl StatusRequest {
    fn into_value(self) -> Value {
        let mut map = serde_json::Map::new();
        if let Some(branch) = self.branch {
            map.insert("branch".to_string(), Value::String(branch));
        }
        Value::Object(map)
    }
}

impl HistoryRequest {
    fn into_value(self) -> Value {
        let mut map = serde_json::Map::new();
        if let Some(branch) = self.branch {
            map.insert("branch".to_string(), Value::String(branch));
        }
        if let Some(start) = self.start {
            map.insert("start".to_string(), Value::Number(start.into()));
        }
        if let Some(limit) = self.limit {
            map.insert("limit".to_string(), Value::Number(limit.into()));
        }
        Value::Object(map)
    }
}

impl TranscriptTailRequest {
    fn into_value(self) -> Value {
        let mut map = serde_json::Map::new();
        map.insert("request_id".to_string(), Value::String(self.request_id));
        if let Some(branch) = self.branch {
            map.insert("branch".to_string(), Value::String(branch));
        }
        if let Some(since) = self.since {
            map.insert("since".to_string(), Value::String(since));
        }
        if let Some(limit) = self.limit {
            map.insert("limit".to_string(), Value::Number(limit.into()));
        }
        if let Some(wait_ms) = self.wait_ms {
            map.insert("wait_ms".to_string(), Value::Number(wait_ms.into()));
        }
        Value::Object(map)
    }
}

/// Synchronous client for the Duet control-plane service.
///
/// The client communicates over one of two transports:
///   * Spawned `codebased` process via stdin/stdout pipes
///   * TCP socket to an already running service
pub struct ServiceClient {
    transport: Transport,
    next_request_id: u64,
    handshake: Option<HandshakeInfo>,
}

enum Transport {
    Process {
        child: Child,
        reader: BufReader<ChildStdout>,
        writer: BufWriter<ChildStdin>,
    },
    Tcp {
        reader: BufReader<TcpStream>,
        writer: BufWriter<TcpStream>,
    },
}

impl ServiceClient {
    /// Fetch runtime status via a typed request/response.
    pub fn status(&mut self, request: StatusRequest) -> Result<RuntimeStatus, ClientError> {
        let response = self.call("status", request.into_value())?;
        serde_json::from_value(response).map_err(ClientError::from)
    }

    /// Fetch branch history with typed turn summaries.
    pub fn history(&mut self, request: HistoryRequest) -> Result<Vec<TurnSummary>, ClientError> {
        let response = self.call("history", request.into_value())?;
        let turns_value = response
            .get("turns")
            .cloned()
            .unwrap_or(Value::Array(vec![]));
        serde_json::from_value(turns_value).map_err(ClientError::from)
    }

    /// List branches known to the runtime.
    pub fn list_branches(&mut self) -> Result<Vec<BranchInfo>, ClientError> {
        let response = self.call("list_branches", Value::Object(serde_json::Map::new()))?;
        let branches_value = response
            .get("branches")
            .cloned()
            .unwrap_or(Value::Array(vec![]));
        serde_json::from_value(branches_value).map_err(ClientError::from)
    }

    /// Connect to a service by spawning a `codebased` command and performing the handshake.
    pub fn connect_stdio<I, S>(mut command: I, client_name: &str) -> Result<Self, ClientError>
    where
        I: Iterator<Item = S>,
        S: AsRef<OsStr>,
    {
        let program = command.next().ok_or(ClientError::EmptyRuntimeCommand)?;
        let mut child = Command::new(program);
        child.stdin(Stdio::piped());
        child.stdout(Stdio::piped());
        child.stderr(Stdio::inherit());
        for arg in command {
            child.arg(arg);
        }

        let mut child = child.spawn()?;
        let stdout = child.stdout.take().ok_or(ClientError::MissingStdout)?;
        let stdin = child.stdin.take().ok_or(ClientError::MissingStdin)?;

        let transport = Transport::Process {
            reader: BufReader::new(stdout),
            writer: BufWriter::new(stdin),
            child,
        };

        let mut client = ServiceClient {
            transport,
            next_request_id: 1,
            handshake: None,
        };

        let handshake = client.perform_handshake(client_name)?;
        client.handshake = Some(handshake);
        Ok(client)
    }

    /// Connect to a service listening on a TCP socket and perform the handshake.
    pub fn connect_tcp<A>(addr: A, client_name: &str) -> Result<Self, ClientError>
    where
        A: ToSocketAddrs,
    {
        let mut last_err = None;
        for candidate in addr.to_socket_addrs()? {
            match TcpStream::connect(candidate) {
                Ok(stream) => {
                    stream.set_nodelay(true).ok();
                    let reader = BufReader::new(stream.try_clone()?);
                    let writer = BufWriter::new(stream);
                    let transport = Transport::Tcp { reader, writer };

                    let mut client = ServiceClient {
                        transport,
                        next_request_id: 1,
                        handshake: None,
                    };

                    let handshake = client.perform_handshake(client_name)?;
                    client.handshake = Some(handshake);
                    return Ok(client);
                }
                Err(err) => last_err = Some(err),
            }
        }

        Err(ClientError::Io(last_err.unwrap_or_else(|| {
            io::Error::new(io::ErrorKind::Other, "no address resolved")
        })))
    }

    /// Return the handshake details negotiated with the service.
    pub fn handshake(&self) -> Option<&HandshakeInfo> {
        self.handshake.as_ref()
    }

    /// Issue a command against the service.
    pub fn call<P>(&mut self, command: &str, params: P) -> Result<Value, ClientError>
    where
        P: Serialize,
    {
        if self.handshake.is_none() {
            return Err(ClientError::HandshakeNotCompleted);
        }

        let params_value = serde_json::to_value(params)?;
        self.send_request(command, params_value)
    }

    /// Fetch assertion events from the dataspace using typed request/response structures.
    pub fn dataspace_events(
        &mut self,
        request: DataspaceEventsRequest,
    ) -> Result<DataspaceEventsResult, ClientError> {
        let params = request.into_value();
        let response = self.call("dataspace_events", params)?;
        parse_dataspace_events_response(response)
    }

    /// Tail agent transcript events using typed request/response structures.
    pub fn transcript_tail(
        &mut self,
        request: TranscriptTailRequest,
    ) -> Result<TranscriptTailResult, ClientError> {
        let response = self.call("transcript_tail", request.into_value())?;
        parse_transcript_tail_response(response)
    }

    fn perform_handshake(&mut self, client_name: &str) -> Result<HandshakeInfo, ClientError> {
        let response = self.send_request(
            "handshake",
            json!({
                "client": client_name,
                "protocol_version": PROTOCOL_VERSION,
            }),
        )?;

        let protocol_version = response
            .get("protocol_version")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("handshake missing protocol_version".into())
            })?
            .to_owned();

        let runtime = response.get("runtime").ok_or_else(|| {
            ClientError::MalformedResponse("handshake missing runtime object".into())
        })?;

        let runtime_version = runtime
            .get("version")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("handshake missing runtime.version".into())
            })?
            .to_owned();

        let echoed_client = runtime
            .get("client")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("handshake missing runtime.client".into())
            })?
            .to_owned();

        let features = runtime
            .get("features")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                ClientError::MalformedResponse("handshake missing runtime.features".into())
            })?
            .iter()
            .filter_map(Value::as_str)
            .map(String::from)
            .collect();

        Ok(HandshakeInfo {
            protocol_version,
            runtime_version,
            client_name: echoed_client,
            features,
        })
    }

    fn send_request(&mut self, command: &str, params: Value) -> Result<Value, ClientError> {
        let request_id = self.next_request_id;
        self.next_request_id += 1;

        let envelope = json!({
            "id": request_id,
            "command": command,
            "params": params,
        });

        let mut payload = serde_json::to_vec(&envelope)?;
        payload.push(b'\n');
        self.transport.write_all(&payload)?;

        let line = self.transport.read_line()?;
        let response: Value = serde_json::from_slice(&line)?;
        let response_id = response
            .get("id")
            .and_then(Value::as_u64)
            .ok_or_else(|| ClientError::MalformedResponse("response missing id".into()))?;

        if response_id != request_id {
            return Err(ClientError::MalformedResponse(format!(
                "response id mismatch (expected {request_id}, got {response_id})"
            )));
        }

        if let Some(error) = response.get("error") {
            let code = error.get("code").and_then(Value::as_str).map(String::from);
            let message = error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("unknown service error")
                .to_owned();
            let details = error.get("details").cloned().unwrap_or(Value::Null);
            return Err(ClientError::Protocol(ProtocolError {
                code,
                message,
                details,
            }));
        }

        match response.get("result") {
            Some(result) => Ok(result.clone()),
            None => Ok(Value::Null),
        }
    }
}

impl Transport {
    fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        match self {
            Transport::Process { writer, .. } => {
                writer.write_all(buf)?;
                writer.flush()
            }
            Transport::Tcp { writer, .. } => {
                writer.write_all(buf)?;
                writer.flush()
            }
        }
    }

    fn read_line(&mut self) -> io::Result<Vec<u8>> {
        let mut buffer = Vec::with_capacity(256);
        let bytes = match self {
            Transport::Process { reader, .. } => reader.read_until(b'\n', &mut buffer)?,
            Transport::Tcp { reader, .. } => reader.read_until(b'\n', &mut buffer)?,
        };

        if bytes == 0 {
            Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "connection closed by runtime",
            ))
        } else {
            Ok(buffer)
        }
    }
}

impl Drop for ServiceClient {
    fn drop(&mut self) {
        if let Transport::Process { child, .. } = &mut self.transport {
            // Attempt a graceful shutdown; ignore errors because we're in Drop.
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn parse_dataspace_events_response(value: Value) -> Result<DataspaceEventsResult, ClientError> {
    let obj = value.as_object().ok_or_else(|| {
        ClientError::MalformedResponse("dataspace_events result must be object".into())
    })?;

    let events_value = obj.get("events").and_then(Value::as_array).ok_or_else(|| {
        ClientError::MalformedResponse("dataspace_events result missing events array".into())
    })?;

    let mut batches = Vec::with_capacity(events_value.len());
    for batch_val in events_value {
        let batch_obj = batch_val.as_object().ok_or_else(|| {
            ClientError::MalformedResponse("dataspace_events batch is not an object".into())
        })?;

        let turn = batch_obj
            .get("turn")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("dataspace_events batch missing turn".into())
            })?
            .to_owned();

        let actor = batch_obj
            .get("actor")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("dataspace_events batch missing actor".into())
            })?
            .to_owned();

        let actor_info = batch_obj.get("actor_info").cloned().unwrap_or(Value::Null);

        let clock = batch_obj
            .get("clock")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                ClientError::MalformedResponse("dataspace_events batch missing clock".into())
            })?;

        let timestamp_str = batch_obj
            .get("timestamp")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("dataspace_events batch missing timestamp".into())
            })?;
        let timestamp = DateTime::parse_from_rfc3339(timestamp_str).map_err(|err| {
            ClientError::MalformedResponse(format!("dataspace_events timestamp parse error: {err}"))
        })?;

        let event_values = batch_obj
            .get("events")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                ClientError::MalformedResponse("dataspace_events batch missing events array".into())
            })?;

        let mut parsed_events = Vec::with_capacity(event_values.len());
        for event_val in event_values {
            let event_obj = event_val.as_object().ok_or_else(|| {
                ClientError::MalformedResponse("dataspace_events event is not an object".into())
            })?;

            let action_str = event_obj
                .get("action")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    ClientError::MalformedResponse("dataspace_events event missing action".into())
                })?;
            let action = match action_str {
                "assert" => DataspaceEventAction::Assert,
                "retract" => DataspaceEventAction::Retract,
                other => {
                    return Err(ClientError::MalformedResponse(format!(
                        "dataspace_events unknown action {other}"
                    )));
                }
            };

            let handle = event_obj
                .get("handle")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    ClientError::MalformedResponse("dataspace_events event missing handle".into())
                })?
                .to_owned();

            let value_structured = event_obj.get("value_structured").cloned();
            let summary = event_obj
                .get("summary")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);

            let transcript = event_obj
                .get("transcript")
                .and_then(Value::as_object)
                .map(parse_event_transcript)
                .transpose()?;

            parsed_events.push(DataspaceEvent {
                action,
                handle,
                value_structured,
                summary,
                transcript,
            });
        }

        batches.push(DataspaceEventBatch {
            turn,
            actor,
            actor_info,
            clock,
            timestamp,
            events: parsed_events,
        });
    }

    let next_cursor = obj
        .get("next_cursor")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let head = obj
        .get("head")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let has_more = obj
        .get("has_more")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    Ok(DataspaceEventsResult {
        events: batches,
        next_cursor,
        head,
        has_more,
    })
}

fn parse_event_transcript(
    transcript_obj: &serde_json::Map<String, Value>,
) -> Result<EventTranscript, ClientError> {
    let request_id = transcript_obj
        .get("request_id")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            ClientError::MalformedResponse("dataspace_events transcript missing request_id".into())
        })?
        .to_owned();
    let prompt = transcript_obj
        .get("prompt")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let response = transcript_obj
        .get("response")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let agent = transcript_obj
        .get("agent")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let agent_id = transcript_obj
        .get("agent_id")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let response_timestamp = transcript_obj
        .get("response_timestamp")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let role = transcript_obj
        .get("role")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let tool = transcript_obj
        .get("tool")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);

    Ok(EventTranscript {
        request_id,
        prompt,
        response,
        agent,
        agent_id,
        response_timestamp,
        role,
        tool,
    })
}

fn parse_transcript_tail_response(value: Value) -> Result<TranscriptTailResult, ClientError> {
    let obj = value.as_object().ok_or_else(|| {
        ClientError::MalformedResponse("transcript_tail result must be object".into())
    })?;

    let request_id = obj
        .get("request_id")
        .and_then(Value::as_str)
        .ok_or_else(|| ClientError::MalformedResponse("transcript_tail missing request_id".into()))?
        .to_owned();

    let branch = obj
        .get("branch")
        .and_then(Value::as_str)
        .ok_or_else(|| ClientError::MalformedResponse("transcript_tail missing branch".into()))
        .map(|s| BranchId::new(s.to_string()))?;

    let events_value = obj.get("events").and_then(Value::as_array).ok_or_else(|| {
        ClientError::MalformedResponse("transcript_tail missing events array".into())
    })?;

    let mut batches = Vec::with_capacity(events_value.len());
    for batch_val in events_value {
        let batch_obj = batch_val.as_object().ok_or_else(|| {
            ClientError::MalformedResponse("transcript_tail batch is not an object".into())
        })?;

        let turn_str = batch_obj
            .get("turn")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("transcript_tail batch missing turn".into())
            })?;
        let turn = TurnId::new(turn_str.to_string());

        let actor = batch_obj
            .get("actor")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("transcript_tail batch missing actor".into())
            })?
            .to_owned();

        let clock = batch_obj
            .get("clock")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                ClientError::MalformedResponse("transcript_tail batch missing clock".into())
            })?;

        let timestamp_str = batch_obj
            .get("timestamp")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ClientError::MalformedResponse("transcript_tail batch missing timestamp".into())
            })?;
        let timestamp = DateTime::parse_from_rfc3339(timestamp_str).map_err(|err| {
            ClientError::MalformedResponse(format!("transcript_tail timestamp parse error: {err}"))
        })?;

        let event_values = batch_obj
            .get("events")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                ClientError::MalformedResponse("transcript_tail batch missing events array".into())
            })?;

        let mut events = Vec::with_capacity(event_values.len());
        for event_val in event_values {
            let event_obj = event_val.as_object().ok_or_else(|| {
                ClientError::MalformedResponse("transcript_tail event is not an object".into())
            })?;

            let action_str = event_obj
                .get("action")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    ClientError::MalformedResponse("transcript_tail event missing action".into())
                })?;
            let action = match action_str {
                "assert" => DataspaceEventAction::Assert,
                "retract" => DataspaceEventAction::Retract,
                other => {
                    return Err(ClientError::MalformedResponse(format!(
                        "transcript_tail unknown action {other}"
                    )));
                }
            };

            let handle = event_obj
                .get("handle")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    ClientError::MalformedResponse("transcript_tail event missing handle".into())
                })?
                .to_owned();

            let value_structured = event_obj.get("value_structured").cloned();
            let summary = event_obj
                .get("summary")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            let transcript = event_obj
                .get("transcript")
                .and_then(Value::as_object)
                .map(parse_event_transcript)
                .transpose()?;

            events.push(TranscriptEvent {
                action,
                handle,
                value_structured,
                summary,
                transcript,
            });
        }

        batches.push(TranscriptEventBatch {
            turn,
            actor,
            clock,
            timestamp,
            events,
        });
    }

    let next_cursor = obj
        .get("next_cursor")
        .and_then(Value::as_str)
        .map(|s| TurnId::new(s.to_string()));
    let head = obj
        .get("head")
        .and_then(Value::as_str)
        .map(|s| TurnId::new(s.to_string()));
    let has_more = obj
        .get("has_more")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    Ok(TranscriptTailResult {
        request_id,
        branch,
        events: batches,
        next_cursor,
        head,
        has_more,
    })
}
