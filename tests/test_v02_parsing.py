import json
from datetime import date
from pathlib import Path

import pytest

from filings_graph.build import (
    _class_key,
    _event_items,
    _own_observation,
    _ownership_metadata,
    _proxy_observation,
    archive,
    parse_13_xml,
    proxy_ownership_date,
)
from filings_graph.models import FilingRow, HoldingRow, ProxyHolderRow, Pulled
from filings_graph.proxy import parse_ownership_tables_detailed

ROOT = Path(__file__).resolve().parents[1]
PULL_DIR = ROOT / "data/external/filings-graph/pull-v0.2"
UNIVERSE = ROOT / "data/universe.csv"


def _cached_pull(cik: str) -> Pulled:
    return Pulled.model_validate_json((PULL_DIR / f"{cik}.json").read_text())


def _archive_pulls(tmp_path: Path, pulls: list[Pulled]) -> dict:
    pull_dir = tmp_path / "pull"
    pull_dir.mkdir(parents=True)
    for index, pulled in enumerate(pulls):
        (pull_dir / f"{index:04}.json").write_text(pulled.model_dump_json(indent=1))
    out = tmp_path / "archive.json"
    archive(pull_dir, out, universe_path=UNIVERSE)
    return json.loads(out.read_text())


def test_proxy_table_holder_names_resolve_to_ciks_and_reject_descriptions(tmp_path):
    cases = [
        ("BlackRock, Inc. (b)", "BlackRock, Inc.", "0002012383"),
        (
            "The Vanguard Group, Inc. and certain of its affiliates",
            "The Vanguard Group, Inc.",
            "0000102909",
        ),
        ("State Street Corporation (d)", "State Street Corporation", "0000093751"),
    ]
    own = [
        HoldingRow(
            holder_name=filing_name,
            holder_cik=cik,
            pct=8.0,
            shares=1000,
            as_of="2026-06-30",
            form="SC 13G/A",
            accession=f"{cik}-26-000001",
            filed="2026-07-31",
            class_title="Common Stock",
            parsed_from="xml",
        )
        for _, filing_name, cik in cases
    ]
    proxies = [
        ProxyHolderRow(
            table="holders",
            name=name,
            pct=9.0,
            shares=1100,
            as_of="2026-05-18",
            filed="2026-05-28",
            accession=f"0000000001-26-{index:06d}",
            raw=name,
        )
        for index, (name, _, _) in enumerate(cases, start=1)
    ]
    proxies.append(
        ProxyHolderRow(
            table="holders",
            name="Vanguard subsidiaries or business divisions",
            pct=6.0,
            shares=700,
            accession="0000000001-26-000004",
            raw="Vanguard subsidiaries or business divisions",
        )
    )
    issuer_stake = HoldingRow(
        holder_name="Example Corp",
        holder_cik="0000000001",
        issuer_cik="0002012383",
        issuer_name="BlackRock, Inc.",
        pct=6.0,
        shares=600,
        as_of="2026-01-01",
        form="SC 13G/A",
        accession="0000000001-26-000010",
        filed="2026-02-01",
        parsed_from="xml",
    )
    pulled = Pulled(
        header={"cik": "0000000001", "name": "Example Corp"},
        universe={},
        ten_k=None,
        proxy=None,
        holdings=own,
        stakes=[issuer_stake],
        proxy_holders=proxies,
    )

    archived = _archive_pulls(tmp_path / "proxy-names", [pulled])
    entities = {entity["uri"]: entity for entity in archived["entities"]}
    edges = [
        edge for edge in archived["edges"] if edge.get("metadata", {}).get("predicate") == "beneficial_owner"
    ]
    strict_affiliate_name = "The Vanguard Group, Inc. and certain of its affiliates"
    for name, _, cik in cases:
        if name == strict_affiliate_name:
            continue  # the closed qualifier list deliberately does not strip "certain of its affiliates"
        uri = f"urn:sec:cik:{cik}"
        assert name in entities[uri]["properties"]["aliases"]
        holder_edges = [edge for edge in edges if edge["source"] == uri]
        assert len(holder_edges) == 1
        assert any(obs["source"] == "proxy-table" for obs in holder_edges[0]["metadata"]["observations"])
        assert holder_edges[0]["metadata"]["pct"] == 8.0
        assert holder_edges[0]["metadata"]["proxy_disagrees"] == {"pct": 9.0, "as_of": "2026-05-18"}

    strict_uri = next(
        edge["source"]
        for edge in edges
        if any(
            observation.get("source") == "proxy-table"
            and observation.get("accession") == "0000000001-26-000002"
            for observation in edge["metadata"].get("observations", [])
        )
    )
    assert strict_uri != "urn:sec:cik:0000102909"
    assert entities[strict_uri]["name"] == strict_affiliate_name

    company = entities["urn:sec:cik:0000000001"]
    rejected = company["properties"]["proxy_rejected"]
    assert any(
        row["name"] == "Vanguard subsidiaries or business divisions"
        and row["reason"] == "name is a description, not a holder"
        for row in rejected
    )
    assert not any(edge["source"].endswith("vanguard.subsidiaries.or.business.divisions") for edge in edges)


