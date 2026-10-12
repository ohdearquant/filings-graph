"""Point in time on a beneficial_owner edge keys on the filing date, not the event date
(DESIGN.md "Point in time"). Each case is one edge's metadata as the archive stores it."""

from filings_graph.graph import state_as_of


def obs(pct, as_of, filed, *, form="SCHEDULE 13G", source="xml", accession=None, suspect=False):
    return {
        "pct": pct,
        "as_of": as_of,
        "filed": filed,
        "form": form,
        "source": source,
        "accession": accession or f"acc-{filed}",
        "suspect": suspect,
    }


def edge(observations, **state):
    newest = max(observations, key=lambda o: (o.get("as_of") or "", o.get("filed") or ""))
    md = {
        "control": False,
        "pct": newest.get("pct"),
        "as_of": newest.get("as_of"),
        "filed": newest.get("filed"),
        "form": newest.get("form"),
        "last_asserted": newest.get("as_of"),
        "valid_to": None,
        "exit_filed": None,
        "observations": observations,
    }
    md.update(state)
    return md


def test_a_stake_is_invisible_between_its_event_date_and_its_filing_date():
    md = edge([obs(6.1, "2025-09-30", "2025-11-12")])
    assert state_as_of(md, "2025-10-15") is None
    assert state_as_of(md, "2025-11-12") == {"pct": 6.1, "as_of": "2025-09-30", "form": "SCHEDULE 13G"}


def test_an_amendment_filed_after_the_date_leaves_the_earlier_filing_as_the_state():
    md = edge(
        [
            obs(6.1, "2024-12-31", "2025-02-10", form="SCHEDULE 13G"),
            obs(7.2, "2025-12-31", "2026-02-11", form="SCHEDULE 13G/A"),
        ]
    )
    # the amendment's own as_of (2025-12-31) is before the date, its filing is not
    assert state_as_of(md, "2026-01-15") == {"pct": 6.1, "as_of": "2024-12-31", "form": "SCHEDULE 13G"}
    assert state_as_of(md, "2026-02-11") == {"pct": 7.2, "as_of": "2025-12-31", "form": "SCHEDULE 13G/A"}


def test_an_exit_closes_the_edge_from_its_filing_date_not_its_event_date():
    md = edge(
        [
            obs(8.0, "2024-12-31", "2025-02-10"),
            obs(2.35, "2025-09-30", "2025-11-12", form="SCHEDULE 13G/A"),
        ],
        valid_to="2025-09-30",
        exit_filed="2025-11-12",
        exit_kind="sold",
    )
    assert state_as_of(md, "2025-10-15") == {"pct": 8.0, "as_of": "2024-12-31", "form": "SCHEDULE 13G"}
    assert state_as_of(md, "2025-11-12") is None
    assert state_as_of(md, None) is None


def test_own_filing_outranks_the_proxy_row_as_the_builder_does():
    md = edge(
        [
            obs(7.2, "2025-12-31", "2026-02-11"),
            obs(7.5, "2026-03-02", "2026-03-20", form="DEF 14A", source="proxy-table"),
        ]
    )
    assert state_as_of(md, "2026-04-01")["pct"] == 7.2
    assert state_as_of(md, "2026-03-01")["pct"] == 7.2


def test_only_suspect_rows_visible_is_a_holder_with_no_percent():
    md = edge([obs(76.0, "2026-02-17", "2026-02-27", form="DEF 14A", source="proxy-table", suspect=True)])
    assert state_as_of(md, "2026-03-01") == {"pct": None, "as_of": "2026-02-17", "form": "DEF 14A"}
    assert state_as_of(md, "2026-02-20") is None


def test_no_date_reads_the_stored_current_state():
    md = edge([obs(6.1, "2024-12-31", "2025-02-10"), obs(7.2, "2025-12-31", "2026-02-11")])
    assert state_as_of(md, None) == {"pct": 7.2, "as_of": "2025-12-31", "form": "SCHEDULE 13G"}
