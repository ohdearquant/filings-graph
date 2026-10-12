"""Tests marked `needs_local_data` read real filings from the local EDGAR cache and the pulled rows
under data/external/ (written by `python -m filings_graph.build pull`), pinned to the pull they were written
against. Without that data they are skipped with the reason below, never passed."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "data/external/filings-graph/pull-v0.2"
REASON = (
    "needs the pinned pull under data/external/filings-graph/pull-v0.2 and the EDGAR cache it was read from"
)


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_local_data: reads the pinned pull and the EDGAR cache")


def pytest_collection_modifyitems(config, items):
    if PINNED.exists():
        return
    skip = pytest.mark.skip(reason=REASON)
    for item in items:
        if "needs_local_data" in item.keywords:
            item.add_marker(skip)
