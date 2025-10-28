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
	cargo build --bin codebased
	uv run --project $(PYTHON_PROJECT) duet status

install:
	@echo "Installing codebased (release build)..."
	cargo install --path .
	@echo "Installing duet CLI (editable mode)..."
	uv tool install --editable $(PYTHON_PROJECT)
	@echo "✓ Both tools installed successfully!"
	@echo "  codebased: $$(which codebased)"
	@echo "  duet:  $$(which duet)"

install-dev:
	@echo "Installing codebased (debug build)..."
	cargo build --bin codebased
	@ln -sf $(PWD)/target/debug/codebased ~/.cargo/bin/codebased
	@echo "Installing duet CLI (editable mode)..."
	uv tool install --editable $(PYTHON_PROJECT)
	@echo "✓ Both tools installed successfully (dev mode)!"
	@echo "  codebased: $$(which codebased) (symlinked to debug build)"
	@echo "  duet:  $$(which duet)"
