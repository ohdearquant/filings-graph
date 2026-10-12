"""Build the filings-graph store from EDGAR, end to end.

    uv run python -m filings_graph.build pull    [--limit N] [--universe data/universe.csv]
    uv run python -m filings_graph.build archive --out data/external/filings-graph/<name>.json
    uv run python -m filings_graph.build config  --store .khive/<name>.db --out .khive/<name>.toml
    uv run python -m filings_graph.build load    --config .khive/<name>.toml --archive data/external/filings-graph/<name>.json
    uv run python -m filings_graph.build all     --name filings-graph-v0 [--limit N]

Scope: the latest 10-K per company, its Exhibit 21, every Schedule 13D/13G filed
about it since its last fiscal year end, the latest DEF 14A, the 8-Ks since the 10-K. `pull` reads
EDGAR through `filings_graph.edgar.Edgar` (cached) and writes one JSON per company under
data/external/filings-graph/pull/. `archive` derives entities and edges from those rows alone (no model in
the loop) and writes an immutable generation
with a manifest. `load` creates entities then edges through the stock kkernel against one config.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html as htmlmod
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from filings_graph.edgar import Edgar, cik10
from filings_graph.khive import Khive
from filings_graph.models import (
    Archive,
    CompanyHeader,
    Edge,
    Entity,
    FilingRow,
    HoldingRow,
    ProxyHolderRow,
    Pulled,
    SubsidiaryRow,
)
from filings_graph.proxy import (
    _strip_trailing_bracketed_markers,
    parse_ownership_tables_audited,
    parse_ownership_tables_detailed,
)

ROOT = Path(__file__).resolve().parents[2]
_PROXY_LEGAL_FORM_SUFFIXES = (
    (re.compile(r"\s+limited\s+partnership\.?$", re.IGNORECASE), "lp"),
    (re.compile(r"\s+incorporated\.?$", re.IGNORECASE), "inc"),
    (re.compile(r"\s+corporation\.?$", re.IGNORECASE), "corp"),
    (re.compile(r"\s+company\.?$", re.IGNORECASE), "co"),
    (re.compile(r"\s+limited\.?$", re.IGNORECASE), "ltd"),
    (re.compile(r"\s+inc\.?$", re.IGNORECASE), "inc"),
    (re.compile(r"\s+corp\.?$", re.IGNORECASE), "corp"),
    (re.compile(r"\s+co\.?$", re.IGNORECASE), "co"),
    (re.compile(r"\s+llc\.?$", re.IGNORECASE), "llc"),
    (re.compile(r"\s+llp\.?$", re.IGNORECASE), "llp"),
    (re.compile(r"\s+l\.?\s*p\.?$", re.IGNORECASE), "lp"),
    (re.compile(r"\s+ltd\.?$", re.IGNORECASE), "ltd"),
    (re.compile(r"\s+plc\.?$", re.IGNORECASE), "plc"),
    (re.compile(r"\s+n\.?\s*a\.?$", re.IGNORECASE), "na"),
    (re.compile(r"\s+s\.?\s*a\.?$", re.IGNORECASE), "sa"),
    (re.compile(r"\s+ag\.?$", re.IGNORECASE), "ag"),
)
_PROXY_TRAILING_QUALIFIERS = (
    re.compile(r"\s*,?\s*and\s+its\s+affiliates\.?$", re.IGNORECASE),
    re.compile(r"\s*,?\s*and\s+affiliates\.?$", re.IGNORECASE),
    re.compile(r"\s*,?\s*and\s+subsidiaries\.?$", re.IGNORECASE),
    re.compile(r"\s*\(\s*and\s+subsidiaries\s*\)\s*$", re.IGNORECASE),
    re.compile(r"\s*,?\s*and/or\s+certain\s+other\s+entities\.?$", re.IGNORECASE),
    re.compile(r"\s*,?\s*and\s+certain\s+related\s+entities\.?$", re.IGNORECASE),
    re.compile(r"\s*,?\s*and\s+related\s+entities\.?$", re.IGNORECASE),
    re.compile(r"\s*,?\s*and\s+affiliated\s+entities\.?$", re.IGNORECASE),
    re.compile(r"\s*,?\s*et\s+al\.?$", re.IGNORECASE),
    re.compile(r"\s*,\s*division\s+of\s*$", re.IGNORECASE),
)


def proxy_legal_name_identity(name: str) -> tuple[tuple[str, ...], str | None]:
    """Return exact core tokens and the trailing legal form used for proxy-to-CIK matching."""
    value = re.sub(r"\s+([,.;])", r"\1", name.strip())
    while True:
        prior = value
        value = re.sub(r"\s*⁽\s*[⁰¹²³⁴⁵⁶⁷⁸⁹]+\s*⁾\s*$", "", value)
        value = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹*†‡§]+\s*$", "", value).rstrip(" ,;")
        value = re.sub(r"\s*[([]\s*(?:[a-z]|\d{1,2}|[ivxlcdm]+)\s*[)\]]\s*$", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*\(\s*the\s+[^()]+\)\s*$", "", value, flags=re.IGNORECASE)
        for pattern in _PROXY_TRAILING_QUALIFIERS:
            value = pattern.sub("", value)
        if value == prior:
            break
    # Joint reporting names use the first reporting person as the identity anchor.
    value = re.split(r"\s*/\s*", value, maxsplit=1)[0].strip()
    legal_form = None
    for pattern, form in _PROXY_LEGAL_FORM_SUFFIXES:
        if pattern.search(value):
            value = pattern.sub("", value).rstrip(" ,.;")
            legal_form = form
            break
    tokens = tuple(re.findall(r"[a-z0-9]+", value.casefold()))
    if tokens[:1] == ("the",):
        tokens = tokens[1:]
    return tokens, legal_form


def proxy_legal_names_match(
    left: tuple[tuple[str, ...], str | None], right: tuple[tuple[str, ...], str | None]
) -> bool:
    return left[0] == right[0] and (left[1] is None or right[1] is None or left[1] == right[1])


def _proxy_same_name_rows(rows: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Any]] = {}
    for row in rows:
        key = (
            norm_name(row.name),
            norm_name(row.class_title or ""),
            row.table,
        )
        grouped.setdefault(key, []).append(row)
    result = []
    for duplicates in grouped.values():
        if len({row.shares for row in duplicates}) < 2:
            continue
        first = duplicates[0]
        result.append(
            {
                "name": first.name,
                "table": first.table,
                "class_title": first.class_title,
                "rows": [
                    {"pct": row.pct, "shares": row.shares, "raw": row.raw}
                    for row in sorted(
                        duplicates, key=lambda item: (item.shares or 0, item.pct or 0, item.raw)
                    )
                ],
            }
        )
    return sorted(result, key=lambda item: (item["name"].casefold(), item["class_title"] or ""))


# Form 8-K item titles (SEC Form 8-K general instructions), for the note text
EIGHT_K_ITEMS = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Election of Directors or Officers; Compensatory Arrangements",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Employee Benefit Plans",
    "5.05": "Amendments to the Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}
PULL_DIR = ROOT / "data/external/filings-graph/pull"
FORMS_13 = (
    "SC 13D",
    "SC 13D/A",
    "SC 13G",
    "SC 13G/A",
    "SCHEDULE 13D",
    "SCHEDULE 13D/A",
    "SCHEDULE 13G",
    "SCHEDULE 13G/A",
)
HOLDINGS_CAP = 300  # 13D/13G rows read per company (an asset manager's own filings run to thousands)
ITEM_SLICE = 100  # entities per create(items=...) request
LINK_SLICE = 1000  # the link verb's bulk cap
# The whole ops JSON is ONE argv element of the kkernel call, and Linux refuses a single argument of 131,072
# bytes or more (MAX_ARG_STRLEN; macOS has no such cap). A full link slice runs to ~300 KB, so slices are cut by
# bytes as well as by count.
ARGV_CAP = 100_000

US_STATES = {
    "Alabama",
    "Alaska",
    "Arizona",
    "Arkansas",
    "California",
    "Colorado",
    "Connecticut",
    "Delaware",
    "Florida",
    "Georgia",
    "Hawaii",
    "Idaho",
    "Illinois",
    "Indiana",
    "Iowa",
    "Kansas",
    "Kentucky",
    "Louisiana",
    "Maine",
    "Maryland",
    "Massachusetts",
    "Michigan",
    "Minnesota",
    "Mississippi",
    "Missouri",
    "Montana",
    "Nebraska",
    "Nevada",
    "New Hampshire",
    "New Jersey",
    "New Mexico",
    "New York",
    "North Carolina",
    "North Dakota",
    "Ohio",
    "Oklahoma",
    "Oregon",
    "Pennsylvania",
    "Rhode Island",
    "South Carolina",
    "South Dakota",
    "Tennessee",
    "Texas",
    "Utah",
    "Vermont",
    "Virginia",
    "Washington",
    "West Virginia",
    "Wisconsin",
    "Wyoming",
    "District of Columbia",
    "Puerto Rico",
    "U.S. Virgin Islands",
    "Guam",
}
PLACES = US_STATES | {
    "United States",
    "USA",
    "U.S.",
    "Canada",
    "Ontario",
    "Quebec",
    "Alberta",
    "British Columbia",
    "Nova Scotia",
    "Mexico",
    "Brazil",
    "Argentina",
    "Chile",
    "Colombia",
    "Peru",
    "Panama",
    "Costa Rica",
    "Bermuda",
    "Cayman Islands",
    "British Virgin Islands",
    "Bahamas",
    "Barbados",
    "Jersey",
    "Guernsey",
    "Isle of Man",
    "Gibraltar",
    "United Kingdom",
    "England",
    "England and Wales",
    "Scotland",
    "Wales",
    "Northern Ireland",
    "Ireland",
    "France",
    "Germany",
    "Netherlands",
    "Belgium",
    "Luxembourg",
    "Switzerland",
    "Austria",
    "Italy",
    "Spain",
    "Portugal",
    "Denmark",
    "Sweden",
    "Norway",
    "Finland",
    "Iceland",
    "Poland",
    "Czech Republic",
    "Hungary",
    "Romania",
    "Greece",
    "Turkey",
    "Cyprus",
    "Malta",
    "Israel",
    "United Arab Emirates",
    "Dubai",
    "Saudi Arabia",
    "Qatar",
    "Egypt",
    "South Africa",
    "Nigeria",
    "Kenya",
    "Morocco",
    "Mauritius",
    "India",
    "Pakistan",
    "Bangladesh",
    "Sri Lanka",
    "China",
    "Hong Kong",
    "Macau",
    "Taiwan",
    "Japan",
    "South Korea",
    "Korea",
    "Singapore",
    "Malaysia",
    "Thailand",
    "Vietnam",
    "Indonesia",
    "Philippines",
    "Australia",
    "New Zealand",
    "Russia",
    "Ukraine",
    "Kazakhstan",
    "Delaware, USA",
    "Netherlands Antilles",
    "Curacao",
    "Curaçao",
    "Liechtenstein",
    "Monaco",
    "Slovakia",
    "Slovenia",
    "Croatia",
    "Serbia",
    "Bulgaria",
    "Lithuania",
    "Latvia",
    "Estonia",
    "Peoples Republic of China",
    "People's Republic of China",
    "PRC",
    "UK",
    "U.K.",
    # the rest of the world, Canadian provinces, and the spellings exhibits print
    "United States of America",
    "U.S.A.",
    "US",
    "Manitoba",
    "Saskatchewan",
    "New Brunswick",
    "Newfoundland and Labrador",
    "Newfoundland",
    "Prince Edward Island",
    "Yukon",
    "Venezuela",
    "Russian Federation",
    "Tunisia",
    "Ecuador",
    "Uruguay",
    "Dominican Republic",
    "Guatemala",
    "Paraguay",
    "Trinidad and Tobago",
    "Papua New Guinea",
    "Algeria",
    "Jamaica",
    "El Salvador",
    "Senegal",
    "Armenia",
    "Bosnia and Herzegovina",
    "Bosnia",
    "Bolivia",
    "Ethiopia",
    "Botswana",
    "Sudan",
    "South Sudan",
    "Lao People's Democratic Republic",
    "Laos",
    "Maldives",
    "Myanmar",
    "Kosovo",
    "Republic of Korea",
    "Korea, Republic of",
    "Seychelles",
    "Ghana",
    "Madagascar",
    "Nepal",
    "Moldova",
    "Albania",
    "Czechia",
    "Jordan",
    "Montenegro",
    "Nicaragua",
    "Réunion",
    "Reunion",
    "Uzbekistan",
    "Tanzania",
    "Honduras",
    "Haiti",
    "Cuba",
    "Belize",
    "Guyana",
    "Suriname",
    "Aruba",
    "Cayman",
    "Bahrain",
    "Kuwait",
    "Oman",
    "Lebanon",
    "Iraq",
    "Iran",
    "Syria",
    "Yemen",
    "Afghanistan",
    "Azerbaijan",
    "Georgia (country)",
    "Kyrgyzstan",
    "Tajikistan",
    "Turkmenistan",
    "Mongolia",
    "North Macedonia",
    "Macedonia",
    "Belarus",
    "Cambodia",
    "Brunei",
    "Timor-Leste",
    "Fiji",
    "Samoa",
    "Tonga",
    "Vanuatu",
    "Solomon Islands",
    "New Caledonia",
    "French Polynesia",
    "Angola",
    "Benin",
    "Burkina Faso",
    "Burundi",
    "Cameroon",
    "Cape Verde",
    "Chad",
    "Congo",
    "Democratic Republic of the Congo",
    "Cote d'Ivoire",
    "Côte d'Ivoire",
    "Ivory Coast",
    "Djibouti",
    "Equatorial Guinea",
    "Eritrea",
    "Eswatini",
    "Swaziland",
    "Gabon",
    "Gambia",
    "Guinea",
    "Lesotho",
    "Liberia",
    "Libya",
    "Malawi",
    "Mali",
    "Mauritania",
    "Mozambique",
    "Namibia",
    "Niger",
    "Rwanda",
    "Sierra Leone",
    "Somalia",
    "Togo",
    "Uganda",
    "Zambia",
    "Zimbabwe",
    "Andorra",
    "San Marino",
    "Vatican",
    "Faroe Islands",
    "Greenland",
    "Anguilla",
    "Antigua and Barbuda",
    "Dominica",
    "Grenada",
    "Montserrat",
    "Saint Kitts and Nevis",
    "Saint Lucia",
    "Saint Vincent and the Grenadines",
    "Turks and Caicos Islands",
    "Turks and Caicos",
    "Virgin Islands",
    "Marshall Islands",
    "Labuan",
    "Nauru",
    "Palau",
    "Micronesia",
    "Hong Kong SAR",
    "Macao",
    "Alderney",
    "Sark",
    "Cook Islands",
    "Northern Mariana Islands",
    "American Samoa",
    "Mexico City",
    "Republic of North Macedonia",
    "Republic Of North Macedonia",
}
CA_PROVINCES = {
    "Ontario",
    "Quebec",
    "Alberta",
    "British Columbia",
    "Nova Scotia",
    "Manitoba",
    "Saskatchewan",
    "New Brunswick",
    "Newfoundland and Labrador",
    "Newfoundland",
    "Prince Edward Island",
    "Yukon",
}
_PLACE_RE = re.compile(
    r"^(?:(?:a |an )?(?:company |corporation |entity )?(?:incorporated|organized|organised|formed|established|registered)?"
    r"\s*(?:in|under the laws of|under)?\s*(?:the\s+)?(?:state of |province of |commonwealth of )?)"
    r"(" + "|".join(re.escape(p) for p in sorted(PLACES, key=len, reverse=True)) + r")"
    r"(?:\s+(?:corporation|company|co\.|corp\.?|inc\.?|llc|l\.l\.c\.|limited liability company|limited partnership|"
    r"partnership|limited|ltd\.?|entity|trust|l\.?p\.?|gmbh|s\.a\.|b\.v\.|plc|sarl|ag|kk|pte\.? ltd\.?))?\.?$",
    re.IGNORECASE,
)
_NAME_PLACE = re.compile(
    r"^(.{3,160}?)\s*(?:[\(\-–—,]|\s[-–—]\s)\s*(an?\s+[A-Za-z .'&]+?|[A-Za-z .'&]+?)\)?\.?\s*$"
)


def split_name_place(line: str) -> tuple[str, str] | None:
    """`Name (Place)`, `Name - Place`, `Name, a Florida LLC`, `Name, Delaware`: (name, place) or None."""
    m = _NAME_PLACE.match(line.strip())
    if not m:
        return None
    place = place_of(m.group(2).strip())
    if not place:
        return None
    return m.group(1).strip(" ,;"), place


_PLACE_ANY_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in sorted(PLACES, key=len, reverse=True)) + r")\b", re.IGNORECASE
)
_PLACE_HEAD_RE = re.compile(
    r"^(" + "|".join(re.escape(p) for p in sorted(PLACES, key=len, reverse=True)) + r")\b(.*)$", re.IGNORECASE
)


def _place_text(matched: str) -> str:
    """The place as the exhibit prints it, in the set's spelling of case."""
    low = matched.lower()
    # PLACES includes historical casing variants. Select one deterministically rather than
    # letting the process-specific iteration order of a set change unrelated pull rows.
    return next((p for p in sorted(PLACES) if p.lower() == low), matched)


