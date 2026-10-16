import json
from pathlib import Path

import pytest

from filings_graph.build import (
    _ownership_metadata,
    _proxy_same_name_rows,
    archive,
    proxy_legal_name_identity,
    proxy_legal_names_match,
)
from filings_graph.edgar import Edgar
from filings_graph.models import HoldingRow, ProxyHolderRow, Pulled
from filings_graph.proxy import parse_ownership_tables_audited

ROOT = Path(__file__).resolve().parents[1]
PULL_DIR = ROOT / "data/external/filings-graph/pull-v0.2"
PULL_DIR_V03 = ROOT / "data/external/filings-graph/pull-v0.3"
UNIVERSE = ROOT / "data/universe.csv"
PAY_CASES = {
    "0000354190": ("Pat Gallagher", 550.0, "Target Percent of Salary", 5),
    "0000062996": ("Keith J. Allman", 208.2, "Percentage of Base Salary", 6),
    "0001053507": ("Olivier Puech", 157.0, "PSU Award Granted", 4),
    "0000896878": ("Fiscal 2026 Bonus Target", 200.0, "Compensation Decision", 1),
    "0001022079": ("Performance Shares", 50.0, "Component Weight", 2),
    "0000072331": ("Jennifer McDonough", 65.0, "Payout (% of Target)", 5),
}


def _cached_proxy(cik: str) -> str:
    record = json.loads((PULL_DIR / f"{cik}.json").read_text())
    filing = record["proxy"]
    raw = Edgar(offline=True).document(cik, filing["accession"], filing["primary_document"])
    return raw.decode("utf-8", "replace")


def _cached_proxy_v03(cik: str, accession: str) -> str:
    record = json.loads((PULL_DIR_V03 / f"{cik}.json").read_text())
    filing = record["proxy"]
    assert filing["accession"] == accession
    raw = Edgar(offline=True).document(cik, accession, filing["primary_document"])
    return raw.decode("utf-8", "replace")


@pytest.mark.needs_local_data
def test_item_403_tables_are_accepted_from_cached_filings():
    cases = {
        "0000004962": {"Warren Buffett": 22.1, "The Vanguard Group, Inc.": 6.8, "BlackRock, Inc.": 6.4},
        "0000093751": {"BlackRock, Inc.": 8.99},
        "0000048465": {"The Hormel Foundation": 46.62},
    }
    for cik, expected in cases.items():
        rows, _, _ = parse_ownership_tables_audited(_cached_proxy(cik))
        got = {row.name: row.pct for row in rows if row.name in expected}
        assert got == expected
    amex_rows, _, _ = parse_ownership_tables_audited(_cached_proxy("0000004962"))
    assert {row.table for row in amex_rows if row.name in cases["0000004962"]} == {"insiders"}


@pytest.mark.needs_local_data
def test_boeing_names_survive_empty_leading_paragraphs_in_cached_cells():
    rows, _, _ = parse_ownership_tables_audited(_cached_proxy("0000012927"))
    got = {row.name: row.pct for row in rows}
    assert got["The Vanguard Group"] == 9.0
    assert got["FMR LLC"] == 7.0
    assert got["BlackRock, Inc."] == 6.8


@pytest.mark.needs_local_data
def test_brown_forman_ownership_header_after_layout_rows_is_detected():
    rows, _, _ = parse_ownership_tables_audited(_cached_proxy("0000014693"))
    got = {row.name: row.pct for row in rows}
    assert any(pct == 60.3 for pct in got.values())
    assert any(pct == 10.8 for pct in got.values())


@pytest.mark.needs_local_data
def test_eversource_percent_cells_with_trailing_footnotes_are_parsed():
    rows, _, _ = parse_ownership_tables_audited(_cached_proxy("0000072741"))
    got = {row.name: row.pct for row in rows}
    assert got["The Vanguard Group, Inc."] == 12.65
    assert got["BlackRock, Inc."] == 11.38
    assert got["State Street Corporation"] == 7.46


