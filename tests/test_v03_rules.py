import json
import re
from pathlib import Path

import pytest

from filings_graph.build import archive, parse_13_xml
from filings_graph.models import FilingRow
from filings_graph.proxy import parse_ownership_tables_detailed

ROOT = Path(__file__).resolve().parents[1]
PULL_DIR = ROOT / "data/external/filings-graph/pull-v0.2"
UNIVERSE = ROOT / "data/universe.csv"
TYSON_XML = (
    ROOT / "data/external/edgar/www.sec.gov/Archives/edgar/data/100493/000199937126003264/primary_doc.xml"
)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _entities(archive_data: dict) -> dict[str, dict]:
    return {entity["uri"]: entity for entity in archive_data["entities"]}


def _owner_edges(archive_data: dict) -> list[dict]:
    return [
        edge
        for edge in archive_data["edges"]
        if edge.get("metadata", {}).get("predicate") == "beneficial_owner"
    ]


def _names(entity: dict) -> set[str]:
    return {entity.get("name", ""), *entity.get("properties", {}).get("aliases", [])} - {""}


def _matches(entities: dict[str, dict], uri: str, fragment: str) -> bool:
    return any(_norm(fragment) in _norm(name) for name in _names(entities.get(uri, {})))


def _matches_canonical(entities: dict[str, dict], uri: str, fragment: str) -> bool:
    return _norm(fragment) in _norm(entities.get(uri, {}).get("name", ""))


def _company_matches(entities: dict[str, dict], uri: str, fragment: str) -> bool:
    entity = entities.get(uri, {})
    text = " ".join([entity.get("name", ""), *entity.get("properties", {}).get("tickers", [])])
    return _norm(fragment) in _norm(text)


@pytest.fixture(scope="module")
def cached_archive(tmp_path_factory):
    if not PULL_DIR.exists():
        pytest.skip(
            "needs the pinned pull under data/external/filings-graph/pull-v0.2 and the EDGAR cache it was read from"
        )
    out_dir = tmp_path_factory.mktemp("v03-cached-archive")
    out = out_dir / "archive.json"
    archive(PULL_DIR, out, universe_path=UNIVERSE)
    return json.loads(out.read_text())


def test_lineage_omission_closes_six_dropped_members(cached_archive):
    entities = _entities(cached_archive)
    wanted = [
        ("wellington management company", "PPG"),
        ("wellington management company", "Darden"),
        ("wellington management company", "Dominion"),
        ("wellington management company", "Charles River"),
        ("Franklin Advisers", "Albemarle"),
        ("First Eagle Global Fund", "C. H. Robinson"),
    ]
    found = []
    for holder, company in wanted:
        matching = [
            edge
            for edge in _owner_edges(cached_archive)
            if _matches(entities, edge["source"], holder)
            and _company_matches(entities, edge["target"], company)
            and any(
                observation.get("source") != "proxy-table"
                for observation in edge["metadata"].get("observations", [])
            )
        ]
        assert len(matching) == 1, (holder, company, len(matching))
        edge = matching[0]
        assert edge["metadata"]["exit_kind"] == "departed"
        assert edge["metadata"]["valid_to"]
        found.append((holder, company))
    assert len(found) == 6


def test_cached_wellington_ppg_is_departed(cached_archive):
    entities = _entities(cached_archive)
    rows = [
        edge
        for edge in _owner_edges(cached_archive)
        if _norm(entities[edge["source"]]["name"]) == "wellington management company llp"
        and _company_matches(entities, edge["target"], "PPG")
    ]
    assert len(rows) == 1
    assert rows[0]["metadata"]["exit_kind"] == "departed"


def test_cached_brian_chesky_remains_live_at_13_6_percent(cached_archive):
    entities = _entities(cached_archive)
    rows = [
        edge
        for edge in _owner_edges(cached_archive)
        if _matches(entities, edge["source"], "Brian Chesky")
        and _company_matches(entities, edge["target"], "Airbnb")
    ]
    assert len(rows) == 1
    assert rows[0]["metadata"]["valid_to"] is None
    assert rows[0]["metadata"]["pct"] == 13.6


