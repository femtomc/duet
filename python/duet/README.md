# Duet CLI

A lightweight command-line interface for the Duet runtime. The CLI speaks the
runtime's newline-delimited JSON control protocol via the `codebased` daemon and
prints results with Rich, making it ideal for automation or agent-based smoke
tests.

## Quick start

```bash
cargo build --bin codebased                 # build the runtime daemon
uv run --project python/duet python -m duet status
```

The CLI organises commands into logical groups; run `duet` (or `duet --help`) for a
complete overview. Frequently used entry points include:

- `duet status` (runtime heartbeat)
- `duet workspace read|write|scan` (workspace data)
- `duet agent chat|responses` (Claude agent workflows)
  - `duet agent chat --wait-for-response "prompt"`
  - `duet agent chat --continue --wait-for-response "follow-up"`
  - `duet agent responses --select`
- `duet transcript show|tail`
- `duet dataspace tail`
- `duet query workflows`
- `duet run workflow-start --interactive examples/workflows/...`
- `duet daemon start|status|stop` (local daemon lifecycle)

When you need the full identifiers behind the truncated request ids, run
`duet debug agent-requests` to list them with timestamps and prompt previews.

Install shell completions with `duet --install-completion` to make command
discovery easier.

Advanced runtime and automation commands (history inspection, raw RPCs,
reaction management, etc.) live under the `duet debug ...` namespace so the
main help stays focused on everyday workflows.

Interactive selectors are available for request-centric commands: omit the
request id (for `duet transcript …`) or pass `--select`/`--resume-select` to
pick from recent agent conversations without copying UUIDs.

## Connecting to an existing daemon

To attach the CLI to a daemon that is already running, supply both
`--daemon-host` and `--daemon-port`:

```bash
duet --daemon-host 10.0.0.12 --daemon-port 9999 status
```

Without these flags the CLI auto-discovers a local daemon using the nearest
`.duet/daemon.json`. Override the storage location with `--root PATH` if you
want to pin a specific runtime directory.

If you have a custom runtime binary, point the CLI at it with
`--codebased-bin /path/to/codebased` (the legacy `CODEBASED_BIN` and
`DUETD_BIN` environment variables are also honoured). Otherwise the CLI
launches `target/debug/codebased` and falls back to `codebased` on `PATH`.