def place_of_strict(cell: str) -> str | None:
    """A cell that is nothing but a place (with the usual `incorporated in the State of` wrapping)."""
    m = _PLACE_RE.match(cell.strip())
    return _place_text(m.group(1)) if m else None


def place_of(cell: str) -> str | None:
    """The place a cell names: `Delaware`, `Incorporated in the United Kingdom`, `organized under
    the laws of the State of New York`; None when the cell is not a place. A
    composite cell (`Alberta, Canada`, `USA (Delaware)`, `United States, Texas`, `Lima, Peru`,
    `Hong Kong SAR, China`) answers its most specific part, a state or province before a country;
    a trailing artifact of at most three characters (`Delaware es`) is ignored. A cell that reads
    as a company name (`Intervet (Ireland) Limited`, `3065619 Nova Scotia Company`) is not a place."""
    s = cell.strip()
    if not s:
        return None
    m = _PLACE_RE.match(s)
    if m:
        return _place_text(m.group(1))
    if len(s) > 60 or _CORP_RE.search(s) or re.match(r"^\d", s):
        return None
    parts = [p.strip(" .") for p in re.split(r"[,/()]|\s+[-–—]\s+", s) if p.strip(" .")]
    found: list[str] = []
    for p in parts:
        m = _PLACE_RE.match(p)
        if m:
            found.append(_place_text(m.group(1)))
    if not found:
        m = _PLACE_HEAD_RE.match(s)
        if m and len(m.group(2).strip()) <= 3:
            return _place_text(m.group(1))
        return None
    found.sort(key=lambda p: (0 if p in US_STATES else 1 if p in CA_PROVINCES else 2, -len(p)))
    return found[0]


def footnote_places(text: str) -> dict[str, str]:
    """`(4) Incorporated/Organized under the laws of Delaware` lines at the foot of an exhibit:
    marker -> place (Regions prints the jurisdiction only this way)."""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        m = re.match(r"^\s*\((\d{1,2})\)\s+(.{4,200})$", ln.strip())
        if not m:
            continue
        pm = _PLACE_ANY_RE.search(m.group(2))
        if pm:
            out.setdefault(m.group(1), _place_text(pm.group(1)))
    return out


_FOOT_RE = re.compile(r"\s*(\(\d{1,2}\)\s*)+$")
_CORP_RE = re.compile(
    r"(?:\b(?:Inc|LLC|L\.L\.C|Ltd|Limited|Corp|Corporation|Company|Co|L\.P|LP|LLP|GmbH|S\.A|S\.A\.S|B\.V|N\.V|"
    r"S\.r\.l|S\.L|Pty|plc|PLC|Trust|Bank|Partners|Holdings|Holding|Associates|AG|Oy|AB|A/S|K\.K|Sdn|Bhd|SE|"
    r"SpA|S\.p\.A|Sarl|SARL|Srl|Lda|Ltda|Pte|ApS|BV|NV|SA|Kft|OOO|Fund|Insurance|Capital|International|Group|"
    r"Services|Finance|Leasing|Depositor|Dealership|Mortgage|Securities|Investments|Properties|Realty|Partnership|"
    r"Reinsurance|Assurance|Underwriters|Management|Ventures|Enterprises|Industries|Technologies|Systems|Solutions|"
    r"Laboratories|Clinical|Diagnostics|Pathology|Health|Healthcare|Medical|Pharma|Pharmaceuticals|Labs)\b\.?|"
    r"\b(?:Sp\. z o\.o\.|s\.r\.o\.|d\.o\.o\.))",
)
_PCT_BARE_RE = re.compile(r"^(?:100|\d{1,2})(?:\.\d+)?$")
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# -- helpers --------------------------------------------------------------------------------


def text_of(fragment: str) -> str:
    return _WS_RE.sub(" ", htmlmod.unescape(_TAG_RE.sub(" ", fragment))).strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_name(name: str) -> str:
    """Lower-case words joined by `.`: the slug goes into `urn:filings-graph:*` uris, and a `-` join makes
    `Apollo SK Strategic Co-Investors` read as an OpenAI key (`sk-strategic-co-...`) to the store's
    secret gate, which refused the create."""
    s = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    return re.sub(r"\s+", ".", s)


def filing_row(f: dict) -> FilingRow:
    return FilingRow(
        form=f["form"],
        filed=f["filingDate"],
        period=f.get("reportDate") or None,
        accession=f["accessionNumber"],
        primary_document=f.get("primaryDocument") or None,
        primary_doc_description=f.get("primaryDocDescription") or None,
        items=f.get("items") or None,
    )


def universe(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        out.append(
            {
                "symbol": r["ticker"],
                "security": r["name"],
                "cik": cik10(r["cik"]),
            }
        )
    return out


# -- Exhibit 21 ----------------------------------------------------------------------------


_HEADER_WORDS = re.compile(
    r"\b(?:name of|names of|subsidiar|jurisdiction|incorporat|organiz|organis|registrant|exhibit|percent|ownership|"
    r"owned|voting|held by|state or country|country of|as of \w+ \d|doing business|d/b/a|entity name|entities|"
    r"immediate parent|the company|notes?:)\b",
    re.IGNORECASE,
)
_PCT_HEADER = re.compile(r"percent|ownership|owned|voting|held by", re.IGNORECASE)
_NUMBERING_RE = re.compile(r"^\d{1,4}[.)]?$")


def _merge_pct_cells(cells: list[str]) -> list[str]:
    """`40 | %` (CF, Steel Dynamics) is one cell, `40%`; a bare `%` cell with no number before it drops."""
    out: list[str] = []
    for c in cells:
        if c == "%" and out and re.fullmatch(r"\d{1,3}(?:\.\d+)?", out[-1]):
            out[-1] = out[-1] + "%"
        elif c != "%":
            out.append(c)
    return out


def _is_header(cells: list[str]) -> bool:
    """A row that names the columns. A row carrying a company name or a cell that is only a place
    is data whatever words it also prints (`Florida Power & Light Company (100%-owned)`)."""
    joined = " ".join(cells)
    if any(place_of_strict(c) for c in cells) or any(_CORP_RE.search(c) for c in cells):
        return False
    return bool(_HEADER_WORDS.search(joined))


def _markers(name: str) -> list[str]:
    """`Ascentium Depositor LLC (4)` -> [`4`]. The name itself stays as printed, marker included: the
    cell is recorded verbatim and the store keys on the printed name."""
    return re.findall(r"\((\d{1,2})\)", name)


def _looks_like_name(s: str) -> bool:
    if len(s) < 3 or len(s) > 160 or _NUMBERING_RE.match(s) or _PCT_RE.fullmatch(s):
        return False
    if re.fullmatch(r"[\d.,%()\s]+", s) or re.match(
        r"^(?:subsidiaries|list of|names? of|exhibit|schedule|the following|as of|notes?)\b", s, re.IGNORECASE
    ):
        return False
    return bool(_CORP_RE.search(s)) or (len(s.split()) >= 2 and not _HEADER_WORDS.search(s))


def parse_exhibit21(raw: str, accession: str) -> list[SubsidiaryRow]:
    """Rows of a subsidiaries exhibit.
    Tables: a header row is any row whose text names the columns (name of / jurisdiction /
    percent ...), and once a header has named a percent column a bare `100` in a data row is the
    percent (AIG); `40 | %` is merged to `40%` first (CF). A data row answers its jurisdiction from
    a place cell, else from a `(n)` footnote marker on the name resolved against the exhibit's
    footnotes (Regions), else None when the exhibit prints no jurisdiction anywhere (Labcorp:
    names only). Without tables: numbered or plain lines `Name (n)` / `Name (Place)` / `Name -
    Place`. Section headings and the registrant's own row are left to the caller's filter."""
    rows: list[SubsidiaryRow] = []
    order = 0
    foot = footnote_places(htmlmod.unescape(_TAG_RE.sub("\n", raw)))
    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.DOTALL | re.IGNORECASE)
    table_rows: list[list[str]] = []
    for tr in trs:
        cells = [text_of(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL | re.IGNORECASE)]
        cells = _merge_pct_cells([c for c in cells if c])
        if cells:
            table_rows.append(cells)
    pct_col = False
    any_place = any(place_of_strict(c) for cells in table_rows for c in cells)
    for cells in table_rows:
        if _is_header(cells):
            pct_col = pct_col or bool(_PCT_HEADER.search(" ".join(cells)))
            continue
        if len(cells) == 1:
            got = split_name_place(cells[0])
            if got:
                order += 1
                rows.append(
                    SubsidiaryRow(
                        name=got[0],
                        jurisdiction=got[1],
                        line=cells[0][:300],
                        accession=accession,
                        order=order,
                    )
                )
                continue
            name = cells[0]
            if re.match(r"^\(\d{1,2}\)\s", name):  # a footnote line
                continue
            place = next((foot[k] for k in _markers(name) if k in foot), None)
            if (place or not any_place) and _looks_like_name(name):
                order += 1
                rows.append(
                    SubsidiaryRow(
                        name=name, jurisdiction=place, line=cells[0][:300], accession=accession, order=order
                    )
                )
            continue
        # the place cell: a cell that is only a place first, else a composite one (`Alberta, Canada`)
        place_cell = next((c for c in cells if place_of_strict(c)), None) or next(
            (c for c in cells if place_of(c)), None
        )
        place = place_of(place_cell) if place_cell else None
        name_cell = next(
            (
                c
                for c in cells
                if c != place_cell
                and not _PCT_RE.fullmatch(c)
                and not re.fullmatch(r"[\d.,]+", c)
                and not _NUMBERING_RE.match(c)
            ),
            None,
        )
        if not name_cell:
            continue
        name = name_cell
        if place is None:
            place = next((foot[k] for k in _markers(name) if k in foot), None)
        if place is None and any_place:
            continue
        # beside a cell that is only a place, a one-word name (`MediCall`) is a name
        if not _looks_like_name(name) and not (
            place_cell and place_of_strict(place_cell) and len(name) >= 3 and not _HEADER_WORDS.search(name)
        ):
            continue
        pct = None
        for c in cells:
            m = _PCT_RE.fullmatch(c)
            if m:
                pct = float(m.group(1))
            elif pct_col and c != name_cell and _PCT_BARE_RE.fullmatch(c):
                pct = float(c)
        order += 1
        rows.append(
            SubsidiaryRow(
                name=name,
                jurisdiction=place,
                ownership_pct=pct,
                line=" | ".join(cells)[:300],
                accession=accession,
                order=order,
            )
        )
    if rows:
        return rows
    text = htmlmod.unescape(_TAG_RE.sub("\n", raw))
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for ln in lines:
        if len(ln) > 200 or _NUMBERING_RE.match(ln) or re.match(r"^\(\d{1,2}\)\s", ln):
            continue
        got = split_name_place(ln)
        if got:
            order += 1
            rows.append(
                SubsidiaryRow(
                    name=got[0], jurisdiction=got[1], line=ln[:300], accession=accession, order=order
                )
            )
            continue
        place = next((foot[k] for k in _markers(ln) if k in foot), None)
        if place and _looks_like_name(ln):
            order += 1
            rows.append(
                SubsidiaryRow(name=ln, jurisdiction=place, line=ln[:300], accession=accession, order=order)
            )
    return rows


# -- Schedules 13D / 13G -------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(s: str | None) -> str | None:
    if s and re.fullmatch(r"\d{2}/\d{2}/\d{4}", s):
        return dt.datetime.strptime(s, "%m/%d/%Y").date().isoformat()  # noqa: DTZ007 - date only
    return s or None