def test_departed_rows_are_the_thirteen_filer_lineage_exits(cached_archive):
    entities = _entities(cached_archive)
    expected = [
        ("Wellington Management Company LLP", "PPG", "2026-03-31"),
        ("Wellington Management Company LLP", "Darden", "2026-06-30"),
        ("Wellington Management Company LLP", "Dominion", "2026-06-30"),
        ("Wellington Management Company LLP", "Charles River", "2025-12-31"),
        ("Franklin Advisers, Inc.", "Albemarle", "2025-12-31"),
        ("FIRST EAGLE GLOBAL FUND", "C. H. Robinson", "2026-03-31"),
        ("SSGA FUNDS MANAGEMENT, INC.", "EchoStar", "2026-06-30"),
        ("SSGA FUNDS MANAGEMENT, INC.", "Fox", "2026-06-30"),
        ("SSGA FUNDS MANAGEMENT, INC.", "TKO", "2026-06-30"),
        ("STAHL MURRAY", "Texas Pacific Land", "2026-05-05"),
        ("Capital Ventures International", "First Solar", "2026-06-30"),
        ("Susquehanna Advisors Group, Inc.", "First Solar", "2026-06-30"),
        ("Ott David C.", "Fortive", "2026-03-31"),
    ]
    departed = [
        edge for edge in _owner_edges(cached_archive) if edge["metadata"].get("exit_kind") == "departed"
    ]
    assert len(departed) == 13
    assert all(edge["metadata"].get("valid_to") for edge in departed)
    for holder, company, valid_to in expected:
        matching = [
            edge
            for edge in departed
            if _matches(entities, edge["source"], holder)
            and _company_matches(entities, edge["target"], company)
            and edge["metadata"].get("valid_to") == valid_to
        ]
        assert len(matching) == 1, (holder, company, valid_to, len(matching))

    by_accession: dict[tuple[str, str, str], dict[str, object]] = {}
    own_by_holder: dict[tuple[str, str], list[dict]] = {}
    for edge in _owner_edges(cached_archive):
        meta = edge["metadata"]
        for observation in meta.get("observations", []):
            if observation.get("source") == "proxy-table":
                continue
            key = (edge["target"], _norm(meta.get("class") or "<null>"), observation["accession"])
            filing = by_accession.setdefault(
                key,
                {
                    "as_of": observation.get("as_of") or "",
                    "filed": observation.get("filed") or "",
                    "members": set(),
                    "filers": set(),
                },
            )
            filing["members"].add(edge["source"])
            if observation.get("holder_cik"):
                filing["filers"].add(observation["holder_cik"])
            own_by_holder.setdefault((edge["target"], edge["source"]), []).append(observation)
    for edge in departed:
        meta = edge["metadata"]
        closing = meta["exit_accession"]
        prior = meta["exit_prior_accession"]
        target_class = (edge["target"], _norm(meta.get("class") or "<null>"))
        closing_row = by_accession[(*target_class, closing)]
        prior_row = by_accession[(*target_class, prior)]
        closing_rank = (closing_row["as_of"], closing_row["filed"])
        prior_rank = (prior_row["as_of"], prior_row["filed"])
        assert closing != prior
        assert closing_rank > prior_rank
        assert edge["source"] not in closing_row["members"]
        assert prior_row["members"] & closing_row["members"]
        assert prior_row["filers"] & closing_row["filers"]
        for filer_cik in prior_row["filers"] & closing_row["filers"]:
            latest_accession = max(
                (
                    (row["as_of"], row["filed"], accession)
                    for (target, cls, accession), row in by_accession.items()
                    if (target, cls) == target_class and filer_cik in row["filers"]
                )
            )[2]
            assert latest_accession == closing
        assert not any(
            (observation.get("as_of") or "", observation.get("filed") or "") > prior_rank
            and observation.get("pct") is not None
            and observation["pct"] >= 5.0
            and not observation.get("five_pct_or_less")
            for observation in own_by_holder[(edge["target"], edge["source"])]
        )


