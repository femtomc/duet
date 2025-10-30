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

The CLI organises commands into logical groups; run `duet --help` for a
complete overview. Frequently used entry points include:

- `duet status`, `duet history`, `duet raw` (runtime inspection)
- `duet workspace read|write|scan` (workspace data)
- `duet agent invoke|responses|chat` (Claude agent workflows)
- `duet dataspace assertions|tail` and `duet transcript show|tail`
- `duet daemon start|status|stop` (lifecycle management)

Install shell completions with `duet --install-completion` to make command
discovery easier.

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