def test_13_xml_preserves_class_cusip_all_types_and_decimal_shares():
    row = FilingRow(form="SC 13G/A", filed="2026-06-30", accession="0000000001-26-000001")
    purpose = "p" * 620
    raw = f"""<ownershipDocument>
      <cik>0000000001</cik><securitiesClassTitle>Class A Common Stock</securitiesClassTitle>
      <issuerCusipNumber>123456789</issuerCusipNumber><eventDateRequiresFilingThisStatement>06/29/2026</eventDateRequiresFilingThisStatement>
      <coverPageHeaderReportingPersonDetails>
        <reportingPersonName>Holder LLC</reportingPersonName>
        <reportingPersonCIK>0000000002</reportingPersonCIK>
        <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>1.00</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
        <classPercent>5.00</classPercent><typeOfReportingPerson>CO</typeOfReportingPerson>
        <typeOfReportingPerson>IN</typeOfReportingPerson>
      </coverPageHeaderReportingPersonDetails><formData><items><item5>
        <classOwnership5PercentOrLess>Y</classOwnership5PercentOrLess>
      </item5></items></formData><transactionPurpose>{purpose}</transactionPurpose>
    </ownershipDocument>""".encode()

    holding = parse_13_xml(raw, row)[0]

    assert holding.class_title == "Class A Common Stock"
    assert holding.cusip == "123456789"
    assert holding.shares == 1
    assert holding.reporting_person_types == ["CO", "IN"]
    assert holding.five_pct_or_less is True
    assert holding.purpose == purpose


def test_proxy_parser_keeps_name_strips_footnote_and_rejects_address():
    raw = """<table><tr><th>Name and Address of Beneficial Owner</th><th>Shares</th><th>Percent of Class</th></tr>
      <tr><td>Vanguard Group, Inc. (13)<br>100 Vanguard Blvd. Malvern, PA 19355</td><td>1,000,000</td><td>10.0%</td></tr>
      <tr><td>100 E. Pratt Street Baltimore, MD 21202</td><td>900,000</td><td>9.0%</td></tr></table>"""

    accepted, rejected = parse_ownership_tables_detailed(raw)

    assert [(r.name, r.pct) for r in accepted] == [("Vanguard Group, Inc.", 10.0)]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "name contains ZIP code"


def test_proxy_ownership_section_heading_classifies_mixed_table_as_holders():
    raw = """<p>All stockholders known to the company to be the beneficial owners of more than 5% of common stock.</p>
      <table><tr><th>Name</th><th>Shares</th><th>Percent of Class</th></tr>
      <tr><td>Acme Investments</td><td>10,000</td><td>6%</td></tr></table>"""

    parsed, _ = parse_ownership_tables_detailed(raw)

    assert [(r.name, r.table, r.pct) for r in parsed] == [("Acme Investments", "holders", 6.0)]


