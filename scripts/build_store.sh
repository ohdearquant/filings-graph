#!/usr/bin/env bash
# Archive the pulled rows, write a store config, load it and reindex: one named generation.
#   scripts/build_store.sh filings-graph-v0
# Refuses to replace an existing archive or store of that name (archive() does too).
set -euo pipefail
# The embedding model is downloaded on first use into ~/.lattice/models, with directories made under this
# shell's umask, and the model loader refuses a file below a group-writable directory. A stock Ubuntu
# account has umask 0002, so set 022 before anything here creates a directory.
umask 022
NAME="${1:?store name, e.g. filings-graph-v0}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$PWD"
source "$ROOT/scripts/writable_ancestors_hint.sh"
# kkernel refuses a store with a group- or world-writable directory above it; under umask 0002 the
# checkout root and .khive/ come out that way, and both are inside this checkout.
mkdir -p "$ROOT/.khive"
chmod g-w,o-w "$ROOT" "$ROOT/.khive" || true
ARCHIVE="data/external/filings-graph/${NAME}.json"
STORE=".khive/${NAME}.db"
CONFIG=".khive/${NAME}.toml"
[ -e "$STORE" ] && { echo "refusing: $STORE exists"; exit 2; }
echo "== archive $(date +%H:%M:%S)"
uv run python -m filings_graph.build archive --out "$ARCHIVE"
echo "== config $(date +%H:%M:%S)"
uv run python -m filings_graph.build config --store "$STORE" --out "$CONFIG"
echo "== load $(date +%H:%M:%S)"
uv run python -m filings_graph.build load --config "$CONFIG" --archive "$ARCHIVE" || { rc=$?; writable_ancestors_hint "$ROOT"; exit "$rc"; }
echo "== done $(date +%H:%M:%S)"