def test_seventeen_false_departures_are_live(cached_archive):
    entities = _entities(cached_archive)
    expected = [
        ("Brian Chesky", "Airbnb", 13.6, "0001193125-26-352825"),
        ("Joseph Gebbia", "Airbnb", 7.5, "0001193125-26-352831"),
        ("Antony P. Ressler", "Ares", 33.1, "0001104659-26-096203"),
        ("MS&AD Insurance Group Holdings", "Berkley", 15.8, "0001193125-26-258039"),
        ("Mitsui Sumitomo Insurance", "Berkley", 15.8, "0001193125-26-258039"),
        ("LAL Family Corporation", "Estee Lauder", 21.9, "0001140361-26-005591"),
        ("James S. Tisch", "Loews", 8.4, "0001140361-26-031845"),
        ("IAC Inc.", "MGM", 26.1, "0001104659-26-068674"),
        ("Hotchkis and Wiley", "Workday", 6.31, "0001172661-26-003304"),
        ("Artisan Investments GP LLC", "Willis Towers Watson", 5.9, "0001193125-26-349312"),
        ("Artisan Partners Asset Management", "Willis Towers Watson", 5.9, "0001193125-26-349312"),
        ("Artisan Partners Holdings", "Willis Towers Watson", 5.9, "0001193125-26-349312"),
        ("Artisan Partners Limited Partnership", "Willis Towers Watson", 5.9, "0001193125-26-349312"),
        ("Blossom Fountain", "Wynn", 5.0, "0001213900-25-101921"),
        ("Galaxy Entertainment Group", "Wynn", 5.0, "0001213900-25-101921"),
        ("Herobright", "Wynn", 5.0, "0001213900-25-101921"),
        ("LAL Family Partners", "Estee Lauder", 21.9, "0001140361-26-005587"),
    ]
    for holder, company, pct, accession in expected:
        matches = [
            edge
            for edge in _owner_edges(cached_archive)
            if any(_norm(holder) in _norm(name) for name in _names(entities[edge["source"]]))
            and _company_matches(entities, edge["target"], company)
            and any(
                observation.get("accession") == accession and observation.get("pct") == pct
                for observation in edge["metadata"].get("observations", [])
            )
        ]
        assert matches, (holder, company, accession)
        assert all(edge["metadata"].get("valid_to") is None for edge in matches), (holder, company)


def test_exit_kinds_distinguish_transfer_merger_and_threshold(cached_archive):
    entities = _entities(cached_archive)
    owners = _owner_edges(cached_archive)

    rollins = next(
        edge
        for edge in owners
        if _matches(entities, edge["source"], "Gary W. Rollins Voting Trust")
        and _company_matches(entities, edge["target"], "Rollins")
    )
    assert rollins["metadata"]["exit_kind"] == "transferred"

    charter = next(
        edge
        for edge in owners
        if _matches(entities, edge["source"], "Liberty Broadband")
        and _company_matches(entities, edge["target"], "Charter")
    )
    assert charter["metadata"]["exit_kind"] == "merger"
    note = charter["metadata"]["exit_note"]
    assert len(note) <= 300
    assert "completed" in note.casefold() and "disposed" in note.casefold()

    threshold_cases = [
        ("State Street", "Hormel"),
        ("State Street", "Northern Trust"),
        ("State Street", "Biogen"),
        ("State Street", "Incyte"),
        ("State Street", "eBay"),
        ("FMR", "General Electric"),
        ("Abigail P. Johnson", "General Electric"),
        ("FMR", "United Airlines"),
        ("Abigail P. Johnson", "United Airlines"),
        ("FMR", "West Pharmaceutical"),
        ("Abigail P. Johnson", "West Pharmaceutical"),
        ("FMR", "State Street"),
        ("Abigail P. Johnson", "State Street"),
        ("Capital Research Global Investors", "Las Vegas Sands"),
        ("Capital World Investors", "Apollo"),
    ]
    threshold_edges = []
    for holder, company in threshold_cases:
        matching = [
            edge
            for edge in owners
            if _matches_canonical(entities, edge["source"], holder)
            and _company_matches(entities, edge["target"], company)
        ]
        assert len(matching) == 1, (holder, company, len(matching))
        assert matching[0]["metadata"]["exit_kind"] == "threshold_flag"
        threshold_edges.extend(matching)
    assert len(threshold_edges) == 15

    vanguard = [
        edge
        for edge in owners
        if _matches(entities, edge["source"], "The Vanguard Group") and edge["metadata"].get("valid_to")
    ]
    assert len(vanguard) == 500
    assert all(edge["metadata"]["exit_kind"] == "filer_split" for edge in vanguard)


