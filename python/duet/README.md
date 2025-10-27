# Duet CLI

A lightweight command-line interface for the Duet runtime. The CLI speaks the
runtime's newline-delimited JSON control protocol via the `duetd` daemon and
prints results with Rich, making it ideal for automation or agent-based smoke
tests.

Usage:

```bash
cargo build --bin duetd
uv run --project python/duet python -m duet status
```

Available subcommands include `status`, `history`, `send`, `register-entity`,
`list-entities`, `goto`, `back`, `fork`, `merge`, and `raw`.

Set `DUETD_BIN` to point at a custom runtime binary if needed; otherwise the CLI
tries to launch `target/debug/duetd` (falling back to `duetd` on `PATH`).
