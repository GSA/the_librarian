#!/usr/bin/env bash
# Adopter path in miniature: package the toolkit, unzip it, build a fresh venv from the copy,
# initialize empty stores, run preflight and complete an MCP handshake (status, search).
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cd "$repo"
python -m tools.package --output "$work/the-librarian.zip"
python -m zipfile -e "$work/the-librarian.zip" "$work/kit"

cd "$work/kit"
python -m venv .venv
.venv/bin/python -m pip install --quiet ./tools/context-mcp
.venv/bin/python -m tools.initialize
.venv/bin/librarian-preflight --root "$PWD" --verify-start
.venv/bin/python "$repo/.github/scripts/mcp_probe.py" "$PWD"
echo "package smoke test passed"