def test_own_filing_state_beats_newer_proxy_and_keeps_both_observations():
    own = HoldingRow(
        holder_name="The Vanguard Group",
        holder_cik="0000102909",
        pct=0,
        shares=0,
        as_of="2026-01-12",
        filed="2026-03-13",
        form="SC 13G/A",
        accession="0000102909-26-000001",
        class_title="Common Stock",
        parsed_from="xml",
    )
    proxy = ProxyHolderRow(
        table="holders",
        name="The Vanguard Group",
        pct=8.0,
        shares=10_000,
        as_of="2026-02-01",
        as_of_source="table",
        filed="2026-03-01",
        class_title="Common Stock",
        accession="0000000001-26-000002",
        raw="row",
    )

    metadata, _ = _ownership_metadata([_own_observation(own)], [_proxy_observation(proxy)])

    assert metadata["pct"] == 0
    assert metadata["form"] == "SC 13G/A"
    assert metadata["valid_to"] == "2026-01-12"
    assert metadata["exit_filed"] == "2026-03-13"
    assert metadata["proxy_disagrees"] == {"pct": 8.0, "as_of": "2026-02-01"}
    assert {o["source"] for o in metadata["observations"]} == {"xml", "proxy-table"}


def test_exact_five_percent_flag_is_an_exit_and_classes_stay_distinct():
    own = HoldingRow(
        holder_name="State Street",
        holder_cik="0000093751",
        pct=5,
        shares=1000,
        as_of="2026-06-30",
        filed="2026-08-01",
        form="SC 13G/A",
        accession="0000093751-26-000051",
        class_title="Class A Common Stock",
        five_pct_or_less=True,
        parsed_from="xml",
    )

    metadata, _ = _ownership_metadata([_own_observation(own)], [])

    assert metadata["valid_to"] == "2026-06-30"
    assert metadata["exit_filed"] == "2026-08-01"
    assert metadata["exit_kind"] == "threshold_flag"
    assert _class_key("Class A Common Stock") != _class_key("Class B Common Stock")
    assert _class_key("Common Stock", "65249B109") != _class_key("Common Stock", "65249B208")
    assert _class_key("Class A Common Stock", "093712107") == _class_key("Common Stock", "093712107")
    assert _class_key("Common stock, $1.00 par value") == _class_key("Common stock, par value $1.00")


def test_proxy_date_uses_nearby_table_heading_and_filing_fallback():
    raw = """<p>Retirement plan information as of December 31, 2021</p>
      <table><tr><th>Plan</th><th>Percent</th></tr><tr><td>Plan A</td><td>6%</td></tr></table>
      <p>Beneficial owners of more than 5% of our common stock as of March 15, 2026</p>
      <table><tr><th>Name and Address of Beneficial Owner</th><th>Shares</th><th>Percent of Class</th></tr>
      <tr><td>Acme Investments</td><td>10,000</td><td>6%</td></tr></table>"""

    assert proxy_ownership_date(raw, "2026-04-01") == ("2026-03-15", "table")
    assert proxy_ownership_date("<html><table><tr><td>revenue</td></tr></table></html>", "2026-04-01") == (
        "2026-04-01",
        "filed",
    )


def test_proxy_date_uses_record_date_instead_of_holder_footnote_date():
    raw = """<p>Director ages as of February 17, 2026 (the Record Date).</p>
      <p>Security ownership of certain beneficial owners as of the Record Date</p>
      <table><tr><th>Name of Beneficial Owner</th><th>Shares</th><th>Percent of Class</th></tr>
      <tr><td>Acme Investments</td><td>10,000</td><td>6%</td></tr>
      <tr><td>Vanguard Group</td><td>20,000</td><td>7%</td></tr>
      <tr><td colspan="3">Based on a filing reporting beneficial ownership as of December 29, 2023.</td></tr></table>"""

    assert proxy_ownership_date(raw, "2026-03-01") == ("2026-02-17", "record_date")


