"""Entry point for the Duet CLI."""

from .cli import main as _main


if __name__ == "__main__":  # pragma: no cover - exercised manually
    raise SystemExit(_main())