def test_proxy_names_reuse_person_uri_for_same_name(cached_archive):
    entities = _entities(cached_archive)
    duplicates = set()
    names_by_company: dict[tuple[str, str], set[str]] = {}
    for edge in _owner_edges(cached_archive):
        name = entities.get(edge["source"], {}).get("name", "")
        key = (edge["target"], _norm(name))
        names_by_company.setdefault(key, set()).add(edge["source"])
    duplicates = {key: uris for key, uris in names_by_company.items() if key[1] and len(uris) > 1}
    assert duplicates == {}


def test_distinct_names_under_one_cik_split_and_are_listed(cached_archive):
    entities = _entities(cached_archive)
    expected_names = {
        "0000918186": {"WR Berkley & Others LLC", "William R. Berkley"},
        "0001825861": {
            "Horton Family Limited Partnership",
            "Horton Family Limited Partnership II",
        },
        "0002006397": {
            "Mastercard Foundation",
            "Mastercard Foundation Asset Management Corp",
        },
    }
    splits: dict[str, list[dict]] = {}
    for cik, wanted in expected_names.items():
        matching = [
            entity for entity in entities.values() if entity.get("properties", {}).get("shared_cik") == cik
        ]
        assert len(matching) == 2
        assert {entity["name"] for entity in matching} == wanted
        assert all(entity["properties"].get("cik") == cik for entity in matching)
        assert all(entity["uri"].startswith(f"urn:sec:cik:{cik}:") for entity in matching)
        splits[cik] = matching
    listed = {row["cik"] for row in cached_archive["generation"]["cik_splits"]}
    assert set(expected_names) == listed
    for row in cached_archive["generation"]["cik_splits"]:
        assert row["shared_accessions"]


def test_names_never_together_under_a_cik_merge_as_aliases(cached_archive):
    entities = _entities(cached_archive)
    merges = cached_archive["generation"]["cik_alias_merges"]
    assert {row["cik"] for row in merges} >= {"0001800227", "0001827011"}
    assert all(row["names"] and row["first_last_as_of"] for row in merges)

    mgm = [
        edge
        for edge in _owner_edges(cached_archive)
        if _company_matches(entities, edge["target"], "MGM")
        and _matches(entities, edge["source"], "People Incorporated")
    ]
    assert len(mgm) == 1
    assert _matches_canonical(entities, mgm[0]["source"], "People Incorporated")
    assert mgm[0]["metadata"]["valid_to"] is None
    assert mgm[0]["metadata"]["pct"] == 26.5
    assert any(
        observation.get("pct") == 26.1 and observation.get("accession") == "0001104659-26-068674"
        for observation in mgm[0]["metadata"]["observations"]
    )
    assert not any(
        edge["metadata"].get("exit_kind") == "departed"
        and _company_matches(entities, edge["target"], "MGM")
        and _matches(entities, edge["source"], "IAC Inc.")
        for edge in _owner_edges(cached_archive)
    )

    reddit = [
        edge
        for edge in _owner_edges(cached_archive)
        if _company_matches(entities, edge["target"], "Reddit")
        and _matches(entities, edge["source"], "Steven Huffman")
    ]
    assert len(reddit) == 1
    assert _matches(entities, reddit[0]["source"], "Steve Ladd Huffman")
    assert _matches_canonical(entities, reddit[0]["source"], "Steve Ladd Huffman")
    assert len(reddit[0]["metadata"]["observations"]) == 2