def parse_13_xml(raw: bytes, row: FilingRow) -> list[HoldingRow]:
    """The structured primary_doc.xml of a SCHEDULE 13D/13G (late 2024 on): one HoldingRow per
    reporting person. The two schemas name things differently: 13G keeps persons in
    `coverPageHeaderReportingPersonDetails` with `classPercent` and
    `reportingPersonBeneficiallyOwnedAggregateNumberOfShares`; 13D in `reportingPersonInfo` with
    `percentOfClass`, `aggregateAmountOwned`, `reportingPersonCIK`, `memberOfGroup`, and the
    event date is `dateOfEvent` instead of `eventDateRequiresFilingThisStatement`."""
    root = ET.fromstring(raw)
    flat: dict[str, str] = {}
    persons: list[dict[str, str]] = []
    for el in root.iter():
        name = _local(el.tag)
        if name in ("coverPageHeaderReportingPersonDetails", "reportingPersonInfo"):
            person: dict[str, str] = {}
            types = []
            for child in el.iter():
                if child is el or not (child.text or "").strip():
                    continue
                child_name = _local(child.tag)
                value = child.text.strip()
                if child_name == "typeOfReportingPerson":
                    types.append(value)
                person.setdefault(child_name, value)
            person["__reporting_person_types__"] = "\x1f".join(types)
            persons.append(person)
        elif (el.text or "").strip() and name not in flat:
            flat[name] = el.text.strip()
    filer_cik = flat.get("cik")
    as_of = _date(flat.get("eventDateRequiresFilingThisStatement") or flat.get("dateOfEvent"))
    rule = flat.get("designateRulePursuantThisScheduleFiled")
    issuer_cik = flat.get("issuerCik") or flat.get("issuerCIK")
    purpose = flat.get("transactionPurpose")
    class_title = flat.get("securitiesClassTitle") or flat.get("classTitle") or flat.get("securityClassTitle")
    class_tokens = {
        token.casefold()
        for token in re.findall(r"\b(?:class\s+[A-Z]|series\s+[A-Z0-9]+)\b", class_title or "", re.IGNORECASE)
    }
    class_note = None
    for el in root.iter():
        if "comment" not in _local(el.tag).casefold():
            continue
        comment = " ".join(part.strip() for part in el.itertext() if part.strip())
        tokens = {
            token.casefold()
            for token in re.findall(r"\b(?:class\s+[A-Z]|series\s+[A-Z0-9]+)\b", comment, re.IGNORECASE)
        }
        if class_title and tokens - class_tokens:
            class_note = comment[:300]
            break
    class_conflict = class_note is not None
    cusip = next(
        (flat[k] for k in ("issuerCusipNumber", "issuerCusip", "cusipNo", "cusip") if flat.get(k)), None
    )
    all_text = " ".join((el.text or "").strip() for el in root.iter() if (el.text or "").strip())
    filer_split_signal = bool(
        re.search(
            r"\b(?:transferred to|reallocated to|moved to another|reported separately|report separately|"
            r"internal realignment|no longer (?:has|reports?|holds?).{0,120}(?:separately|subsidiar|division))\b",
            all_text,
            re.IGNORECASE | re.DOTALL,
        )
    )
    out = []
    for p in persons:
        pct = p.get("classPercent") or p.get("percentOfClass")
        shares = p.get("reportingPersonBeneficiallyOwnedAggregateNumberOfShares") or p.get(
            "aggregateAmountOwned"
        )
        group = p.get("memberOfGroup")
        types = [t for t in p.get("__reporting_person_types__", "").split("\x1f") if t]
        parsed_shares = None
        if shares:
            try:
                share_value = Decimal(shares.replace(",", "").strip())
                parsed_shares = (
                    int(share_value) if share_value == share_value.to_integral_value() else float(share_value)
                )
            except (InvalidOperation, ValueError):
                pass
        flag = p.get("classOwnership5PercentOrLess")
        if flag is None:
            flag = flat.get("classOwnership5PercentOrLess")
        out.append(
            HoldingRow(
                holder_name=p.get("reportingPersonName", "?"),
                holder_cik=p.get("reportingPersonCIK") or (filer_cik if len(persons) == 1 else None),
                holder_jurisdiction=p.get("citizenshipOrOrganization"),
                pct=float(pct.replace(",", ""))
                if pct and re.fullmatch(r"\d+(\.\d+)?", pct.strip())
                else None,
                shares=parsed_shares,
                as_of=as_of,
                form=row.form,
                accession=row.accession,
                filed=row.filed,
                rule=rule,
                reporting_person_type=types[0] if types else p.get("typeOfReportingPerson"),
                reporting_person_types=types,
                class_title=class_title,
                class_note=class_note,
                class_conflict=class_conflict,
                cusip=next(
                    (p[k] for k in ("issuerCusipNumber", "issuerCusip", "cusipNo", "cusip") if p.get(k)),
                    cusip,
                ),
                five_pct_or_less=bool(flag and flag.strip().lower() in ("y", "yes", "true")),
                filer_split_signal=filer_split_signal,
                issuer_cik=cik10(issuer_cik) if issuer_cik and issuer_cik.isdigit() else None,
                issuer_name=flat.get("issuerName"),
                member_of_group=None if group is None else group.lower() in ("y", "yes", "true"),
                purpose=purpose if purpose else None,
                parsed_from="xml",
            )
        )
    return out


_HDR_FILED_BY = re.compile(r"FILED BY:(.*?)(?:SUBJECT COMPANY:|$)", re.DOTALL)
_HDR_NAME = re.compile(r"COMPANY CONFORMED NAME:\s*(.+)")
_HDR_CIK = re.compile(r"CENTRAL INDEX KEY:\s*(\d{10})")
_HDR_STATE = re.compile(r"STATE OF INCORPORATION:\s*([A-Z0-9]{1,3})")
_HDR_EVENT = re.compile(r"DATE OF EVENT REQUIRING STATEMENT:\s*(\d{8})")
_HDR_SUBJECT = re.compile(r"SUBJECT COMPANY:(.*?)(?:FILED BY:|$)", re.DOTALL)


def subject_of(raw_headers: str) -> tuple[str | None, str | None]:
    """(subject company CIK, name) from an `-index-headers.html`."""
    text = htmlmod.unescape(_TAG_RE.sub("\n", raw_headers))
    m = _HDR_SUBJECT.search(text)
    if not m:
        return None, None
    cik = _HDR_CIK.search(m.group(1))
    name = _HDR_NAME.search(m.group(1))
    return (cik.group(1) if cik else None), (name.group(1).strip() if name else None)


_HTML_PCT = re.compile(
    r"PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW[^%]{0,400}?(\d{1,3}(?:\.\d+)?)\s*%",
    re.DOTALL | re.IGNORECASE,
)


def parse_13_headers(raw: str, row: FilingRow, body: str | None) -> HoldingRow:
    """The `-index-headers.html` of any 13D/13G (every era) names the filer; the old HTML body
    gives the percent by a regex on the cover page, marked html-regex so it can be told from a structured value."""
    text = htmlmod.unescape(_TAG_RE.sub("\n", raw))
    block = _HDR_FILED_BY.search(text)
    seg = block.group(1) if block else text
    name = _HDR_NAME.search(seg)
    cik = _HDR_CIK.search(seg)
    state = _HDR_STATE.search(seg)
    ev = _HDR_EVENT.search(text)
    pct = None
    parsed_from = "headers-only"
    if body:
        m = _HTML_PCT.search(text_of(body))
        if m:
            pct = float(m.group(1))
            parsed_from = "html-regex"
    return HoldingRow(
        holder_name=name.group(1).strip() if name else "?",
        holder_cik=cik.group(1) if cik else None,
        holder_jurisdiction=state.group(1) if state else None,
        pct=pct,
        as_of=dt.datetime.strptime(ev.group(1), "%Y%m%d").date().isoformat() if ev else None,  # noqa: DTZ007 - date only
        form=row.form,
        accession=row.accession,
        filed=row.filed,
        parsed_from=parsed_from,
    )


# -- pull ----------------------------------------------------------------------------------


_IDX_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_IDX_HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_MONTH_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s*\d{4}\b",
    re.IGNORECASE,
)