@pytest.mark.needs_local_data
def test_item_403_headers_without_the_word_ownership_keep_cached_holder_rows():
    ford, _, ford_rejected = parse_ownership_tables_audited(_cached_proxy("0000037996"))
    ford_pct = {row.name: row.pct for row in ford}
    assert (
        next(pct for name, pct in ford_pct.items() if name.startswith("State Street Corporation and certain"))
        == 8.41
    )
    assert ford_pct["The Vanguard Group and certain of its affiliates"] == 11.68
    assert not any("Ford Common Stock" in table["header"] for table in ford_rejected)

    aig, _, aig_rejected = parse_ownership_tables_audited(_cached_proxy("0000005272"))
    aig_pct = {row.name: row.pct for row in aig}
    assert aig_pct["BlackRock, Inc."] == 7.7
    assert aig_pct["The Vanguard Group"] == 12.8
    assert not any(
        "Number of Shares" in table["header"] and "Name and Address" in table["header"]
        for table in aig_rejected
    )


@pytest.mark.needs_local_data
def test_five_percent_section_labels_are_accepted_from_cached_filings():
    cases = [
        (
            "0001327567",
            "0001308179-25-000624",
            {"The Vanguard Group": (8.2, 55_137_000), "BlackRock, Inc.": (7.3, 49_244_010)},
        ),
        (
            "0001535527",
            "0001104659-26-055599",
            {"BlackRock, Inc.": (6.70, 16_954_069), "Vanguard Capital Management": (7.27, 18_449_066)},
        ),
        (
            "0001973266",
            "0001193125-26-173833",
            {
                "Endeavor Group Holdings, Inc.": (63.9, 122_142_162),
                "Egon P. Durban": (63.9, 122_142_162),
                "Vincent K. McMahon": (8.6, 6_442_325),
                "BlackRock, Inc.": (5.8, 4_349_551),
                "Morgan Stanley": (5.6, 4_214_167),
            },
        ),
    ]
    for cik, accession, expected in cases:
        rows, _, _ = parse_ownership_tables_audited(_cached_proxy_v03(cik, accession))
        got = {row.name: (row.pct, row.shares) for row in rows}
        for name, value in expected.items():
            assert got.get(name) == value, (cik, name, got.get(name))


@pytest.mark.needs_local_data
def test_first_class_column_is_not_mistaken_for_the_holder():
    rows, _, _ = parse_ownership_tables_audited(_cached_proxy_v03("0000080424", "0001193125-26-372211"))
    got = {row.name: row for row in rows}
    assert got["Vanguard Capital Management"].pct == 7.52
    assert got["Vanguard Capital Management"].shares == 174_810_071
    assert got["Vanguard Capital Management"].class_title == "Common"
    assert got["BlackRock, Inc."].pct == 6.70
    assert got["BlackRock, Inc."].shares == 155_762_299
    assert got["BlackRock, Inc."].class_title == "Common"


@pytest.mark.needs_local_data
def test_title_of_class_caption_does_not_shift_multiclass_owner_rows():
    rows, _, _ = parse_ownership_tables_audited(_cached_proxy("0000352915"))
    got = {row.name: row for row in rows}
    assert got["Alan B. Miller"].pct == 77.8
    assert got["Alan B. Miller"].shares == 5_112_384
    assert got["Marc D. Miller"].pct == 20.4
    assert got["Marc D. Miller"].shares == 1_343_316
    assert got["BlackRock, Inc."].pct == 7.7
    assert got["BlackRock, Inc."].shares == 4_139_531
    assert "5,112,384" not in got


@pytest.mark.needs_local_data
def test_pge_class_column_keeps_common_and_preferred_holders():
    rows, _, _ = parse_ownership_tables_audited(_cached_proxy_v03("0001004980", "0001004980-26-000020"))
    got = {row.name: row for row in rows}
    for name, pct, shares in (
        ("FMR LLC", 6.0, 132_160_040),
        ("Massachusetts Financial Services Company", 5.4, 118_822_231),
        ("State Street Corporation", 5.2, 114_004_068),
    ):
        assert got[name].pct == pct
        assert got[name].shares == shares
        assert got[name].class_title == "PG&E Corporation common stock"
    preferred = got["Stonehill Capital Management LLC, et al."]
    assert preferred.pct == 7.9
    assert preferred.class_title == "Pacific Gas and Electric Company first preferred stock"