def test_event_items_are_sorted_strings():
    assert _event_items("5.02, 1.01") == ["1.01", "5.02"]


@pytest.mark.needs_local_data
def test_cached_abbott_and_ameriprise_classes_coalesce_and_dedupe(tmp_path):
    abbott = _cached_pull("0000001800")
    own = [h for h in abbott.holdings if h.accession == "0000102909-26-000500"]
    proxy = [r for r in abbott.proxy_holders if r.name == "The Vanguard Group"]
    assert len(own) == len(proxy) == 1
    abbott.holdings, abbott.proxy_holders = own, proxy
    abbott.stakes, abbott.exhibit21, abbott.eight_ks = [], [], []
    abbott.ten_k = abbott.proxy = None

    archived = _archive_pulls(tmp_path / "abbott", [abbott])
    edges = [
        e
        for e in archived["edges"]
        if e.get("metadata", {}).get("predicate") == "beneficial_owner"
        and e["target"] == "urn:sec:cik:0000001800"
        and archived_entity_name(archived, e["source"]) == "The Vanguard Group"
    ]
    assert len(edges) == 1
    assert edges[0]["metadata"]["class"] == "Common Stock"
    assert edges[0]["metadata"]["valid_to"] == "2026-03-13"
    assert {o["source"] for o in edges[0]["metadata"]["observations"]} == {"xml", "proxy-table"}

    state_street = _cached_pull("0000093751")
    ameriprise = _cached_pull("0000820027")
    header_copy = [r for r in state_street.stakes if r.accession == "0000093751-26-000205"]
    xml_copy = [r for r in ameriprise.holdings if r.accession == "0000093751-26-000205"]
    assert len(header_copy) == len(xml_copy) == 1
    state_street.stakes, ameriprise.holdings = header_copy, xml_copy
    state_street.holdings = state_street.proxy_holders = state_street.exhibit21 = state_street.eight_ks = []
    ameriprise.stakes = ameriprise.proxy_holders = ameriprise.exhibit21 = ameriprise.eight_ks = []
    state_street.ten_k = state_street.proxy = ameriprise.ten_k = ameriprise.proxy = None

    archived = _archive_pulls(tmp_path / "ameriprise", [state_street, ameriprise])
    edges = [
        e
        for e in archived["edges"]
        if e.get("metadata", {}).get("predicate") == "beneficial_owner"
        and e["target"] == "urn:sec:cik:0000820027"
        and archived_entity_name(archived, e["source"]).upper().startswith("STATE STREET")
    ]
    assert len(edges) == 1
    observations = edges[0]["metadata"]["observations"]
    assert len([o for o in observations if o["accession"] == "0000093751-26-000205"]) == 1
    assert observations[0]["source"] == "xml"
    assert edges[0]["metadata"]["class"] == "COMMON STOCK"


def archived_entity_name(archive_data: dict, uri: str) -> str:
    return next(entity["name"] for entity in archive_data["entities"] if entity["uri"] == uri)