def proxy_ownership_date(raw: str, filed: str) -> tuple[str, str]:
    """Use the 5% table's nearby heading, then the proxy's record date, then filing date."""
    table_rx = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
    tables = list(table_rx.finditer(raw))
    table_date = None
    previous_table_end = 0
    for table in tables:
        parsed, _ = parse_ownership_tables_detailed(table.group(0))
        if not any(r.pct is not None and r.pct >= 5 for r in parsed):
            previous_table_end = table.end()
            continue
        prefix = raw[max(previous_table_end, table.start() - 1600) : table.start()]
        blocks = re.findall(
            r"<(?:caption|p|div|h[1-6])\b[^>]*>(.*?)</(?:caption|p|div|h[1-6])\s*>",
            prefix,
            re.IGNORECASE | re.DOTALL,
        )
        captions = re.findall(
            r"<caption\b[^>]*>(.*?)</caption\s*>", table.group(0), re.IGNORECASE | re.DOTALL
        )
        contexts = [text_of(block) for block in reversed(blocks)] + [text_of(caption) for caption in captions]
        for context in contexts:
            if not re.search(
                r"\b(?:beneficial|ownership|holder|stockholder|shareholder|5\s*%)\b", context, re.IGNORECASE
            ):
                continue
            for date_match in reversed(list(_MONTH_DATE.finditer(context))):
                if not re.search(r"\b(?:as of|as at)\s*$", context[: date_match.start()], re.IGNORECASE):
                    continue
                try:
                    table_date = dt.datetime.strptime(date_match.group(0), "%B %d, %Y").date().isoformat()  # noqa: DTZ007 - date only
                except ValueError:
                    table_date = None
                break
            if table_date:
                break
        if table_date:
            break
        previous_table_end = table.end()
    record_date = None
    record_matches = list(re.finditer(r"record date", text_of(raw), re.IGNORECASE))
    for m in record_matches:
        around = text_of(raw)[max(0, m.start() - 120) : m.end() + 180]
        dates = list(_MONTH_DATE.finditer(around))
        if dates:
            try:
                nearest = min(
                    dates,
                    key=lambda d: abs((d.start() + d.end()) // 2 - (m.start() + len("record date") // 2)),
                )
                record_date = dt.datetime.strptime(nearest.group(0), "%B %d, %Y").date().isoformat()  # noqa: DTZ007 - date only
            except ValueError:
                record_date = None
            if record_date:
                break
    for date, source in ((table_date, "table"), (record_date, "record_date")):
        if date:
            try:
                event_date = dt.date.fromisoformat(date)
                filed_date = dt.date.fromisoformat(filed)
            except ValueError:
                continue
            if event_date <= filed_date and (filed_date - event_date).days <= 400:
                return date, source
    return filed, "filed"


def exhibit_files(e: Edgar, cik: str, accession: str, files: list[dict], exhibit_type: str) -> list[str]:
    """Documents of one filing whose Type column on the `-index.html` page starts with
    `exhibit_type` (EX-21, EX-21.1, ...). File names are not reliable: Abbott's FY2025 10-K
    names its exhibits by sequence, so the type column is read instead."""
    page = next((f["name"] for f in files if f["name"].endswith("-index.html")), None)
    if page is None:
        return []
    raw = e.document(cik, accession, page).decode("utf-8", "replace")
    out = []
    for tr in _IDX_ROW.findall(raw):
        cells = [text_of(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL | re.IGNORECASE)]
        if not any(c.upper().startswith(exhibit_type) for c in cells):
            continue
        href = _IDX_HREF.search(tr)
        if href:
            name = href.group(1).rsplit("/", 1)[-1]
            if name.lower().endswith((".htm", ".html", ".txt")) and name not in out:
                out.append(name)
    return out


def cached_filing_files(e: Edgar, cik: str, accession: str) -> list[dict]:
    """The files of one filing: its index when cached or when the pull is online, else the files already
    present for that accession.

    An offline rebuild reads only the cache, and some older 13D/13G filings have only their cached
    headers or primary document and no index.json, so a missing index is never looked up offline
    and a 404 online falls back to the files present.
    """
    index_url = e.document_url(cik, accession, "index.json")
    index_path = e._cache_path(index_url)
    if e.has_cached(index_url):
        return e.filing_index(cik, accession)
    if not e.offline:
        try:
            return e.filing_index(cik, accession)
        except httpx.HTTPStatusError as ex:
            if ex.response.status_code != 404:
                raise
    if not index_path.parent.is_dir():
        return []
    return [{"name": p.name, "type": ""} for p in sorted(index_path.parent.iterdir()) if p.is_file()]


def pull_company(
    e: Edgar,
    urow: dict[str, str],
    predecessor: dict[str, Any] | None = None,
    universe_ciks: set[str] | None = None,
    proxy_table_audit_counts: dict[str, int] | None = None,
) -> Pulled:
    cik = urow["cik"]
    sub = e.submissions(cik)
    pred = predecessor or {}
    header = CompanyHeader(
        cik=cik,
        name=sub.get("name", ""),
        tickers=sorted(sub.get("tickers") or []),
        exchanges=sorted(sub.get("exchanges") or []),
        sic=sub.get("sic") or None,
        sic_description=sub.get("sicDescription") or None,
        state_of_incorporation=sub.get("stateOfIncorporation") or None,
        lei=sub.get("lei") or None,
        ein=sub.get("ein") or None,
        fiscal_year_end=sub.get("fiscalYearEnd") or None,
        entity_type=sub.get("entityType") or None,
        former_names=sorted(n.get("name", "") for n in sub.get("formerNames") or [] if n.get("name")),
        predecessor_cik=pred.get("predecessor_cik"),
        predecessor_kind=pred.get("kind"),
        predecessor_effective=pred.get("effective"),
    )
    rows = [filing_row(f) for f in e.filings(sub)]
    warnings: list[str] = []
    # the CIK path each filing's documents are read under (a merged predecessor row keeps its own)
    doc_cik = {r.accession: cik for r in rows}
    own_ciks = {cik}
    if pred.get("predecessor_cik") and not str(pred.get("kind") or "").startswith("spin-off"):
        # a successor issuer (redomiciliation, holding-company merger: XOM, BLK, PSKY, SW): the
        # filing history continues under the old CIK, and 13D/13G about the company split across both
        pcik = pred["predecessor_cik"]
        prows = [filing_row(f) for f in e.filings(e.submissions(pcik))]
        seen = {r.accession for r in rows}
        for r in prows:
            if r.accession not in seen:
                rows.append(r)
                doc_cik[r.accession] = pcik
        rows.sort(key=lambda r: r.filed, reverse=True)
        own_ciks.add(pcik)
        warnings.append(f"successor issuer: {len(prows)} filing rows merged from predecessor CIK {pcik}")
    ten_k = next((r for r in rows if r.form == "10-K"), None)
    if not header.state_of_incorporation and ten_k and ten_k.primary_document:
        # some submissions headers carry no state; the 10-K cover's inline-XBRL tag is the fallback
        cover = e.document(doc_cik[ten_k.accession], ten_k.accession, ten_k.primary_document).decode(
            "utf-8", "replace"
        )
        m = re.search(r'name="dei:EntityIncorporationStateCountryCode"[^>]*>\s*([A-Z0-9]{1,3})\s*<', cover)
        if m:
            header.state_of_incorporation = m.group(1)
            warnings.append(f"state of incorporation from the 10-K cover: {m.group(1)}")
        else:
            warnings.append("state of incorporation empty in header and cover")
    files: list[dict] = []
    ex21: list[SubsidiaryRow] = []
    ex21_file = None
    ex21_files: list[str] = []
    if ten_k is None:
        warnings.append("no 10-K in submissions")
    else:
        tk_cik = doc_cik[ten_k.accession]
        files = cached_filing_files(e, tk_cik, ten_k.accession)
        cand = exhibit_files(e, tk_cik, ten_k.accession, files, "EX-21")
        if cand:
            # every EX-21.x file is read (some filers split the list across 21.1 and 21.2); a row
            # printed in both files is kept twice here and merges into one edge in archive()
            ex21_file = cand[0]
            ex21_files = list(cand)
            for fn in cand:
                raw = e.document(tk_cik, ten_k.accession, fn).decode("utf-8", "replace")
                for r in parse_exhibit21(raw, ten_k.accession):
                    if r.name.lower().startswith("registrant") or norm_name(r.name) == norm_name(header.name):
                        continue
                    ex21.append(r.model_copy(update={"order": len(ex21) + 1}))
            if not ex21:
                warnings.append(f"EX-21 {ex21_file}: no rows parsed")
        else:
            warnings.append("10-K index page lists no EX-21 document")
    # ownership: every 13D/13G about this company since the last fiscal year end (the 10-K period)
    # ownership window: since the last fiscal year end, and at least twelve months (annual 13G
    # amendments cluster in February, so a mid-2026 FYE would otherwise see none)
    year_ago = (dt.date.today() - dt.timedelta(days=365)).isoformat()  # noqa: DTZ011 - local calendar date
    since = min(ten_k.period, year_ago) if ten_k and ten_k.period else year_ago
    holdings: list[HoldingRow] = []
    stakes: list[HoldingRow] = []
    rows13 = sorted(
        (r for r in rows if r.form in FORMS_13 and r.filed >= since),
        key=lambda r: (r.filed, r.accession),
        reverse=True,
    )
    universe_ciks = universe_ciks or set()
    nonuniverse_stakes = 0
    capped = False
    for r in rows13:
        # A company's submissions list every 13D/13G it is party to, as SUBJECT or as FILER (an asset
        # manager files thousands a year about other issuers): the index headers say which, before
        # anything heavier is read.
        # The headers file name is deterministic, so the direction costs one request; the filing
        # index and the XML are read only for inbound rows (an asset manager's 300 outbound rows
        # would otherwise cost three requests each).
        hdr_name = f"{r.accession}-index-headers.html"
        rc = doc_cik[r.accession]
        hdr_url = e.document_url(rc, r.accession, hdr_name)
        if e.offline and not e.has_cached(hdr_url):
            e.skips += 1
            warnings.append(f"{r.form} {r.accession}: uncached index-headers skipped in offline pull")
            continue
        try:
            headers = e.document(rc, r.accession, hdr_name).decode("utf-8", "replace")
        except httpx.HTTPStatusError as ex:
            warnings.append(f"{r.form} {r.accession}: index-headers {ex.response.status_code}")
            continue
        subject_cik, subject_name = subject_of(headers)
        inbound = subject_cik in own_ciks
        subject_in_universe = subject_cik in universe_ciks or inbound
        if not subject_in_universe:
            nonuniverse_stakes += 1
            if nonuniverse_stakes > HOLDINGS_CAP:
                capped = True
                continue
        if not subject_in_universe:
            stakes.append(
                parse_13_headers(headers, r, None).model_copy(
                    update={"issuer_cik": subject_cik, "issuer_name": subject_name}
                )
            )
            continue
        names = [f["name"] for f in cached_filing_files(e, rc, r.accession)]
        xml = [
            n for n in names if n == "primary_doc.xml" or (n.endswith(".xml") and not n.endswith("_htm.xml"))
        ]
        try:
            parsed: list[HoldingRow] = []
            if xml:
                parsed = parse_13_xml(e.document(rc, r.accession, xml[0]), r)
            if parsed:
                h = parse_13_headers(headers, r, None)
                for p in parsed:
                    a, b = norm_name(p.holder_name), norm_name(h.holder_name)
                    if a == b or a.startswith(b) or b.startswith(a):
                        p.holder_cik = p.holder_cik or h.holder_cik
                        p.holder_jurisdiction = p.holder_jurisdiction or h.holder_jurisdiction
            else:
                body = None
                if r.primary_document and r.primary_document.lower().endswith((".htm", ".html", ".txt")):
                    body = e.document(rc, r.accession, r.primary_document).decode("utf-8", "replace")
                parsed = [parse_13_headers(headers, r, body)]
            for p in parsed:
                p.issuer_cik = p.issuer_cik or subject_cik
                p.issuer_name = p.issuer_name or subject_name
            (holdings if inbound else stakes).extend(parsed)
        except (ET.ParseError, UnicodeDecodeError) as ex:
            warnings.append(f"{r.form} {r.accession}: {ex}")
    if capped:
        warnings.append(f"non-universe 13D/13G stakes capped at {HOLDINGS_CAP} (newest kept)")
    proxy = next((r for r in rows if r.form == "DEF 14A"), None)
    proxy_holders: list[ProxyHolderRow] = []
    if proxy and proxy.primary_document and proxy.primary_document.lower().endswith((".htm", ".html")):
        praw = e.document(doc_cik[proxy.accession], proxy.accession, proxy.primary_document).decode(
            "utf-8", "replace"
        )
        as_of, as_of_source = proxy_ownership_date(praw, proxy.filed)
        parsed_proxy, rejected_proxy, rejected_proxy_tables = parse_ownership_tables_audited(
            praw, audit_counts=proxy_table_audit_counts
        )
        proxy_holders = [
            ProxyHolderRow(
                table=r.table,
                name=r.name,
                pct=r.pct,
                shares=r.shares,
                as_of=as_of,
                as_of_source=as_of_source,
                filed=proxy.filed,
                class_title=r.class_title,
                accession=proxy.accession,
                raw=r.raw,
                suspect=r.suspect,
                suspect_reason=r.suspect_reason,
                proxy_table_conflict=r.proxy_table_conflict,
            )
            for r in parsed_proxy
        ]
        proxy_rejected = [{**r, "accession": proxy.accession, "filed": proxy.filed} for r in rejected_proxy]
        proxy_rejected_tables = [
            {**r, "accession": proxy.accession, "filed": proxy.filed} for r in rejected_proxy_tables
        ]
        proxy_same_name_rows = _proxy_same_name_rows(parsed_proxy)
        if not proxy_holders:
            warnings.append("DEF 14A: no ownership table parsed")
        elif not any(r.pct is not None and r.pct >= 5.0 for r in proxy_holders):
            warnings.append("DEF 14A: ownership table parsed, no row at 5% or more")
    else:
        proxy_rejected = []
        proxy_rejected_tables = []
        proxy_same_name_rows = []
    eight_ks = [r for r in rows if r.form == "8-K" and ten_k is not None and r.filed >= ten_k.filed]
    return Pulled(
        header=header,
        universe={k: v for k, v in urow.items() if k != "cik"},
        ten_k=ten_k,
        ten_k_files=sorted(files, key=lambda x: (x.get("name", ""), x.get("type", ""))),
        exhibit21=sorted(ex21, key=lambda x: (x.accession, x.order, norm_name(x.name))),
        exhibit21_file=ex21_file,
        exhibit21_files=sorted(ex21_files),
        holdings=sorted(
            holdings,
            key=lambda x: (x.accession, norm_name(x.holder_name), x.as_of or "", x.class_title or ""),
        ),
        stakes=sorted(
            stakes, key=lambda x: (x.accession, norm_name(x.holder_name), x.as_of or "", x.class_title or "")
        ),
        holdings_rows=len(rows13),
        holdings_capped=capped,
        proxy=proxy,
        proxy_holders=sorted(
            proxy_holders, key=lambda x: (x.accession, norm_name(x.name), x.as_of or "", x.class_title or "")
        ),
        proxy_rejected=sorted(
            proxy_rejected,
            key=lambda x: (x.get("accession", ""), norm_name(x.get("name", "")), x.get("reason", "")),
        ),
        proxy_rejected_tables=sorted(
            proxy_rejected_tables,
            key=lambda x: (x.get("accession", ""), x.get("header", ""), x.get("reason", "")),
        ),
        proxy_same_name_rows=proxy_same_name_rows,
        eight_ks=sorted(eight_ks, key=lambda x: (x.accession, x.filed)),
        warnings=sorted(warnings),
    )


def predecessors(path: Path | None) -> dict[str, dict[str, Any]]:
    """cik -> {predecessor_cik, kind, effective} from a JSON file `{"companies": [{"cik", "predecessor_cik", "predecessor": {"kind", "effective"}}]}`."""
    if not path:
        return {}
    comp = json.loads(path.read_text())["companies"]
    out = {}
    for r in comp:  # a list of rows, each carrying its cik
        if r.get("predecessor_cik"):
            po = r.get("predecessor") or {}
            out[r["cik"]] = {
                "predecessor_cik": r["predecessor_cik"],
                "kind": po.get("kind"),
                "effective": po.get("effective"),
            }
    return out


def pull(
    universe_path: Path,
    out_dir: Path = PULL_DIR,
    *,
    limit: int = 0,
    refresh: bool = False,
    predecessors_path: Path | None = None,
) -> None:
    e = Edgar(offline=refresh)
    rows = universe(universe_path)
    preds = predecessors(predecessors_path)
    universe_ciks = {r["cik"] for r in rows}
    if limit:
        rows = rows[:limit]
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    failures: list[str] = []
    proxy_table_audit_totals = {
        "tables_with_candidate_row": 0,
        "tables_with_candidate_row_neither_outcome": 0,
    }
    for i, urow in enumerate(rows, 1):
        path = out_dir / f"{urow['cik']}.json"
        if path.exists() and not refresh:
            existing = Pulled.model_validate_json(path.read_text())
            if existing.proxy and existing.proxy.primary_document:
                record_audit: dict[str, int] = {}
                raw = e.document(
                    existing.header.cik,
                    existing.proxy.accession,
                    existing.proxy.primary_document,
                ).decode("utf-8", "replace")
                parse_ownership_tables_audited(raw, audit_counts=record_audit)
                for key in proxy_table_audit_totals:
                    proxy_table_audit_totals[key] += record_audit.get(key, 0)
            continue
        try:
            record_audit = {}
            p = pull_company(
                e,
                urow,
                preds.get(urow["cik"]),
                universe_ciks,
                proxy_table_audit_counts=record_audit,
            )
        except Exception as ex:  # noqa: BLE001 - isolate a failed company so the pull reports all affected CIKs
            failures.append(urow["cik"])
            print(
                f"[{time.time() - t0:6.0f}s] {i}/{len(rows)} {urow['symbol']:<6} FAILED {type(ex).__name__}: {str(ex)[:160]}",
                flush=True,
            )
            continue
        for key in proxy_table_audit_totals:
            proxy_table_audit_totals[key] += record_audit.get(key, 0)
        # `class_note` and `class_conflict` are archive-enrichment fields, not pull-stage
        # observations. Keep the pull serialization stable with prior snapshots while the
        # archive builder still fills them from its cached class-conflict pass.
        non_pull_fields = {"__all__": {"class_note", "class_conflict"}}
        path.write_text(
            p.model_dump_json(
                indent=1,
                exclude={"holdings": non_pull_fields, "stakes": non_pull_fields},
            )
        )
        print(
            f"[{time.time() - t0:6.0f}s] {i}/{len(rows)} {urow['symbol']:<6} subs {len(p.exhibit21):>4} "
            f"holdings {len(p.holdings):>3} stakes {len(p.stakes):>3} 8-K {len(p.eight_ks):>3} net {e.requests} cache {e.hits}"
            + (f"  WARN {p.warnings}" if p.warnings else ""),
            flush=True,
        )
    summary = {
        "files": len(list(out_dir.glob("*.json"))),
        "cache_misses": e.misses,
        "cache_skipped_optional_paths": e.skips,
        "network_requests": e.requests,
        "cache_hits": e.hits,
        "failed_ciks": sorted(failures),
        "tables_with_candidate_row": proxy_table_audit_totals["tables_with_candidate_row"],
        "tables_with_candidate_row_and_neither_outcome": proxy_table_audit_totals[
            "tables_with_candidate_row_neither_outcome"
        ],
    }
    print(f"PULL SUMMARY {json.dumps(summary, sort_keys=True)}", flush=True)
    if out_dir != PULL_DIR:
        (out_dir.parent / f"{out_dir.name}-summary.json").write_text(
            json.dumps(summary, sort_keys=True, indent=1)
        )
    if failures or (e.offline and e.misses):  # online, a first read of a document is a miss and not an error
        raise SystemExit(f"pull incomplete: cache_misses={e.misses}, failed_companies={len(failures)}")


# -- archive -------------------------------------------------------------------------------


def _edgar_url(cik: str, accession: str, primary_document: str | None) -> str:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    return f"{base}/{primary_document}" if primary_document else f"{base}/{accession}-index.html"


def _desc_events(events: list[dict]) -> str:
    """One sentence for the description so a text search finds the event: `Since the 10-K: 8 8-K
    filings; Item 5.02 (Departure or Election of Directors or Officers...) on 2026-02-20, ...`"""
    if not events:
        return ""
    named = []
    for ev in events:
        for i, t in zip(ev["items"], ev["titles"], strict=True):
            if i in ("5.02", "1.01", "2.01", "1.03", "4.01", "5.01", "1.05"):  # the items a reader asks about
                named.append(f"Item {i} ({t}) on {ev['at']}")
    return f" Since the 10-K: {len(events)} 8-K filings" + ("; " + ", ".join(named) if named else "") + "."


def _event_items(items: str | None) -> list[str]:
    return sorted(i.strip() for i in (items or "").split(",") if i.strip())


def _desc_company(h: CompanyHeader, u: dict[str, str]) -> str:
    bits = [f"{h.name} ({', '.join(h.tickers)})" if h.tickers else h.name]
    if h.sic_description:
        bits.append(f"SIC {h.sic} {h.sic_description}")
    if h.state_of_incorporation:
        bits.append(f"incorporated in {h.state_of_incorporation}")
    return ". ".join(bits) + "."


def _class_key(class_title: str | None, cusip: str | None = None) -> str | None:
    """Prefer the security identifier; normalize the title when no CUSIP is available."""
    cusip_key = re.sub(r"\s+", "", cusip or "").casefold()
    if cusip_key:
        return f"cusip:{cusip_key}"
    title = (class_title or "").casefold()
    title = re.sub(r"(?<!\d)\.(?=\d)", "0.", title)
    title = re.sub(r"\bper\s+share\b", " ", title)
    number = r"(?:\d+(?:\.\d+)?|\.\d+)"
    par_value = re.search(
        rf"par\s+value\s+\$?\s*({number})|\$?\s*({number})\s+par\s+value",
        title,
    )
    if par_value:
        amount = Decimal(next(group for group in par_value.groups() if group)).normalize()
        title = f"{title[: par_value.start()]} par value ${format(amount, 'f')} {title[par_value.end() :]}"
    title = re.sub(r"[,;:()]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    return title or None


def _class_titles_compatible(left: str | None, right: str | None) -> bool:
    """Whether one title is a generic or fuller label for the same share class."""

    def parts(title: str | None) -> tuple[str | None, str]:
        normalized = _class_key(title) or ""
        designator = re.search(r"\bclass\s+([a-z0-9]+)\b", normalized)
        base = re.sub(r"\bclass\s+[a-z0-9]+\b", " ", normalized)
        base = re.sub(r"\bpar\s+value\s+\$?\s*[0-9.]+", " ", base)
        return (designator.group(1) if designator else None, re.sub(r"\s+", " ", base).strip())

    left_class, left_base = parts(left)
    right_class, right_base = parts(right)
    if left_class and right_class and left_class != right_class:
        return False
    return bool(
        left_base
        and right_base
        and (left_base == right_base or left_base in right_base or right_base in left_base)
    )


def _own_observation(hr: HoldingRow, *, as_of: str | None = None) -> dict[str, Any]:
    event_date = hr.as_of if as_of is None else as_of
    filed = hr.filed
    days = None
    if event_date and filed:
        try:
            days = (dt.date.fromisoformat(filed) - dt.date.fromisoformat(event_date)).days
        except ValueError:
            days = None
    reasons = []
    if hr.pct is not None and hr.pct >= 5 and (hr.shares is None or hr.shares < 1000):
        reasons.append("shares missing or below 1000 while pct is at least 5")
    if days is not None and days > 400:
        reasons.append(f"event date precedes filing date by {days} days")
    return {
        "accession": hr.accession,
        "holder_cik": hr.holder_cik,
        "form": hr.form,
        "pct": hr.pct,
        "shares": hr.shares,
        "as_of": event_date,
        "filed": filed,
        "source": "xml" if hr.parsed_from == "xml" else "headers-only",
        "reporting_person_types": sorted(
            set(hr.reporting_person_types or ([hr.reporting_person_type] if hr.reporting_person_type else []))
        ),
        "five_pct_or_less": hr.five_pct_or_less,
        "purpose_len": len(hr.purpose) if hr.purpose is not None else None,
        "class": hr.class_title,
        "class_note": hr.class_note,
        "class_conflict": hr.class_conflict,
        "cusip": hr.cusip,
        "parsed_from": hr.parsed_from,
        "suspect": bool(reasons),
        "suspect_reason": "; ".join(reasons) if reasons else None,
        "filer_split_signal": hr.filer_split_signal,
        "member_of_group": hr.member_of_group,
        "intent": "active" if "13D" in hr.form else "passive",
        "rule": hr.rule,
        "purpose": hr.purpose,
    }


def _proxy_observation(pr: ProxyHolderRow) -> dict[str, Any]:
    return {
        "accession": pr.accession,
        "form": "DEF 14A",
        "pct": pr.pct,
        "shares": pr.shares,
        "as_of": pr.as_of,
        "filed": pr.filed,
        "source": "proxy-table",
        "table": pr.table,
        "raw": pr.raw,
        "suspect": pr.suspect,
        "suspect_reason": pr.suspect_reason,
        "proxy_table_conflict": pr.proxy_table_conflict,
        "reporting_person_types": [],
        "five_pct_or_less": False,
        "purpose_len": None,
        "class": pr.class_title,
        "cusip": None,
        "as_of_source": pr.as_of_source,
    }


def _ordered_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda o: (o.get("as_of") or "", o.get("filed") or "", o.get("accession") or ""),
        reverse=True,
    )


def _merger_exit_note(exit_row: dict[str, Any], purpose: str | None = None) -> str | None:
    shares, pct = exit_row.get("shares"), exit_row.get("pct")
    if shares != 0 and not (shares is None and pct == 0):
        return None
    text = purpose or exit_row.get("purpose") or ""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    transaction = re.compile(
        r"\b(?:merger|the\s+combination|business\s+combination|acquisition\b.{0,180}?\bcompleted)\b",
        re.IGNORECASE | re.DOTALL,
    )
    disposition = re.compile(r"\b(?:disposed|cancelled|converted|exchanged)\b", re.IGNORECASE)
    for sentence in sentences:
        if transaction.search(sentence) and disposition.search(sentence):
            return " ".join(sentence.split())[:300]
    return None


def _exit_kind(
    exit_row: dict[str, Any],
    previous: dict[str, Any] | None,
    same_day_related_filer: bool = False,
    same_day_transfer: bool = False,
    purpose: str | None = None,
) -> str:
    if exit_row.get("filer_split_signal") or same_day_related_filer:
        return "filer_split"
    if same_day_transfer:
        return "transferred"
    if _merger_exit_note(exit_row, purpose):
        return "merger"
    old_shares, new_shares = (previous or {}).get("shares"), exit_row.get("shares")
    old_pct, new_pct = (previous or {}).get("pct"), exit_row.get("pct")
    if old_shares and new_shares is not None and new_shares < old_shares * 0.99:
        return "sold"
    if (
        old_shares is not None
        and new_shares is not None
        and new_shares >= old_shares * 0.99
        and old_pct is not None
        and new_pct is not None
        and new_pct < old_pct
    ):
        return "diluted"
    if (
        old_shares
        and new_shares is not None
        and abs(new_shares / old_shares - 1) <= 0.01
        and new_pct is not None
        and 4.5 <= new_pct < 5
    ):
        return "drift"
    if exit_row.get("five_pct_or_less") and new_pct is not None and new_pct >= 5.0:
        return "threshold_flag"
    return "unknown"


def _ownership_metadata(
    own: list[dict[str, Any]],
    proxy: list[dict[str, Any]],
    *,
    same_day_related_filer: bool = False,
    same_day_transfer: bool = False,
    group_exit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], float]:
    own_newest = _ordered_observations([o for o in own if not o.get("suspect")])
    proxy_newest = _ordered_observations([o for o in proxy if not o.get("suspect")])
    all_observations = _ordered_observations(own + proxy)
    all_newest = _ordered_observations([o for o in own + proxy if not o.get("suspect")])
    state = own_newest[0] if own_newest else proxy_newest[0] if proxy_newest else {}
    metadata = {
        k: state.get(k)
        for k in (
            "pct",
            "shares",
            "as_of",
            "filed",
            "form",
            "accession",
            "class",
            "cusip",
            "member_of_group",
            "intent",
            "rule",
            "parsed_from",
            "class_note",
            "class_conflict",
        )
    }
    metadata["parsed_from"] = (
        "proxy-table" if state.get("source") == "proxy-table" else state.get("parsed_from")
    )
    d_observations = [o for o in own_newest if "13D" in o.get("form", "")]
    metadata["purpose"] = d_observations[0].get("purpose") if d_observations else None
    metadata["last_asserted"] = all_newest[0].get("as_of") if all_newest else None
    dates = [o["as_of"] for o in own + proxy if not o.get("suspect") and o.get("as_of")]
    metadata["valid_from"] = min(dates) if dates else None
    metadata["valid_to"] = None
    metadata["exit_filed"] = None
    metadata["exit_accession"] = None
    metadata["exit_prior_accession"] = None
    metadata["exit_kind"] = None
    metadata["exit_note"] = None
    own_exits = [
        o for o in own_newest if o.get("pct") is not None and o["pct"] < 5 or o.get("five_pct_or_less")
    ]
    if own_exits and own_newest and own_exits[0]["accession"] == own_newest[0]["accession"]:
        exit_row = own_exits[0]
        earlier = [
            o
            for o in own_newest
            if (o.get("as_of") or "", o.get("filed") or "", o.get("accession") or "")
            < (exit_row.get("as_of") or "", exit_row.get("filed") or "", exit_row.get("accession") or "")
        ]
        previous = earlier[0] if earlier else None
        metadata["exit_prior_accession"] = previous.get("accession") if previous else None
        metadata["valid_to"] = exit_row.get("as_of")
        metadata["exit_filed"] = exit_row.get("filed")
        metadata["exit_accession"] = exit_row.get("accession")
        purpose = d_observations[0].get("purpose") if d_observations else None
        exit_note = _merger_exit_note(exit_row, purpose)
        metadata["exit_kind"] = _exit_kind(
            exit_row,
            previous,
            same_day_related_filer,
            same_day_transfer,
            purpose,
        )
        metadata["exit_note"] = exit_note if metadata["exit_kind"] == "merger" else None
    if group_exit:
        latest_own = own_newest[0] if own_newest else None
        own_rank = (
            (latest_own.get("as_of") or "", latest_own.get("filed") or "", latest_own.get("accession") or "")
            if latest_own
            else ("", "", "")
        )
        group_rank = (
            group_exit.get("as_of") or "",
            group_exit.get("filed") or "",
            group_exit.get("accession") or "",
        )
        newer_live_own = bool(
            latest_own
            and own_rank > group_rank
            and latest_own.get("pct") is not None
            and latest_own["pct"] >= 5.0
            and not latest_own.get("five_pct_or_less")
        )
        own_threshold_exit = bool(
            latest_own
            and (
                latest_own.get("pct") is not None
                and latest_own["pct"] < 5.0
                or latest_own.get("five_pct_or_less")
            )
        )
        if not newer_live_own and not own_threshold_exit:
            metadata["valid_to"] = group_exit.get("as_of")
            metadata["exit_filed"] = group_exit.get("filed")
            metadata["exit_accession"] = group_exit.get("accession")
            metadata["exit_prior_accession"] = group_exit.get("prior_accession")
            metadata["exit_kind"] = "departed"
            metadata["exit_note"] = None
    proxy_latest = proxy_newest[0] if proxy_newest else None
    if own_newest and proxy_latest and proxy_latest.get("pct") != own_newest[0].get("pct"):
        metadata["proxy_disagrees"] = {"pct": proxy_latest.get("pct"), "as_of": proxy_latest.get("as_of")}
    else:
        metadata["proxy_disagrees"] = None
    suspect = [o for o in own + proxy if o.get("suspect")]
    metadata["suspect"] = bool(suspect)
    metadata["suspect_reason"] = (
        "; ".join(sorted({o["suspect_reason"] for o in suspect if o.get("suspect_reason")})) or None
    )
    observation_fields = (
        "accession",
        "holder_cik",
        "form",
        "pct",
        "shares",
        "as_of",
        "filed",
        "source",
        "table",
        "raw",
        "proxy_table_conflict",
        "reporting_person_types",
        "five_pct_or_less",
        "purpose_len",
        "class",
        "cusip",
        "as_of_source",
        "class_assumed",
        "suspect",
        "suspect_reason",
        "filer_split_signal",
        "class_note",
        "class_conflict",
        "resolved_by",
    )
    metadata["observations"] = [{k: o.get(k) for k in observation_fields if k in o} for o in all_observations]
    metadata["asserted"] = sorted({o["accession"] for o in all_observations})
    pct = metadata.get("pct")
    return metadata, min(1.0, ((pct or 5.0) / 100) + 0.5)


