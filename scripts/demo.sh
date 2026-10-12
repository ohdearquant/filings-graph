#!/usr/bin/env bash
# One command: read the latest filings of a few companies from SEC EDGAR, build the graph archive, load it
# into a local khive store and ask it who holds each company. Needs uv, Rust (builds the khive binary once)
# and network access to sec.gov.
#   FILINGS_GRAPH_CONTACT=you@example.com bash scripts/demo.sh [number of companies, default 3]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/writable_ancestors_hint.sh"

# kkernel refuses to serve from a store whose path has a group- or world-writable directory above it.
# A clone made under umask 0002 leaves the checkout root and .khive/ (where the store goes) group-writable;
# both are inside this checkout, so set them here. If a directory above the checkout is the one at fault,
# the hint printed on failure names it.
mkdir -p "$ROOT/.khive"
chmod g-w,o-w "$ROOT" "$ROOT/.khive" || true

: "${FILINGS_GRAPH_CONTACT:?set FILINGS_GRAPH_CONTACT to an email address you read: sec.gov asks every automated client to declare one}"
N="${1:-3}"
NAME="demo-$(date +%Y%m%d-%H%M%S)"
KKERNEL_REV=5ccfedad5c084b8026e6605a07aa280c7c8450cd

export FILINGS_GRAPH_KKERNEL="${FILINGS_GRAPH_KKERNEL:-$ROOT/.kkernel/bin/kkernel}"
if [ ! -x "$FILINGS_GRAPH_KKERNEL" ]; then
  cargo install --locked --git https://github.com/ohdearquant/khive --rev "$KKERNEL_REV" --root "$ROOT/.kkernel" kkernel
fi

uv run python -m filings_graph.build all --name "$NAME" --limit "$N" || { rc=$?; writable_ancestors_hint "$ROOT"; exit "$rc"; }

TICKER="$(awk -F, 'NR==2 {print $2}' data/universe.csv)"
uv run python scripts/ask.py --config ".khive/${NAME}.toml" company "$TICKER" || { rc=$?; writable_ancestors_hint "$ROOT"; exit "$rc"; }