@pytest.mark.needs_local_data
def test_synopsys_connector_line_continues_name_and_resolves_blackrock(tmp_path):
    rows, _, _ = parse_ownership_tables_audited(_cached_proxy_v03("0000883241", "0000883241-26-000007"))
    assert any(
        row.name == "Entities associated with BlackRock, Inc." and row.pct == 6.2 and row.shares == 11_788_658
        for row in rows
    )
    built = _archive_with_proxy_name(
        tmp_path,
        "Entities associated with BlackRock, Inc.",
        [_holder_row("BlackRock, Inc.", "0002012383")],
    )
    proxy_edges = [
        edge
        for edge in built["edges"]
        if edge.get("metadata", {}).get("predicate") == "beneficial_owner"
        and any(
            obs.get("source") == "proxy-table" for obs in edge.get("metadata", {}).get("observations", [])
        )
    ]
    assert len(proxy_edges) == 1
    assert proxy_edges[0]["source"] == "urn:sec:cik:0002012383"


@pytest.mark.needs_local_data
def test_percent_without_sign_below_five_is_suspect_when_shares_contradict():
    rows, _, _ = parse_ownership_tables_audited(_cached_proxy_v03("0000874766", "0000874766-26-000025"))
    blackrock = next(row for row in rows if row.name == "BlackRock, Inc.")
    assert blackrock.pct == 0.085
    assert blackrock.suspect
    assert blackrock.suspect_reason == "percent inconsistent with shares in this table"


def test_same_name_rows_in_one_table_keep_different_share_counts():
    raw = """<table>
      <tr><th>Name</th><th>Shares Beneficially Owned</th><th>Percent of Class</th></tr>
      <tr><td>Acme Capital LP</td><td>6,000</td><td>6.0%</td></tr>
      <tr><td>Acme Capital LP</td><td>7,000</td><td>7.0%</td></tr>
    </table>"""
    rows, _, _ = parse_ownership_tables_audited(raw)
    assert [(row.name, row.shares, row.pct) for row in rows] == [
        ("Acme Capital LP", 6_000, 6.0),
        ("Acme Capital LP", 7_000, 7.0),
    ]
    assert _proxy_same_name_rows(rows) == [
        {
            "name": "Acme Capital LP",
            "table": "insiders",
            "class_title": None,
            "rows": [
                {"pct": 6.0, "shares": 6_000, "raw": "Acme Capital LP | 6,000 | 6.0%"},
                {"pct": 7.0, "shares": 7_000, "raw": "Acme Capital LP | 7,000 | 7.0%"},
            ],
        }
    ]


def test_proxy_legal_identity_uses_exact_core_tokens_and_compatible_forms():
    same = [
        ("The Vanguard Group, Inc.", "The Vanguard Group"),
        ("Vanguard Capital Management LLC", "Vanguard Capital Management"),
        ("Norges Bank (The Central Bank of Norway)", "Norges Bank"),
        ("Berkshire Hathaway Inc./Warren E. Buffett", "Berkshire Hathaway Incorporated"),
        ("Wellington Management Group LLP and related entities", "Wellington Management Group LLP"),
        ("FMR LLC/Abigail P. Johnson", "FMR LLC"),
        ("Lilly Endowment Inc. (the Endowment)", "Lilly Endowment Incorporated"),
        ("BlackRock, Inc.⁽³⁾", "BlackRock Incorporated"),
        ("BlackRock , Inc .", "BlackRock, Inc."),
    ]
    for left, right in same:
        assert proxy_legal_names_match(proxy_legal_name_identity(left), proxy_legal_name_identity(right))
    assert not proxy_legal_names_match(
        proxy_legal_name_identity("Artisan Partners Holdings LP"),
        proxy_legal_name_identity("Artisan Partners Limited Partnership"),
    )
    assert not proxy_legal_names_match(
        proxy_legal_name_identity("The Vanguard Group"),
        proxy_legal_name_identity("Vanguard Groupama"),
    )
    assert not proxy_legal_names_match(
        proxy_legal_name_identity("The Vanguard Group, Inc. and certain of its affiliates"),
        proxy_legal_name_identity("The Vanguard Group"),
    )