def _enrich_cached_class_notes(pulled: list[Pulled]) -> list[dict[str, str]]:
    """Apply class-comment annotations from locally cached primary XML; never fetch documents."""
    parsed_by_accession: dict[str, list[HoldingRow]] = {}
    raw_hashes: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    for company in pulled:
        for row in company.holdings + company.stakes:
            if row.class_conflict and row.class_note:
                continue
            accession = row.accession
            issuer_cik = row.issuer_cik or company.header.cik
            cache_path = (
                ROOT
                / "data/external/edgar/www.sec.gov/Archives/edgar/data"
                / str(int(issuer_cik))
                / accession.replace("-", "")
                / "primary_doc.xml"
            )
            if not cache_path.is_file():
                continue
            if accession not in parsed_by_accession:
                raw = cache_path.read_bytes()
                parsed_by_accession[accession] = parse_13_xml(
                    raw,
                    FilingRow(form=row.form, filed=row.filed, accession=accession),
                )
                raw_hashes[accession] = hashlib.sha256(raw).hexdigest()
            parsed = parsed_by_accession[accession]
            matching = [
                candidate
                for candidate in parsed
                if (
                    row.holder_cik
                    and candidate.holder_cik == row.holder_cik
                    or not row.holder_cik
                    and cik_name_key_for_enrichment(candidate.holder_name)
                    == cik_name_key_for_enrichment(row.holder_name)
                )
            ]
            conflict = next((candidate for candidate in matching if candidate.class_conflict), None)
            if conflict:
                row.class_note = conflict.class_note
                row.class_conflict = True
                source_hashes[accession] = raw_hashes[accession]
    return [{"accession": accession, "sha256": digest} for accession, digest in sorted(source_hashes.items())]


def cik_name_key_for_enrichment(name: str) -> str:
    return ".".join(re.findall(r"[a-z0-9]+", norm_name(name)))