def test_proxy_bracket_markers_strip_and_bare_fidelity_resolves(cached_archive):
    entities = _entities(cached_archive)
    marked = [
        entity
        for entity in entities.values()
        if "holder" in entity.get("tags", [])
        and re.search(
            r"(?:\(\s*(?:[ivxlcdm]+|\d+|[a-z])\s*\)|\[\s*(?:[ivxlcdm]+|\d+|[a-z])\s*\])$",
            entity.get("name", ""),
            re.IGNORECASE,
        )
    ]
    assert marked == []
    expected = {
        ("urn:sec:cik:0000102909", "urn:sec:cik:0000849395"),
        ("urn:sec:cik:0000315066", "urn:sec:cik:0000849395"),
        ("urn:filings-graph:org:newport.trust.company.llc", "urn:sec:cik:0000040533"),
    }
    found = {
        (edge["source"], edge["target"])
        for edge in _owner_edges(cached_archive)
        if any(o.get("source") == "proxy-table" for o in edge["metadata"].get("observations", []))
        and (edge["source"], edge["target"]) in expected
    }
    assert found == expected
    fidelity = [
        row for row in cached_archive["generation"]["brand_resolved"] if _norm(row["name"]) == "fidelity"
    ]
    assert fidelity
    assert all(row["holder_uri"] == "urn:sec:cik:0000315066" for row in fidelity)


def test_proxy_name_markers_repeat_and_unbracketed_ordinals_are_preserved():
    raw = """<table><tr><th>Name of Beneficial Owner</th><th>Shares</th><th>Percent of Class</th></tr>
      <tr><td>Acme Fund No. 2</td><td>10000</td><td>6%</td></tr>
      <tr><td>Northstar Partners (1)(2)</td><td>10000</td><td>6%</td></tr>
      <tr><td>Maple Trust, (c)</td><td>10000</td><td>6%</td></tr>
      <tr><td>Horton Family Limited Partnership II</td><td>10000</td><td>6%</td></tr></table>"""

    rows, _ = parse_ownership_tables_detailed(raw)
    assert [row.name for row in rows] == [
        "Acme Fund No. 2",
        "Northstar Partners",
        "Maple Trust",
        "Horton Family Limited Partnership II",
    ]


def test_all_bare_brand_rows_resolve_to_archive_ciks(cached_archive):
    generation = cached_archive["generation"]
    assert generation["unresolved_bare_brands"] == []
    rows = generation["brand_resolved"]
    expected = {
        ("blackrock", "0001637459", "urn:sec:cik:0002012383"),
        ("state street", "0000895421", "urn:sec:cik:0000093751"),
        ("vanguard", "0000895421", "urn:sec:cik:0000102909"),
    }
    actual = {(_norm(row["name"]), row["company_cik"], row["holder_uri"]) for row in rows}
    assert expected <= actual
    assert all(
        row["holder_uri"] == "urn:sec:cik:0000315066" for row in rows if _norm(row["name"]) == "fidelity"
    )
    assert all(row["resolved_by"] == "brand" for row in rows)


def test_cached_tyson_class_comment_is_preserved_on_class_a_edge(cached_archive):
    cached = json.loads((PULL_DIR / "0000100493.json").read_text())
    source_row = next(
        item
        for item in cached["holdings"]
        if item["holder_name"] == "TYSON LIMITED PARTNERSHIP" and item["accession"] == "0001999371-26-003264"
    )
    row = FilingRow(
        form=source_row["form"],
        filed=source_row["filed"],
        accession=source_row["accession"],
    )
    holding = next(
        item
        for item in parse_13_xml(TYSON_XML.read_bytes(), row)
        if item.holder_name == "TYSON LIMITED PARTNERSHIP"
    )
    assert holding.class_title == "Class A Common Stock, par value $.10 per share"
    assert holding.class_conflict is True
    assert "Class B Common Stock" in holding.class_note
    assert len(holding.class_note) <= 300

    entities = _entities(cached_archive)
    edge = next(
        edge
        for edge in _owner_edges(cached_archive)
        if _matches(entities, edge["source"], "Tyson Limited Partnership")
        and _company_matches(entities, edge["target"], "Tyson Foods")
        and "class a" in _norm(edge["metadata"].get("class", ""))
    )
    assert edge["metadata"]["class_conflict"] is True
    assert edge["metadata"]["class"] == "Class A Common Stock, par value $.10 per share"
    assert edge["metadata"]["class_note"] == holding.class_note