@pytest.mark.needs_local_data
def test_compensation_tables_are_rejected_whole_from_cached_filings():
    for cik, (name, pct, header_marker, row_count) in PAY_CASES.items():
        rows, _, rejected = parse_ownership_tables_audited(_cached_proxy(cik))
        matching = [table for table in rejected if header_marker.casefold() in table["header"].casefold()]
        assert len(matching) == 1, (cik, name, rejected)
        assert matching[0]["rows"] == row_count
        assert matching[0]["reason"]
        assert not any(row.name.casefold() == name.casefold() and row.pct == pct for row in rows)


def test_ownership_table_with_percent_above_100_is_rejected_whole():
    raw = """<table>
      <tr><th>Name and Address of Beneficial Owner</th><th>Shares Beneficially Owned</th><th>Percent of Class</th></tr>
      <tr><td>Acme Capital LP</td><td>1,500</td><td>150%</td></tr>
    </table>"""
    rows, _, rejected = parse_ownership_tables_audited(raw)
    assert rows == []
    assert rejected == [
        {
            "header": "Name and Address of Beneficial Owner Shares Beneficially Owned Percent of Class",
            "reason": "ownership percentage exceeds 100",
            "rows": 1,
        }
    ]


def test_same_holder_in_both_ownership_blocks_keeps_holders_row_and_conflict():
    raw = """<table>
      <tr><th>Name and Address of Beneficial Owner</th><th>Shares Beneficially Owned</th><th>Percent of Class</th></tr>
      <tr><td>Acme Capital LP 1 Main Street</td><td>6,000</td><td>6.0%</td></tr>
    </table>
    <table>
      <tr><th>Name</th><th>Shares Owned</th><th>Percent of Class</th></tr>
      <tr><td>Acme Capital LP</td><td>6,200</td><td>6.2%</td></tr>
    </table>"""
    rows, _, _ = parse_ownership_tables_audited(raw)
    assert len(rows) == 1
    assert rows[0].table == "holders"
    assert rows[0].pct == 6.0
    assert rows[0].proxy_table_conflict == {
        "percentages": [6.0, 6.2],
        "raw_cells": [
            "Acme Capital LP 1 Main Street | 6,000 | 6.0%",
            "Acme Capital LP | 6,200 | 6.2%",
        ],
    }


def test_outlying_implied_share_count_is_marked_suspect():
    raw = """<table>
      <tr><th>Name and Address of Beneficial Owner</th><th>Shares Beneficially Owned</th><th>Percent of Class</th></tr>
      <tr><td>Acme Capital LP</td><td>5,000,000</td><td>50%</td></tr>
      <tr><td>Beacon Trust</td><td>4,000,000</td><td>40%</td></tr>
      <tr><td>Northstar Fund</td><td>1,000,000</td><td>1%</td></tr>
    </table>"""
    rows, _, _ = parse_ownership_tables_audited(raw)
    suspect = next(row for row in rows if row.name == "Northstar Fund")
    assert suspect.suspect
    assert "differs from table/class median" in suspect.suspect_reason
    assert suspect.raw == "Northstar Fund | 1,000,000 | 1%"


def test_suspect_proxy_row_remains_an_observation_without_setting_state():
    meta, _ = _ownership_metadata(
        [],
        [
            {
                "accession": "0000000001-26-000001",
                "source": "proxy-table",
                "form": "DEF 14A",
                "pct": 7.0,
                "shares": 7_000_000,
                "as_of": "2026-01-01",
                "suspect": True,
                "suspect_reason": "implied count differs by 30%",
            }
        ],
    )
    assert meta["pct"] is None
    assert meta["suspect"]
    assert meta["observations"][0]["suspect"]