def archive(pull_dir: Path, out: Path, *, universe_path: Path) -> Path:
    """Entities and edges from the pulled rows; refuses an existing `out`."""
    for p in (out, out.with_suffix(".generation.json")):
        if p.exists():
            raise SystemExit(f"refusing to replace {p}; pick a new --out")
    files = sorted(pull_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"nothing pulled under {pull_dir}")
    ents: dict[str, Entity] = {}
    edges: list[Edge] = []
    pulled = [Pulled.model_validate_json(f.read_text()) for f in files]
    class_note_sources = _enrich_cached_class_notes(pulled)
    name_ciks: dict[str, set[str]] = {}
    target_name_ciks: dict[tuple[str, str], set[str]] = {}
    target_name_cik_counts: dict[tuple[str, str], dict[str, int]] = {}
    target_name_cik_latest: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    cik_holder_names: dict[str, set[str]] = {}
    cik_identity_names: dict[str, dict[str, set[str]]] = {}
    cik_accession_keys: dict[str, dict[str, set[str]]] = {}
    cik_name_dates: dict[str, dict[str, list[str]]] = {}
    cik_name_sources: dict[tuple[str, str], set[str]] = {}
    cik_proxy_legal_names: dict[str, set[tuple[tuple[str, ...], str | None]]] = {}
    cik_own_accessions: dict[str, set[str]] = {}
    cik_latest_own: dict[str, tuple[str, str, str]] = {}
    company_cik_latest_own: dict[tuple[str, str], tuple[str, str, str]] = {}
    person_names: set[str] = set()
    accession_people: dict[str, list[tuple[str | None, str]]] = {}
    universe_ciks = {p.header.cik for p in pulled}
    issuer_header_names = {p.header.cik: {p.header.name, *p.header.former_names} for p in pulled}

    def cik_name_key(name: str) -> str:
        aliases = {"corp": "corporation", "inc": "incorporated", "co": "company"}
        normalized = ".".join(aliases.get(token, token) for token in norm_name(name).split("."))
        return normalized.removeprefix("the.")

    def proxy_name_tokens(name: str) -> list[str]:
        value = name.strip()
        while True:
            prior = value
            value = re.sub(r"\s*\(\s*(?:[a-hj-z]|[1-9]\d?)\s*\)\s*$", "", value, flags=re.IGNORECASE)
            value = re.sub(r"\s*\(\s*[\"'“‘].*?[\"'”’]\s*\)\s*$", "", value)
            value = re.sub(r"\s+(?:[a-hj-z])\.?\s*$", "", value, flags=re.IGNORECASE)
            value = re.sub(r"\s*\(\s*and\s+subsidiaries\s*\)\s*$", "", value, flags=re.IGNORECASE)
            value = re.sub(
                r"(?:^|\s*,?\s+)and(?:/or)?\s+(?:certain of its affiliates|certain related entities|"
                r"certain other entities|affiliates|its affiliates|subsidiaries)\s*$|"
                r"(?:^|\s*,?\s+)and\s*[,.]?\s*$",
                "",
                value,
                flags=re.IGNORECASE,
            )
            if value == prior:
                break
        tokens = re.findall(r"[a-z0-9]+", norm_name(value))
        if tokens[:1] == ["the"]:
            tokens = tokens[1:]
        return tokens

    def proxy_name_key(name: str) -> str:
        tokens = proxy_name_tokens(name)
        suffixes = (
            ("inc",),
            ("incorporated",),
            ("llc",),
            ("l", "l", "c"),
            ("lp",),
            ("l", "p"),
            ("limited", "partnership"),
            ("ltd",),
            ("limited",),
            ("corp",),
            ("corporation",),
            ("co",),
            ("company",),
            ("plc",),
        )
        for suffix in suffixes:
            if tuple(tokens[-len(suffix) :]) == suffix:
                tokens = tokens[: -len(suffix)]
                break
        return ".".join(tokens)

    def proxy_legal_name_key(name: str) -> tuple[tuple[str, ...], str | None]:
        value = re.sub(r"(?i)^entities\s+associated\s+with\s+", "", name.strip())
        return proxy_legal_name_identity(value)

    def proxy_rejection_reason(name: str) -> str | None:
        key = proxy_name_key(name)
        if not key:
            return "name empty after stripping markers and qualifiers"
        if (
            "subsidiaries.or.business.divisions" in key
            or "directors.and.executive.officers.as.a.group" in key
        ):
            return "name is a description, not a holder"
        return None

    def names_are_variants(a: str, b: str) -> bool:
        ka, kb = cik_name_key(a), cik_name_key(b)
        if ka == kb or ka.startswith(kb) or kb.startswith(ka):
            return True
        ta = re.findall(r"[a-z]+", ka)
        tb = re.findall(r"[a-z]+", kb)
        return bool(ta and tb and ta[0] == tb[0] and ta[-1] == tb[-1])

    for p in pulled:
        for hr in p.holdings + p.stakes:
            if hr.holder_name == "?":
                continue
            name = cik_name_key(hr.holder_name)
            accession_people.setdefault(hr.accession, []).append((hr.holder_cik, hr.holder_name))
            if "IN" in {
                x.upper()
                for x in hr.reporting_person_types
                or ([hr.reporting_person_type] if hr.reporting_person_type else [])
            }:
                person_names.add(name)
            if not hr.holder_cik:
                continue
            name_ciks.setdefault(name, set()).add(hr.holder_cik)
            identity_key = proxy_name_key(hr.holder_name)
            legal_key = proxy_legal_name_key(hr.holder_name)
            if legal_key:
                cik_proxy_legal_names.setdefault(hr.holder_cik, set()).add(legal_key)
            if identity_key:
                cik_identity_names.setdefault(hr.holder_cik, {}).setdefault(identity_key, set()).add(
                    hr.holder_name
                )
                cik_accession_keys.setdefault(hr.holder_cik, {}).setdefault(hr.accession, set()).add(
                    identity_key
                )
                cik_name_dates.setdefault(hr.holder_cik, {}).setdefault(hr.holder_name, []).append(
                    hr.as_of or ""
                )
                name_source = (
                    "issuer_header"
                    if not hr.as_of and hr.holder_name in issuer_header_names.get(hr.holder_cik, set())
                    else hr.parsed_from
                )
                cik_name_sources.setdefault((hr.holder_cik, hr.holder_name), set()).add(name_source)
            cik_own_accessions.setdefault(hr.holder_cik, set()).add(hr.accession)
            target_cik = p.header.cik if any(hr is item for item in p.holdings) else hr.issuer_cik
            if target_cik:
                holder_key = proxy_name_key(hr.holder_name)
                if holder_key:
                    cik_holder_names.setdefault(hr.holder_cik, set()).add(holder_key)
                filing_rank = (hr.filed or "", hr.as_of or "", hr.accession)
                cik_latest_own[hr.holder_cik] = max(
                    cik_latest_own.get(hr.holder_cik, ("", "", "")), filing_rank
                )
                company_cik_latest_own[(target_cik, hr.holder_cik)] = max(
                    company_cik_latest_own.get((target_cik, hr.holder_cik), ("", "", "")), filing_rank
                )
                target_name_ciks.setdefault((target_cik, name), set()).add(hr.holder_cik)
                counts = target_name_cik_counts.setdefault((target_cik, name), {})
                counts[hr.holder_cik] = counts.get(hr.holder_cik, 0) + 1
                target_name_cik_latest[(target_cik, name, hr.holder_cik)] = max(
                    target_name_cik_latest.get((target_cik, name, hr.holder_cik), ("", "", "")),
                    (hr.as_of or "", hr.filed or "", hr.accession),
                )

    # Some cached accessions omit reportingPersonCIK for a person who has it on another
    # accession. Resolve only unique same-name CIKs so co-reporting still drives CIK splits.
    for company in pulled:
        for row in company.holdings + company.stakes:
            target_cik = (
                company.header.cik if any(row is item for item in company.holdings) else row.issuer_cik
            )
            identity_key = proxy_name_key(row.holder_name)
            if not target_cik or not identity_key:
                continue
            candidates = (
                {row.holder_cik}
                if row.holder_cik
                else set(target_name_ciks.get((target_cik, cik_name_key(row.holder_name)), set()))
            )
            if not candidates:
                candidates = set(name_ciks.get(cik_name_key(row.holder_name), set()))
            if len(candidates) != 1:
                continue
            cik = next(iter(candidates))
            cik_identity_names.setdefault(cik, {}).setdefault(identity_key, set()).add(row.holder_name)
            cik_accession_keys.setdefault(cik, {}).setdefault(row.accession, set()).add(identity_key)
            cik_name_dates.setdefault(cik, {}).setdefault(row.holder_name, []).append(row.as_of or "")
            cik_proxy_legal_names.setdefault(cik, set()).add(proxy_legal_name_key(row.holder_name))
            name_source = (
                "issuer_header"
                if not row.as_of and row.holder_name in issuer_header_names.get(cik, set())
                else row.parsed_from
            )
            cik_name_sources.setdefault((cik, row.holder_name), set()).add(name_source)

    multi_person_accessions: set[str] = set()
    shared_cik_accessions = {
        cik: sorted(accession for accession, keys in accessions.items() if len(keys) > 1)
        for cik, accessions in cik_accession_keys.items()
    }
    scoped_ciks: set[str] = {
        cik for cik, accessions in shared_cik_accessions.items() if cik not in universe_ciks and accessions
    }
    for accession, people in accession_people.items():
        unique_people = {(cik, cik_name_key(name), name) for cik, name in people}
        names = sorted({normalized for _, normalized, _ in unique_people})
        if any(not names_are_variants(a, b) for i, a in enumerate(names) for b in names[i + 1 :]):
            multi_person_accessions.add(accession)

    name_keyed_owner_uris: dict[tuple[str, str], set[str]] = {}
    for company in pulled:
        for row in company.holdings + company.stakes:
            if row.holder_cik or row.holder_name == "?":
                continue
            target_cik = (
                company.header.cik if any(row is item for item in company.holdings) else row.issuer_cik
            )
            if not target_cik:
                continue
            key = cik_name_key(row.holder_name)
            known_ciks = set(target_name_ciks.get((target_cik, key), set()))
            if row.accession in multi_person_accessions:
                known_ciks = known_ciks or set(name_ciks.get(key, set()))
                if len(known_ciks) == 1:
                    continue
            else:
                if not known_ciks:
                    known_ciks = {
                        cik
                        for (target, known), ciks in target_name_ciks.items()
                        if target == target_cik and cik_name_key(known.replace(".", " ")) == key
                        for cik in ciks
                    }
                if not known_ciks:
                    known_ciks = set(name_ciks.get(key, set()))
                if known_ciks:
                    continue
            types = {
                value.upper()
                for value in row.reporting_person_types
                or ([row.reporting_person_type] if row.reporting_person_type else [])
            }
            kind = "person" if "IN" in types or key in person_names else "org"
            name_keyed_owner_uris.setdefault((target_cik, key), set()).add(f"urn:filings-graph:{kind}:{key}")

    def cik_holder_uri(cik: str, name: str) -> str:
        if cik in universe_ciks:
            return f"urn:sec:cik:{cik}"
        if cik in scoped_ciks:
            return f"urn:sec:cik:{cik}:{proxy_name_key(name)}"
        return f"urn:sec:cik:{cik}"

    cik_split_details = [
        {
            "cik": cik,
            "keys": sorted(cik_identity_names[cik]),
            "holder_uris": sorted(
                cik_holder_uri(cik, min(cik_identity_names[cik][key])) for key in cik_identity_names[cik]
            ),
            "names": sorted(name for names in cik_identity_names[cik].values() for name in names),
            "shared_accessions": shared_cik_accessions[cik],
        }
        for cik in sorted(scoped_ciks)
    ]

    def alias_date_entry(cik: str, name: str) -> dict[str, str]:
        dates = sorted({value for value in cik_name_dates.get(cik, {}).get(name, []) if value})
        sources = cik_name_sources.get((cik, name), set())
        entry = {
            "name": name,
            "source": "holder_observation"
            if dates
            else "issuer_header"
            if "issuer_header" in sources
            else min(sources, default="unknown"),
        }
        if dates:
            entry["first_as_of"] = dates[0]
            entry["last_as_of"] = dates[-1]
        return entry

    cik_alias_merges = [
        {
            "cik": cik,
            "uri": f"urn:sec:cik:{cik}",
            "names": sorted(name for names in names_by_key.values() for name in names),
            "first_last_as_of": [alias_date_entry(cik, name) for name in sorted(cik_name_dates.get(cik, {}))],
        }
        for cik, names_by_key in sorted(cik_identity_names.items())
        if cik not in scoped_ciks and len(names_by_key) > 1
    ]

    bare_brands = {"blackrock", "vanguard", "state.street", "fidelity"}
    bare_brand_ciks = {
        "blackrock": "0002012383",
        "vanguard": "0000102909",
        "state.street": "0000093751",
        "fidelity": "0000315066",
    }
    unresolved_bare_brands: set[tuple[str, str]] = set()
    brand_resolved_rows: set[tuple[str, str, str]] = set()

    def proxy_holder_cik(name: str, target_cik: str) -> str | None:
        key = proxy_name_key(name)
        legal_key = proxy_legal_name_key(name)
        brand_key = cik_name_key(name)
        bare_brand = brand_key in bare_brands
        if brand_key in bare_brand_ciks:
            return bare_brand_ciks[brand_key]
        if bare_brand:
            candidates = {
                cik
                for cik, names in cik_holder_names.items()
                for known in names
                if (known.startswith(key) or key.startswith(known))
            }
        else:
            candidates = {
                cik
                for cik, names in cik_proxy_legal_names.items()
                if any(proxy_legal_names_match(legal_key, known) for known in names)
            }
        if bare_brand:
            on_company = candidates & {
                cik for company, cik in company_cik_latest_own if company == target_cik
            }
            if len(on_company) == 1:
                return next(iter(on_company))
            if candidates:
                return max(
                    candidates,
                    key=lambda cik: (
                        len(cik_own_accessions.get(cik, set())),
                        cik_latest_own.get(cik, ("", "", "")),
                        -int(cik),
                    ),
                )
            unresolved_bare_brands.add((target_cik, name))
            return None
        if not candidates:
            return None
        on_company = candidates & {cik for company, cik in company_cik_latest_own if company == target_cik}
        pool = on_company or candidates
        return max(
            pool,
            key=lambda cik: (
                company_cik_latest_own.get((target_cik, cik), ("", "", "")),
                cik_latest_own.get(cik, ("", "", "")),
                -int(cik),
            ),
        )

    def holder_uri(
        name: str,
        target_cik: str,
        *,
        is_person: bool = False,
        holder_cik: str | None = None,
        force_name_uri: bool = False,
        is_proxy: bool = False,
    ) -> tuple[str, str | None]:
        k = cik_name_key(name)
        if is_proxy:
            resolved_cik = proxy_holder_cik(name, target_cik)
            if resolved_cik:
                return cik_holder_uri(resolved_cik, name), "brand" if k in bare_brands else None
            existing_name_uris = name_keyed_owner_uris.get((target_cik, k), set())
            if existing_name_uris:
                uri = next(
                    (
                        candidate
                        for candidate in sorted(existing_name_uris)
                        if candidate.startswith("urn:filings-graph:person:")
                    ),
                    min(existing_name_uris),
                )
                return uri, None
            kind = "person" if k in person_names else "org"
            return f"urn:filings-graph:{kind}:{k}", None
        if force_name_uri:
            # A missing reportingPersonCIK must not inherit the lead filer's CIK. Reuse one
            # independently reported CIK for this exact name when it exists elsewhere.
            known_ciks = set(target_name_ciks.get((target_cik, k), set())) or set(name_ciks.get(k, set()))
            if len(known_ciks) == 1:
                return cik_holder_uri(next(iter(known_ciks)), name), None
            kind = "person" if is_person or k in person_names else "org"
            return f"urn:filings-graph:{kind}:{k}", None
        candidates = set(target_name_ciks.get((target_cik, k), set()))
        if not candidates:
            for (company, known), ciks in target_name_ciks.items():
                if company == target_cik and cik_name_key(known.replace(".", " ")) == k:
                    candidates.update(ciks)
        if holder_cik:
            candidates.add(holder_cik)

        def rank_cik(cik: str) -> tuple[int, int, tuple[str, str, str], int]:
            matching = [
                (known, by_cik[cik])
                for (company, known), by_cik in target_name_cik_counts.items()
                if company == target_cik
                and cik in by_cik
                and (known == k or cik_name_key(known.replace(".", " ")) == k)
            ]
            exact_count = target_name_cik_counts.get((target_cik, k), {}).get(cik, 0)
            count = sum(n for _, n in matching)
            latest = max(
                (target_name_cik_latest.get((target_cik, known, cik), ("", "", "")) for known, _ in matching),
                default=("", "", ""),
            )
            return (exact_count, count, latest, -int(cik))

        if candidates:
            # Conflicting CIK assignments for the same issuer/name resolve to the most frequently
            # evidenced CIK, then its latest own assertion, with the smaller CIK as stable tie-break.
            best = max(candidates, key=rank_cik)
            return cik_holder_uri(best, name), None
        if not candidates:
            candidates = set(name_ciks.get(k, set()))
        if candidates:
            return cik_holder_uri(min(candidates), name), None
        kind = "person" if is_person or k in person_names else "org"
        return f"urn:filings-graph:{kind}:{k}", None

    def ent(uri: str, **kw) -> Entity:
        if uri not in ents:
            ents[uri] = Entity(uri=uri, **kw)
        return ents[uri]

    owner_rows: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    holder_facts: dict[str, dict[str, Any]] = {}
    group_accessions: dict[tuple[str, str | None, str], dict[str, Any]] = {}

    def add_owner(
        name: str,
        holder_cik: str | None,
        target_uri: str,
        target_cik: str,
        class_title: str | None,
        observation: dict[str, Any],
        *,
        reporting_person_types: list[str] | None = None,
        jurisdiction: str | None = None,
        raw: str | None = None,
        proxy_alias: str | None = None,
    ) -> None:
        types = set(reporting_person_types or observation.get("reporting_person_types", []))
        force_name_uri = (
            holder_cik is None
            and observation.get("source") != "proxy-table"
            and observation.get("accession") in multi_person_accessions
        )
        uri, resolved_by = holder_uri(
            name,
            target_cik,
            is_person="IN" in {t.upper() for t in types},
            holder_cik=holder_cik,
            force_name_uri=force_name_uri,
            is_proxy=observation.get("source") == "proxy-table",
        )
        if resolved_by:
            observation["resolved_by"] = resolved_by
            brand_resolved_rows.add((target_cik, name, uri))
        class_identity = _class_key(class_title, observation.get("cusip"))
        key = (uri, target_uri, class_identity)
        group = owner_rows.setdefault(
            key,
            {"name": name, "cik": holder_cik, "class_title": class_title, "own": [], "proxy": [], "raw": []},
        )
        group["name"] = min(group["name"], name, key=lambda n: (len(n), norm_name(n)))
        group["class_title"] = group["class_title"] or class_title
        if observation["source"] == "proxy-table":
            observation["class"] = observation.get("class") or class_title
            if raw:
                observation["line"] = raw
            group["proxy"].append(observation)
        else:
            group["own"].append(observation)
        fact = holder_facts.setdefault(
            uri,
            {
                "name": name,
                "names": set(),
                "name_rank": (-1, "", "", ""),
                "cik": holder_cik,
                "types": set(),
                "jurisdiction": jurisdiction,
                "person": False,
                "proxy_aliases": set(),
            },
        )
        fact["names"].add(name)
        if observation.get("source") == "proxy-table" and uri.startswith("urn:sec:cik:"):
            fact["proxy_aliases"].add(proxy_alias or name)
        own_name = observation.get("source") != "proxy-table"
        name_rank = (
            1 if own_name else 0,
            observation.get("as_of") or "",
            observation.get("filed") or "",
            observation.get("accession") or "",
        )
        if name_rank > fact["name_rank"]:
            fact["name"] = name
            fact["name_rank"] = name_rank
        if not fact["cik"]:
            fact["cik"] = holder_cik or (uri.split(":")[3] if uri.startswith("urn:sec:cik:") else None)
        fact["types"].update(types)
        fact["person"] = (
            fact["person"] or "IN" in {t.upper() for t in types} or norm_name(name) in person_names
        )
        fact["jurisdiction"] = fact["jurisdiction"] or jurisdiction
        if raw:
            group["raw"].append(raw)

    # companies first: a company another file reports a stake in, or that files as a holder, would
    # otherwise be minted as a thin issuer/holder entity by the earlier file
    for p in pulled:
        h = p.header
        cu = f"urn:sec:cik:{h.cik}"
        # Filings are attachments of the company, not graph nodes; an
        # 8-K is split into the company's own record (one event row per filing, a sentence in the
        # description). The graph holds companies, subsidiaries, holders, issuers and industries only.
        events = [
            {
                "at": r.filed,
                "form": "8-K",
                "items": items,
                "titles": [EIGHT_K_ITEMS.get(i, "") for i in items],
                "accession": r.accession,
                "url": _edgar_url(h.cik, r.accession, r.primary_document),
            }
            for r in sorted(p.eight_ks, key=lambda r: r.accession)
            for items in [_event_items(r.items)]
        ]
        filings = [
            {
                "form": r.form,
                "filed": r.filed,
                "period": r.period,
                "accession": r.accession,
                "primary_document": r.primary_document,
                "url": _edgar_url(h.cik, r.accession, r.primary_document),
            }
            for r in sorted((r for r in (p.ten_k, p.proxy) if r), key=lambda r: r.accession)
        ]
        ent(
            cu,
            entity_kind="org",
            entity_type="company",
            name=h.name,
            description=_desc_company(h, p.universe) + _desc_events(events),
            properties={
                "uri": cu,
                "cik": h.cik,
                "tickers": h.tickers,
                "exchanges": h.exchanges,
                "sic": h.sic,
                "filings": filings,
                "events": events,
                "jurisdiction": h.state_of_incorporation,
                "lei": h.lei,
                "ein": h.ein,
                "fiscal_year_end": h.fiscal_year_end,
                "former_names": h.former_names,
                "predecessor_cik": h.predecessor_cik,
                "predecessor_kind": h.predecessor_kind,
                "predecessor_effective": h.predecessor_effective,
                "proxy_rejected": list(p.proxy_rejected),
                "proxy_rejected_tables": list(p.proxy_rejected_tables),
                "type": "company",
            },
            tags=["company"],
        )
    for p in pulled:
        h = p.header
        cu = f"urn:sec:cik:{h.cik}"
        if h.sic:
            iu = f"urn:sec:sic:{h.sic}"
            ent(
                iu,
                entity_kind="concept",
                entity_type="industry",
                name=f"SIC {h.sic} {h.sic_description or ''}".strip(),
                description=f"SIC industry {h.sic}: {h.sic_description or ''}.",
                properties={"uri": iu, "sic": h.sic, "type": "industry"},
                tags=["industry"],
            )
            edges.append(
                Edge(
                    source=cu,
                    target=iu,
                    relation="instance_of",
                    metadata={"predicate": "in_industry", "source": "submissions-header"},
                )
            )
        for s in p.exhibit21:
            su = f"urn:filings-graph:sub:{h.cik}:{norm_name(s.name)}"
            ent(
                su,
                entity_kind="org",
                entity_type="subsidiary",
                name=s.name,
                description=f"{s.name}, a subsidiary of {h.name}"
                + (f", organized in {s.jurisdiction}" if s.jurisdiction else "")
                + ".",
                properties={
                    "uri": su,
                    "jurisdiction": s.jurisdiction,
                    "parent_cik": h.cik,
                    "type": "subsidiary",
                },
                tags=["subsidiary"],
            )
            edges.append(
                Edge(
                    source=su,
                    target=cu,
                    relation="part_of",
                    metadata={
                        "predicate": "subsidiary_of",
                        "source": "exhibit-21",
                        "control": True,
                        "ownership_pct": s.ownership_pct,
                        "jurisdiction": s.jurisdiction,
                        "layer": 1,
                        "asserted": [s.accession],
                        "valid_from": p.ten_k.period if p.ten_k else None,
                        "last_asserted": p.ten_k.period if p.ten_k else None,
                        "valid_to": None,
                        "line": s.line,
                    },
                )
            )
        for hr in p.stakes:
            # the pulled company is the filer: an outbound stake in another issuer
            if not hr.issuer_cik:
                continue
            iu2 = f"urn:sec:cik:{hr.issuer_cik}"
            ent(
                iu2,
                entity_kind="org",
                entity_type="issuer",
                name=hr.issuer_name or hr.issuer_cik,
                description=f"{hr.issuer_name or hr.issuer_cik}, an SEC registrant in which a company of the universe reports a stake.",
                properties={"uri": iu2, "cik": hr.issuer_cik, "type": "issuer"},
                tags=["issuer"],
            )
            add_owner(
                hr.holder_name,
                hr.holder_cik,
                iu2,
                hr.issuer_cik,
                hr.class_title,
                _own_observation(hr),
                reporting_person_types=hr.reporting_person_types
                or ([hr.reporting_person_type] if hr.reporting_person_type else []),
                jurisdiction=hr.holder_jurisdiction,
            )
        for pr in p.proxy_holders:
            proxy_name = _strip_trailing_bracketed_markers(pr.name).strip(" ,;")
            reason = proxy_rejection_reason(proxy_name)
            if reason:
                rejected = {
                    "name": proxy_name,
                    "reason": reason,
                    "raw": pr.raw,
                    "accession": pr.accession,
                    "filed": pr.filed or "",
                }
                p.proxy_rejected.append(rejected)
                ents[cu].properties["proxy_rejected"].append(rejected)
                continue
            if pr.pct is None or pr.pct < 5.0:
                continue
            target_uri = cu
            add_owner(
                proxy_name,
                None,
                target_uri,
                h.cik,
                pr.class_title,
                _proxy_observation(pr),
                raw=pr.raw,
                proxy_alias=pr.name,
            )
        for hr in p.holdings:
            observation = _own_observation(hr)
            person_types = hr.reporting_person_types or (
                [hr.reporting_person_type] if hr.reporting_person_type else []
            )
            add_owner(
                hr.holder_name,
                hr.holder_cik,
                cu,
                h.cik,
                hr.class_title,
                observation,
                reporting_person_types=person_types,
                jurisdiction=hr.holder_jurisdiction,
            )
            if (
                h.predecessor_cik
                and not (h.predecessor_kind or "").startswith("spin-off")
                and hr.issuer_cik == h.predecessor_cik
            ):
                predecessor_uri = f"urn:sec:cik:{h.predecessor_cik}"
                if predecessor_uri not in ents:
                    ent(
                        predecessor_uri,
                        entity_kind="org",
                        entity_type="issuer",
                        name=hr.issuer_name or h.predecessor_cik,
                        description=f"{hr.issuer_name or h.predecessor_cik}, predecessor issuer for {h.name}.",
                        properties={"uri": predecessor_uri, "cik": h.predecessor_cik, "type": "issuer"},
                        tags=["issuer", "predecessor"],
                    )
                add_owner(
                    hr.holder_name,
                    hr.holder_cik,
                    predecessor_uri,
                    h.predecessor_cik,
                    hr.class_title,
                    _own_observation(hr),
                    reporting_person_types=person_types,
                    jurisdiction=hr.holder_jurisdiction,
                )

    # Classless proxy/header rows are observations on a (holder, company) pair. Resolve their
    # class from the same accession first, then the pair's unique own class, before making edges.
    pair_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for (source, target, _), group in owner_rows.items():
        pair = pair_rows.setdefault(
            (source, target),
            {"name": group["name"], "cik": group.get("cik"), "own": [], "proxy": [], "raw": []},
        )
        pair["name"] = min(pair["name"], group["name"], key=lambda n: (len(n), norm_name(n)))
        pair["cik"] = pair["cik"] or group.get("cik")
        pair["own"].extend(group["own"])
        pair["proxy"].extend(group["proxy"])
        pair["raw"].extend(group["raw"])

    def class_rank(observation: dict[str, Any]) -> tuple[int, str, str, str]:
        return (
            2 if observation.get("source") == "xml" else 1,
            observation.get("as_of") or "",
            observation.get("filed") or "",
            observation.get("accession") or "",
        )

    def class_identity(observation: dict[str, Any]) -> str | None:
        return _class_key(observation.get("class"), observation.get("cusip"))

    def dedupe_own(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_accession: dict[str, list[dict[str, Any]]] = {}
        for index, observation in enumerate(rows):
            key = observation.get("accession") or f"__missing_accession_{index}"
            by_accession.setdefault(key, []).append(observation)
        deduped = []
        for copies in by_accession.values():
            xml_copies = [o for o in copies if o.get("source") == "xml"]
            chosen = dict(max(xml_copies or copies, key=class_rank))
            chosen["source"] = "xml" if xml_copies else chosen.get("source", "headers-only")
            if xml_copies:
                chosen["parsed_from"] = "xml"
            chosen["reporting_person_types"] = sorted(
                {t for o in copies for t in o.get("reporting_person_types", [])}
            )
            if not class_identity(chosen):
                class_copies = [o for o in copies if class_identity(o)]
                if class_copies:
                    class_copy = max(class_copies, key=class_rank)
                    chosen["class"] = class_copy.get("class")
                    chosen["cusip"] = class_copy.get("cusip")
            deduped.append(chosen)
        return _ordered_observations(deduped)

    def resolve_class(
        observation: dict[str, Any],
        accession_classes: dict[str, set[str]],
        pair_classes: set[str],
        class_rows: dict[str, dict[str, Any]],
        newest_own_class: str | None,
    ) -> dict[str, Any]:
        resolved = dict(observation)
        existing_key = class_identity(resolved)
        title_key = _class_key(resolved.get("class"))
        cusip_key = re.sub(r"\s+", "", resolved.get("cusip") or "").casefold()
        if existing_key and title_key and not cusip_key:
            compatible = {
                key
                for key in pair_classes
                if key in class_rows
                and _class_titles_compatible(resolved.get("class"), class_rows[key].get("class"))
            }
            if len(compatible) == 1 or (len(compatible) > 1 and newest_own_class in compatible):
                inferred = next(iter(compatible)) if len(compatible) == 1 else newest_own_class
                exemplar = class_rows[inferred]
                resolved["class"] = exemplar.get("class")
                resolved["cusip"] = exemplar.get("cusip")
                if len(compatible) > 1:
                    resolved["class_assumed"] = True
                return resolved
            return resolved
        if existing_key and not resolved.get("class") and cusip_key and existing_key in class_rows:
            resolved["class"] = class_rows[existing_key].get("class")
            return resolved
        if existing_key:
            return resolved
        accession = resolved.get("accession") or ""
        classes = accession_classes.get(accession, set())
        inferred = None
        assumed = False
        if len(classes) == 1:
            inferred = next(iter(classes))
        elif len(pair_classes) == 1:
            inferred = next(iter(pair_classes))
        elif len(pair_classes) > 1 and newest_own_class:
            inferred = newest_own_class
            assumed = True
        if inferred:
            exemplar = class_rows[inferred]
            resolved["class"] = exemplar.get("class")
            resolved["cusip"] = exemplar.get("cusip")
        if assumed:
            resolved["class_assumed"] = True
        return resolved

    resolved_owner_rows: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for (source, target), pair in sorted(pair_rows.items()):
        own = dedupe_own(pair["own"])
        proxy = [dict(o) for o in pair["proxy"]]
        all_observations = own + proxy
        class_rows: dict[str, dict[str, Any]] = {}
        accession_classes: dict[str, set[str]] = {}
        for observation in pair["own"] + pair["proxy"]:
            key = class_identity(observation)
            if not key:
                continue
            if key not in class_rows or class_rank(observation) > class_rank(class_rows[key]):
                class_rows[key] = observation
            if observation.get("accession"):
                accession_classes.setdefault(observation["accession"], set()).add(key)
        pair_classes = {
            class_identity(o) for o in pair["own"] if o.get("source") == "xml" and class_identity(o)
        }
        if not pair_classes:
            pair_classes = {class_identity(o) for o in all_observations if class_identity(o)}
        newest_own_class = next(
            (class_identity(o) for o in own if class_identity(o)),
            None,
        )
        own = [resolve_class(o, accession_classes, pair_classes, class_rows, newest_own_class) for o in own]
        proxy = [
            resolve_class(o, accession_classes, pair_classes, class_rows, newest_own_class) for o in proxy
        ]
        for observation in own + proxy:
            class_key = class_identity(observation)
            key = (source, target, class_key)
            group = resolved_owner_rows.setdefault(
                key,
                {
                    "name": pair["name"],
                    "cik": pair.get("cik"),
                    "class_title": observation.get("class"),
                    "own": [],
                    "proxy": [],
                    "raw": pair["raw"],
                },
            )
            group["class_title"] = group["class_title"] or observation.get("class")
            group["own" if observation.get("source") != "proxy-table" else "proxy"].append(observation)
    owner_rows = resolved_owner_rows

    # Group membership needs the resolved class and one assertion per own accession.
    group_accessions = {}
    for (source, target, class_key), rows in owner_rows.items():
        for observation in rows["own"]:
            sparse_headers = (
                observation.get("source") == "headers-only"
                and observation.get("pct") is None
                and observation.get("shares") is None
                and observation.get("as_of") is None
                and not observation.get("class")
                and not observation.get("cusip")
            )
            if sparse_headers:
                continue
            accession_key = (target, class_key, observation["accession"])
            accession = group_accessions.setdefault(
                accession_key,
                {
                    "as_of": observation.get("as_of"),
                    "filed": observation.get("filed"),
                    "members": set(),
                    "filers": set(),
                },
            )
            accession["members"].add(source)
            if observation.get("holder_cik"):
                accession["filers"].add(observation["holder_cik"])
            accession["group"] = accession.get("group", False) or bool(observation.get("member_of_group"))

    # Holder entities are keyed by CIK, with one named identity per reporting-person name where a
    # filing CIK represents multiple people or legal entities.
    holder_display_names: dict[str, set[str]] = {}
    for holder_uri, fact in holder_facts.items():
        holder_display_names.setdefault(norm_name(fact["name"]), set()).add(holder_uri)
    for uri, fact in sorted(holder_facts.items()):
        is_person = fact["person"]
        aliases = sorted(
            name
            for name in ({name for name in fact["names"] if name != fact["name"]} | fact["proxy_aliases"])
            if name
            and not any(other_uri != uri for other_uri in holder_display_names.get(norm_name(name), set()))
        )
        properties = {
            "uri": uri,
            "cik": fact["cik"],
            "jurisdiction": fact["jurisdiction"],
            "reporting_person_types": sorted(fact["types"]),
            "aliases": aliases,
            "type": "holder",
        }
        if fact["cik"] in scoped_ciks:
            properties["shared_cik"] = fact["cik"]
        description = f"{fact['name']}, reports a beneficial ownership position."
        if fact["jurisdiction"]:
            description += f"; organized in {fact['jurisdiction']}"
        description += "."
        holder_entity = ents.get(uri)
        if holder_entity is not None and holder_entity.entity_type == "issuer":
            holder_entity.entity_kind = "person" if is_person else "org"
            holder_entity.entity_type = "individual-holder" if is_person else "holder"
            holder_entity.name = fact["name"]
            holder_entity.description = description
            holder_entity.properties.update(properties)
            holder_entity.tags = sorted(set(holder_entity.tags or []) | {"holder"})
        else:
            holder_entity = ent(
                uri,
                entity_kind="person" if is_person else "org",
                entity_type="individual-holder" if is_person else "holder",
                name=fact["name"],
                description=description,
                properties=properties,
                tags=["holder"],
            )
        holder_entity.properties["aliases"] = aliases

    # An observed CIK, not an accession prefix (the SEC filing agent), anchors a holder's lineage.
    group_exits: dict[tuple[str, str | None], dict[str, dict[str, Any]]] = {}
    lineage_accessions: dict[tuple[str, str | None, str], dict[str, dict[str, Any]]] = {}
    for (target, cls, accession), event in group_accessions.items():
        for filer_cik in event["filers"]:
            lineage_accessions.setdefault((target, cls, filer_cik), {})[accession] = {
                **event,
                "accession": accession,
            }

    own_observations_by_holder: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (source, target, _class_value), rows in owner_rows.items():
        own_observations_by_holder.setdefault((target, source), []).extend(rows["own"])

    def filing_rank(filing: dict[str, Any]) -> tuple[str, str]:
        return filing.get("as_of") or "", filing.get("filed") or ""

    for (target, cls, _filer_cik), filings_by_accession in lineage_accessions.items():
        filings = list(filings_by_accession.values())
        ordered = sorted(
            filings,
            key=lambda filing: (
                filing.get("as_of") or "",
                filing.get("filed") or "",
                filing["accession"],
            ),
        )
        if len(ordered) < 2:
            continue
        latest = ordered[-1]
        latest_rank = filing_rank(latest)
        for earlier in ordered[:-1]:
            earlier_rank = filing_rank(earlier)
            if earlier["accession"] == latest["accession"] or latest_rank <= earlier_rank:
                continue
            if not earlier["members"] & latest["members"]:
                continue
            for member in earlier["members"] - latest["members"]:
                has_newer_live_own = any(
                    filing_rank(observation) > earlier_rank
                    and observation.get("pct") is not None
                    and observation["pct"] >= 5.0
                    and not observation.get("five_pct_or_less")
                    for observation in own_observations_by_holder.get((target, member), [])
                )
                if has_newer_live_own:
                    continue
                candidate = {
                    "as_of": latest.get("as_of") or latest.get("filed"),
                    "filed": latest.get("filed") or latest.get("as_of"),
                    "accession": latest["accession"],
                    "prior_accession": earlier["accession"],
                }
                exits_for_class = group_exits.setdefault((target, cls), {})
                existing = exits_for_class.get(member)
                candidate_rank = (
                    latest_rank,
                    earlier_rank,
                    candidate["accession"],
                    candidate["prior_accession"],
                )
                existing_rank = (
                    (
                        (existing.get("as_of") or "", existing.get("filed") or ""),
                        (existing.get("prior_as_of") or "", existing.get("prior_filed") or ""),
                        existing.get("accession") or "",
                        existing.get("prior_accession") or "",
                    )
                    if existing
                    else (("", ""), ("", ""), "", "")
                )
                candidate["prior_as_of"], candidate["prior_filed"] = earlier_rank
                if candidate_rank > existing_rank:
                    exits_for_class[member] = candidate

    # A same-day filing by a related but distinct CIK is evidence of filer realignment when the
    # older holder reports a threshold exit in the same class at the same issuer.
    def related_filer(a: str, b: str) -> bool:
        stop = {
            "the",
            "group",
            "inc",
            "corp",
            "corporation",
            "company",
            "management",
            "partners",
            "limited",
            "llc",
            "lp",
        }
        wa = {w for w in norm_name(a).split(".") if len(w) >= 5 and w not in stop}
        wb = {w for w in norm_name(b).split(".") if len(w) >= 5 and w not in stop}
        return bool(wa & wb)

    same_day_filings: dict[tuple[str, str | None, str], list[tuple[str, str | None, str, float, str]]] = {}
    for (related_source, related_target, related_class), related_rows in owner_rows.items():
        for observation in related_rows["own"]:
            shares, as_of = observation.get("shares"), observation.get("as_of")
            if shares is not None and shares > 0 and as_of:
                same_day_filings.setdefault((related_target, related_class, as_of), []).append(
                    (
                        related_source,
                        related_rows.get("cik"),
                        related_rows["name"],
                        shares,
                        observation.get("form") or "",
                    )
                )

    for (source, target, cls), rows in sorted(
        owner_rows.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
    ):
        if not rows["own"] and not rows["proxy"]:
            continue
        own_by_id: dict[tuple[str, str, str | None, float | None, float | int | None], dict[str, Any]] = {}
        proxy_by_id: dict[tuple[str, str | None], dict[str, Any]] = {}
        for obs in rows["own"]:
            obs_id = (obs["accession"], obs["source"], obs.get("as_of"), obs.get("pct"), obs.get("shares"))
            own_by_id.setdefault(obs_id, obs)
        for obs in rows["proxy"]:
            obs_id = (obs["accession"], obs.get("class"))
            existing = proxy_by_id.get(obs_id)
            if existing is None:
                proxy_by_id[obs_id] = obs
                continue
            preferred, other = (
                (obs, existing)
                if obs.get("table") == "holders" and existing.get("table") != "holders"
                else (existing, obs)
            )
            if (
                preferred.get("pct") is not None
                and other.get("pct") is not None
                and abs(preferred["pct"] - other["pct"]) > 0.05
            ):
                preferred = dict(preferred)
                preferred["proxy_table_conflict"] = {
                    "percentages": [preferred["pct"], other["pct"]],
                    "raw_cells": [preferred.get("raw", ""), other.get("raw", "")],
                }
            proxy_by_id[obs_id] = preferred
        own = list(own_by_id.values())
        proxy = list(proxy_by_id.values())
        latest_own = _ordered_observations(own)
        same_day_related = False
        same_day_transfer = False
        if (
            latest_own
            and latest_own[0].get("pct") is not None
            and (latest_own[0]["pct"] < 5 or latest_own[0].get("five_pct_or_less"))
        ):
            previous_rows = [
                observation
                for observation in latest_own[1:]
                if observation.get("accession") != latest_own[0].get("accession")
            ]
            previous = previous_rows[0] if previous_rows else None
            old_shares = (previous or {}).get("shares")
            old_form = latest_own[0].get("form") or ""
            for other_source, other_cik, other_name, other_shares, other_form in same_day_filings.get(
                (target, cls, latest_own[0].get("as_of")), []
            ):
                matching_related_filer = (
                    other_source != source
                    and rows.get("cik")
                    and other_cik
                    and rows["cik"] != other_cik
                    and old_shares is not None
                    and old_shares > 0
                    and abs(other_shares / old_shares - 1) <= 0.01
                    and related_filer(rows["name"], other_name)
                )
                if matching_related_filer and "13G" in old_form and "13G" in other_form:
                    same_day_related = True
                    break
                if matching_related_filer and "13D" in old_form and "13D" in other_form:
                    same_day_transfer = True
                    break
        group_exit = group_exits.get((target, cls), {}).get(source)
        metadata, weight = _ownership_metadata(
            own,
            proxy,
            same_day_related_filer=same_day_related,
            same_day_transfer=same_day_transfer,
            group_exit=group_exit,
        )
        if any(o.get("class_assumed") for o in own + proxy):
            metadata["class_assumed"] = True
        if metadata.get("parsed_from") == "proxy-table":
            metadata["intent"] = "as reported by the company"
        metadata["line"] = min(rows["raw"]) if rows["raw"] else None
        target_cik = target.rsplit(":", 1)[-1]
        successor = next(
            (f"urn:sec:cik:{p.header.cik}" for p in pulled if p.header.predecessor_cik == target_cik), None
        )
        if successor:
            metadata["successor_of"] = successor
        edges.append(
            Edge(
                source=source,
                target=target,
                relation="part_of",
                weight=weight,
                metadata={
                    "predicate": "beneficial_owner",
                    "source": metadata.get("form"),
                    "control": False,
                    **metadata,
                },
            )
        )

    # Non-ownership edges merge by endpoints and relation. Beneficial-owner edges also include class.
    merged: dict[tuple[str, str, str, str | None], Edge] = {}
    for ed in edges:
        class_key = (
            _class_key(ed.metadata.get("class"), ed.metadata.get("cusip"))
            if ed.metadata.get("predicate") == "beneficial_owner"
            else None
        )
        k = (ed.source, ed.target, ed.relation, class_key)
        if k in merged:
            m = merged[k].metadata
            for a in ed.metadata.get("asserted", []):
                if a not in m.setdefault("asserted", []):
                    m["asserted"].append(a)
            la = ed.metadata.get("last_asserted")
            if la and (m.get("last_asserted") or "") < la:
                for key in (
                    "last_asserted",
                    "pct",
                    "shares",
                    "as_of",
                    "form",
                    "parsed_from",
                    "intent",
                    "rule",
                    "class_note",
                    "class_conflict",
                    "resolved_by",
                    "exit_kind",
                    "exit_note",
                    "valid_to",
                    "exit_filed",
                    "exit_accession",
                    "exit_prior_accession",
                ):
                    if key in ed.metadata:
                        m[key] = ed.metadata[key]
        else:
            merged[k] = ed
    n_company = sum(1 for e in ents.values() if e.entity_type == "company")
    if n_company != len(pulled):
        raise SystemExit(f"{n_company} company entities for {len(pulled)} pulled files")
    source_dates = [r.filed for p in pulled for r in (p.ten_k, p.proxy) if r]
    source_dates.extend(r.filed for p in pulled for r in p.holdings + p.stakes)
    gen = {
        "name": out.stem,
        "source_max_filed": max(source_dates) if source_dates else None,
        "universe": {"path": str(universe_path), "sha256": sha256_file(universe_path)},
        "pulled": {
            "dir": str(pull_dir),
            "files": len(files),
            "sha256": hashlib.sha256(b"".join(sha256_file(f).encode() for f in files)).hexdigest(),
        },
        "entities": len(ents),
        "edges": len(merged),
        "by_entity_type": dict(sorted(_count(e.entity_type for e in ents.values()).items())),
        "by_predicate": dict(sorted(_count(e.metadata.get("predicate") for e in merged.values()).items())),
        "events_8k": sum(
            len(e.properties.get("events") or []) for e in ents.values() if e.entity_type == "company"
        ),
        "by_8k_item": dict(
            sorted(
                _count(
                    i
                    for e in ents.values()
                    if e.entity_type == "company"
                    for ev in e.properties.get("events") or []
                    for i in ev["items"]
                ).items()
            )
        ),
        "proxy_rejected": {
            "total": sum(len(p.proxy_rejected) for p in pulled),
            "by_reason": dict(sorted(_count(r["reason"] for p in pulled for r in p.proxy_rejected).items())),
        },
        "unresolved_bare_brands": [
            {"company_cik": company, "name": name} for company, name in sorted(unresolved_bare_brands)
        ],
        "brand_resolved": [
            {
                "company_cik": company_cik,
                "name": name,
                "holder_uri": holder_uri,
                "resolved_by": "brand",
            }
            for company_cik, name, holder_uri in sorted(brand_resolved_rows)
        ],
        "cik_splits": cik_split_details,
        "cik_alias_merges": cik_alias_merges,
        "class_note_sources": class_note_sources,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        Archive(generation=gen, entities=list(ents.values()), edges=list(merged.values())).model_dump_json()
    )
    out.with_suffix(".generation.json").write_text(json.dumps(gen, indent=1))
    print(json.dumps(gen, indent=1))
    return out


def _count(it) -> dict:
    out: dict = {}
    for k in it:
        out[k] = out.get(k, 0) + 1
    return out


# -- config + load -------------------------------------------------------------------------


def config(store: Path, out: Path, *, model: str = "all-minilm-l6-v2") -> Path:
    if out.exists():
        raise SystemExit(f"refusing to replace {out}")
    if store.exists():
        raise SystemExit(f"{store} exists; a new generation loads into a new store")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# filings-graph store: companies, subsidiaries, holders and filings from EDGAR, in the base kg pack.\n"
        "# Written by `python -m filings_graph.build config`; run with KHIVE_PACKS=kg,memory.\n"
        '[actor]\nid = "filings-graph"\ndisplay_name = "filings-graph"\n\n'
        f'[[engines]]\nname = "default"\nmodel = "{model}"\ndefault = true\nfusion_weight = 1.0\n\n'
        f'[[backends]]\nname = "main"\nkind = "sqlite"\npath = "{store.resolve()}"\n\n'
        '[display]\ntimezone = "America/New_York"\n'
    )
    print(f"wrote {out} -> {store}")
    return out


