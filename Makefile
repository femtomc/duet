# Root build helpers for Duet (Rust + Python uv workspace)

PYTHON_PROJECT=python/duet

.PHONY: test test-rust test-python fmt fmt-rust cli install install-dev

test: test-rust test-python

test-rust:
	cargo test

test-python:
	uv run --project $(PYTHON_PROJECT) python -m compileall src

fmt: fmt-rust

fmt-rust:
	cargo fmt

cli:
	cargo build --bin duetd
	uv run --project $(PYTHON_PROJECT) python -m duet status

install:
	@echo "Installing duetd (release build)..."
	cargo install --path .
	@echo "Installing duet CLI (editable mode)..."
	uv pip install -e $(PYTHON_PROJECT)
	@echo "✓ Both tools installed successfully!"
	@echo "  duetd: $$(which duetd)"
	@echo "  duet:  $$(which duet)"

install-dev:
	@echo "Installing duetd (debug build)..."
	cargo build --bin duetd
	@ln -sf $(PWD)/target/debug/duetd ~/.cargo/bin/duetd
	@echo "Installing duet CLI (editable mode)..."
	uv pip install -e $(PYTHON_PROJECT)
	@echo "✓ Both tools installed successfully (dev mode)!"
	@echo "  duetd: $$(which duetd) (symlinked to debug build)"
	@echo "  duet:  $$(which duet)"