@pytest.mark.needs_local_data
def test_builder_emits_the_three_american_express_rows_from_cached_pull(tmp_path):
    archive_path = tmp_path / "filings-graph-v0.3.json"
    archive(PULL_DIR, archive_path, universe_path=UNIVERSE)
    built = json.loads(archive_path.read_text())
    entities = {entity["uri"]: entity for entity in built["entities"]}
    target = "urn:sec:cik:0000004962"
    expected = {"Warren Buffett": 22.1, "BlackRock, Inc.": 6.4}
    found = {}
    for edge in built["edges"]:
        if edge.get("target") != target or edge.get("metadata", {}).get("predicate") != "beneficial_owner":
            continue
        name = entities[edge["source"]]["name"]
        for observation in edge["metadata"].get("observations", []):
            if (
                observation.get("source") == "proxy-table"
                and observation.get("accession") == "0001104659-26-034163"
            ):
                if name in expected:
                    found[name] = (observation["pct"], observation.get("table"))
                if edge["source"] == "urn:sec:cik:0000102909":
                    found["Vanguard CIK"] = (observation["pct"], observation.get("table"))
    assert found == {
        **{name: (pct, "insiders") for name, pct in expected.items()},
        "Vanguard CIK": (6.8, "insiders"),
    }


def _archive_with_proxy_name(tmp_path, proxy_name, owner_rows):
    pulled = Pulled(
        header={"cik": "0000000001", "name": "Example Issuer"},
        universe={},
        ten_k=None,
        holdings=owner_rows,
        proxy_holders=[
            ProxyHolderRow(
                table="holders",
                name=proxy_name,
                pct=7.0,
                shares=700,
                as_of="2026-03-31",
                filed="2026-04-10",
                accession="0000000001-26-000002",
                raw=proxy_name,
            )
        ],
    )
    pull_dir = tmp_path / "pull"
    pull_dir.mkdir()
    (pull_dir / "0000000001.json").write_text(pulled.model_dump_json(indent=1))
    out = tmp_path / "archive.json"
    archive(pull_dir, out, universe_path=UNIVERSE)
    return json.loads(out.read_text())


def _holder_row(name, cik):
    return HoldingRow(
        holder_name=name,
        holder_cik=cik,
        pct=8.0,
        shares=800,
        as_of="2026-03-31",
        form="SC 13G/A",
        accession=f"{cik}-26-000001",
        filed="2026-04-10",
        parsed_from="xml",
    )


def test_proxy_name_resolution_does_not_match_a_different_legal_name(tmp_path):
    built = _archive_with_proxy_name(
        tmp_path,
        "Artisan Partners Holdings LP",
        [_holder_row("Artisan Partners Limited Partnership", "0001466153")],
    )
    entities = {entity["uri"]: entity for entity in built["entities"]}
    proxy_edges = [
        edge
        for edge in built["edges"]
        if edge.get("metadata", {}).get("predicate") == "beneficial_owner"
        and any(obs.get("source") == "proxy-table" for obs in edge["metadata"].get("observations", []))
    ]
    assert len(proxy_edges) == 1
    assert proxy_edges[0]["source"] != "urn:sec:cik:0001466153"
    assert entities[proxy_edges[0]["source"]]["name"] == "Artisan Partners Holdings LP"


def test_proxy_name_resolution_keeps_qualifier_and_footnote_cleaning(tmp_path):
    built = _archive_with_proxy_name(
        tmp_path,
        "Artisan Partners Holdings LP and its affiliates (a)",
        [
            _holder_row("Artisan Partners Limited Partnership", "0001466153"),
            _holder_row("Artisan Partners Holdings LP", "0001661234"),
        ],
    )
    proxy_edges = [
        edge
        for edge in built["edges"]
        if edge.get("metadata", {}).get("predicate") == "beneficial_owner"
        and any(obs.get("source") == "proxy-table" for obs in edge["metadata"].get("observations", []))
    ]
    assert len(proxy_edges) == 1
    assert proxy_edges[0]["source"] == "urn:sec:cik:0001661234"