def _slices(rows: list, count: int) -> list[list]:
    """Consecutive slices of at most `count` rows whose JSON stays under ARGV_CAP bytes."""
    out: list[list] = []
    cur: list = []
    size = 0
    for row in rows:
        n = len(json.dumps(row, allow_nan=False).encode()) + 2
        if cur and (len(cur) == count or size + n > ARGV_CAP):
            out.append(cur)
            cur, size = [], 0
        cur.append(row)
        size += n
    if cur:
        out.append(cur)
    return out


def load(khive: Khive, archive_path: Path, *, ids_out: Path | None = None) -> dict[str, str]:
    """Entities in slices of ITEM_SLICE through bulk `create`, then edges in slices of LINK_SLICE
    through bulk `link` (both also cut at ARGV_CAP bytes); the uri→id map is written beside the archive
    so later stages resolve identities without a search."""
    t0 = time.time()
    arc = Archive.model_validate_json(archive_path.read_text())
    ids_out = ids_out or archive_path.with_suffix(".ids.json")
    ids: dict[str, str] = json.loads(ids_out.read_text()) if ids_out.exists() else {}
    todo = [e for e in arc.entities if e.uri not in ids]
    print(f"{len(arc.entities)} entities ({len(todo)} to create), {len(arc.edges)} edges", flush=True)
    # entity_type is a governed closed vocabulary per kind (concept: algorithm | theorem | ...), so the
    # filings-graph type stays in properties.type, which list/search filter on when the column is null.
    all_items = [
        {
            "kind": "entity",
            "name": e.name,
            "entity_kind": e.entity_kind,
            "description": e.description,
            "properties": {**e.properties, "type": e.entity_type},
            "tags": e.tags,
        }
        for e in todo
    ]
    off = 0
    for items in _slices(all_items, ITEM_SLICE):
        chunk = todo[off : off + len(items)]
        r = khive.call("create", items=items, verbose=True, atomic=True)
        created = r.get("entities") or []
        if len(created) != len(chunk):
            raise RuntimeError(
                f"create returned {len(created)} entities for {len(chunk)} items: {json.dumps(r)[:500]}"
            )
        for e, row in zip(chunk, created, strict=True):
            uri = (row.get("properties") or {}).get("uri")
            if uri != e.uri:
                raise RuntimeError(f"create answered out of order: wanted {e.uri}, got {uri}")
            ids[e.uri] = row["id"]
        ids_out.write_text(json.dumps(ids))
        off += len(chunk)
        print(f"[{time.time() - t0:6.0f}s] entities {off}/{len(todo)}", flush=True)
    links = [
        {
            "source_id": ids[ed.source],
            "target_id": ids[ed.target],
            "relation": ed.relation,
            "weight": ed.weight,
            "metadata": ed.metadata,
        }
        for ed in arc.edges
    ]
    off = 0
    for batch in _slices(links, LINK_SLICE):
        r = khive.call("link", links=batch, atomic=False)
        off += len(batch)
        print(
            f"[{time.time() - t0:6.0f}s] edges {off}/{len(links)}: "
            f"{json.dumps({k: v for k, v in r.items() if k != 'edges'})[:300]}",
            flush=True,
        )
    store = re.search(r'path = "([^"]+)"', Path(khive.command[3]).read_text()).group(1)
    r = khive.reindex(store)
    print(
        f"[{time.time() - t0:6.0f}s] reindex: entities {r.get('entities_processed')} models {r.get('models_used')} "
        f"errors_skipped {r.get('errors_skipped')}",
        flush=True,
    )
    return ids