@pytest.mark.needs_local_data
def test_cached_huffman_distinct_names_under_one_cik_split_identity_and_keep_state(tmp_path):
    reddit = _cached_pull("0001713445")
    accessions = {"0001104659-25-105772", "0001827011-26-000020"}
    huffman = [h for h in reddit.holdings if h.accession in accessions]
    assert len(huffman) == 2
    reddit.holdings = huffman
    reddit.stakes = reddit.proxy_holders = reddit.exhibit21 = reddit.eight_ks = []
    reddit.ten_k = reddit.proxy = None

    archived = _archive_pulls(tmp_path, [reddit])
    expected = {"urn:sec:cik:0001827011": "Steve Ladd Huffman"}
    entities = {e["uri"]: e for e in archived["entities"]}
    assert {uri for uri in expected if uri in entities} == set(expected)
    assert all(entities[uri]["name"] == name for uri, name in expected.items())
    assert entities["urn:sec:cik:0001827011"]["properties"]["aliases"] == ["Steven Huffman"]
    edges = [
        e
        for e in archived["edges"]
        if e.get("metadata", {}).get("predicate") == "beneficial_owner"
        and e["source"] == "urn:sec:cik:0001827011"
    ]
    assert len(edges) == 1
    assert edges[0]["metadata"]["pct"] == 24.8
    assert edges[0]["metadata"]["as_of"] == "2026-05-04"
    assert edges[0]["metadata"]["accession"] == "0001827011-26-000020"
    assert len(edges[0]["metadata"]["observations"]) == 2

    berkley = _cached_pull("0000011544")
    berkley_accessions = {
        "0001193125-25-316616",
        "0001193125-26-092603",
        "0001193125-26-310624",
    }
    berkley.holdings = [
        h
        for h in berkley.holdings
        if h.accession in berkley_accessions
        and h.holder_name.lower().removeprefix("the ") in {"w. robert berkley, jr.", "william r. berkley"}
    ]
    assert len(berkley.holdings) == 5
    berkley.stakes = berkley.proxy_holders = berkley.exhibit21 = berkley.eight_ks = []
    berkley.ten_k = berkley.proxy = None
    berkley_archive = _archive_pulls(tmp_path / "berkley", [berkley])
    for name, cik in (
        ("W. Robert Berkley, Jr.", "0001236253"),
        ("William R. Berkley", "0000918186"),
    ):
        matching = [
            e
            for e in berkley_archive["edges"]
            if e.get("metadata", {}).get("predicate") == "beneficial_owner"
            and e["target"] == "urn:sec:cik:0000011544"
            and archived_entity_name(berkley_archive, e["source"]).lower() == name.lower()
        ]
        assert {e["source"] for e in matching} == {f"urn:sec:cik:{cik}"}


@pytest.mark.needs_local_data
def test_cached_hormel_filing_item5_flag_closes_state_street_edge():
    raw_path = (
        ROOT / "data/external/edgar/www.sec.gov/Archives/edgar/data/48465/000009375126000051/primary_doc.xml"
    )
    row = FilingRow(form="SC 13G/A", filed="2026-02-09", accession="0000093751-26-000051")
    holding = next(h for h in parse_13_xml(raw_path.read_bytes(), row) if h.holder_cik == "0000093751")

    assert holding.pct == 5.0
    assert holding.five_pct_or_less is True
    metadata, _ = _ownership_metadata([_own_observation(holding)], [])
    assert metadata["valid_to"] == "2025-12-31"
    assert metadata["exit_filed"] == "2026-02-09"


@pytest.mark.needs_local_data
def test_cached_jpmorgan_proxy_table_date_source_and_floor():
    raw_path = (
        ROOT / "data/external/edgar/www.sec.gov/Archives/edgar/data/19617/000001961726000096/jpm-20260402.htm"
    )
    as_of, source = proxy_ownership_date(raw_path.read_text(errors="replace"), "2026-04-02")
    assert (as_of, source) == ("2025-12-31", "table")
    assert (date.fromisoformat("2026-04-02") - date.fromisoformat(as_of)).days <= 400


@pytest.mark.needs_local_data
def test_cached_proxy_record_date_older_than_400_days_falls_back_to_filing_date():
    raw_path = (
        ROOT
        / "data/external/edgar/www.sec.gov/Archives/edgar/data/912595/000114036126013212/ny20062832x1_def14a.htm"
    )
    as_of, source = proxy_ownership_date(raw_path.read_text(errors="replace"), "2026-04-06")

    assert (as_of, source) == ("2026-04-06", "filed")