# -- cli -----------------------------------------------------------------------------------


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="filings_graph.build")
    sp = ap.add_subparsers(dest="cmd", required=True)
    uni_default = ROOT / "data/universe.csv"
    p = sp.add_parser("pull")
    p.add_argument("--universe", type=Path, default=uni_default)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--out-dir", type=Path, default=PULL_DIR, help="directory for per-company pull JSON")
    p.add_argument(
        "--predecessors",
        type=Path,
        default=None,
        help="JSON file naming each company's predecessor CIK, see build.predecessors",
    )
    p = sp.add_parser("archive")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--universe", type=Path, default=uni_default)
    p.add_argument("--pull-dir", type=Path, default=PULL_DIR)
    p = sp.add_parser("config")
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default="all-minilm-l6-v2")
    p = sp.add_parser("load")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p = sp.add_parser("all")
    p.add_argument("--name", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--universe", type=Path, default=uni_default)
    a = ap.parse_args(argv)
    if a.cmd == "pull":
        pull(
            a.universe, out_dir=a.out_dir, limit=a.limit, refresh=a.refresh, predecessors_path=a.predecessors
        )
    elif a.cmd == "archive":
        archive(a.pull_dir, a.out, universe_path=a.universe)
    elif a.cmd == "config":
        config(a.store, a.out, model=a.model)
    elif a.cmd == "load":
        load(Khive(a.config), a.archive)
    elif a.cmd == "all":
        pull(a.universe, limit=a.limit)
        arc = archive(PULL_DIR, ROOT / f"data/external/filings-graph/{a.name}.json", universe_path=a.universe)
        cfg = config(ROOT / f".khive/{a.name}.db", ROOT / f".khive/{a.name}.toml")
        load(Khive(cfg), arc)


if __name__ == "__main__":
    main(sys.argv[1:])
