"""Typed rows of the filings-graph pipeline: what a filing says (deterministic sources, v0) and what the
graph stores. Every row that becomes an edge keeps the accession that asserted it."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EntityKind = Literal["org", "person", "document", "concept", "dataset"]
Relation = Literal[
    "part_of", "depends_on", "introduced_by", "supersedes", "instance_of", "precedes", "contains"
]


class FilingRow(BaseModel):
    """One row of a company's submissions JSON."""

    form: str
    filed: str = Field(description="filingDate, YYYY-MM-DD")
    period: str | None = Field(default=None, description="reportDate when EDGAR has one")
    accession: str
    primary_document: str | None = None
    primary_doc_description: str | None = None
    items: str | None = Field(default=None, description="8-K items, comma separated, as EDGAR lists them")


class CompanyHeader(BaseModel):
    cik: str
    name: str
    tickers: list[str] = Field(default_factory=list)
    exchanges: list[str] = Field(default_factory=list)
    sic: str | None = None
    sic_description: str | None = None
    state_of_incorporation: str | None = None
    lei: str | None = None
    ein: str | None = None
    fiscal_year_end: str | None = None
    entity_type: str | None = None
    former_names: list[str] = Field(default_factory=list)
    predecessor_cik: str | None = Field(
        default=None,
        description="the CIK the filing history continues from (successor issuer) or the parent (spin-off); read from the predecessors file",
    )
    predecessor_kind: str | None = None
    predecessor_effective: str | None = None


class SubsidiaryRow(BaseModel):
    """One Exhibit 21 row as parsed: the name, the jurisdiction column, a percent when the
    exhibit states one, and the raw line, for checking the parse against."""

    name: str
    jurisdiction: str | None = None
    ownership_pct: float | None = None
    line: str
    accession: str
    order: int


class HoldingRow(BaseModel):
    """One Schedule 13D/13G (or amendment) about the company: who, how much, as of when."""

    holder_name: str
    holder_cik: str | None = None
    holder_jurisdiction: str | None = None
    pct: float | None = None
    shares: int | float | None = None
    as_of: str | None = Field(default=None, description="event date on the cover page")
    form: str
    accession: str
    filed: str
    rule: str | None = Field(default=None, description="13d-1(b)/(c)/(d) designation when stated")
    reporting_person_type: str | None = Field(
        default=None, description="First IA, BK, IN, PN, CO, ... value per the cover page"
    )
    reporting_person_types: list[str] = Field(
        default_factory=list, description="Every typeOfReportingPerson value on the cover page"
    )
    class_title: str | None = Field(default=None, description="securitiesClassTitle or equivalent")
    class_note: str | None = Field(
        default=None, description="conflicting security-class comment from the filing"
    )
    class_conflict: bool = False
    cusip: str | None = None
    five_pct_or_less: bool = False
    filer_split_signal: bool = False
    issuer_cik: str | None = Field(
        default=None,
        description="the subject company; differs from the pulled company when the pulled company is the FILER (an outbound stake)",
    )
    issuer_name: str | None = None
    member_of_group: bool | None = Field(
        default=None, description="13D: the person reports as a member of a group"
    )
    purpose: str | None = Field(default=None, description="Full 13D Item 4 text")
    parsed_from: Literal["xml", "html-regex", "headers-only"]


class ProxyHolderRow(BaseModel):
    """One row of the DEF 14A beneficial-ownership tables (Reg S-K Item 403)."""

    table: Literal["holders", "insiders"]
    name: str
    pct: float | None
    shares: int | float | None
    as_of: str | None = Field(
        default=None, description="the ownership table event date or filing date fallback"
    )
    as_of_source: Literal["table", "record_date", "filed"] = "filed"
    filed: str | None = None
    class_title: str | None = None
    accession: str
    raw: str
    suspect: bool = False
    suspect_reason: str | None = None
    proxy_table_conflict: dict[str, Any] | None = None


class Pulled(BaseModel):
    """Everything `filings_graph.build pull` keeps for one company; the raw documents stay in the EDGAR cache."""

    model_config = ConfigDict(extra="forbid")

    header: CompanyHeader
    universe: dict[str, str] = Field(description="the universe row: symbol, security")
    ten_k: FilingRow | None
    ten_k_files: list[dict[str, Any]] = Field(default_factory=list)
    exhibit21: list[SubsidiaryRow] = Field(default_factory=list)
    exhibit21_file: str | None = None
    exhibit21_files: list[str] = Field(
        default_factory=list, description="every EX-21.x document read, first = exhibit21_file"
    )
    holdings: list[HoldingRow] = Field(
        default_factory=list, description="inbound: stakes in the pulled company"
    )
    stakes: list[HoldingRow] = Field(
        default_factory=list,
        description="outbound: the pulled company's own 13D/13G filings about other issuers",
    )
    holdings_rows: int = Field(default=0, description="13D/13G rows in the window before the cap")
    holdings_capped: bool = False
    proxy: FilingRow | None = None
    proxy_holders: list[ProxyHolderRow] = Field(default_factory=list)
    proxy_rejected: list[dict[str, str]] = Field(default_factory=list)
    proxy_rejected_tables: list[dict[str, Any]] = Field(default_factory=list)
    proxy_same_name_rows: list[dict[str, Any]] = Field(default_factory=list)
    eight_ks: list[FilingRow] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Entity(BaseModel):
    uri: str = Field(
        description="canonical identity; the store id is assigned at load and mapped back to this"
    )
    entity_kind: EntityKind
    entity_type: str
    name: str
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class Edge(BaseModel):
    source: str = Field(description="source entity uri")
    target: str = Field(description="target entity uri")
    relation: Relation
    weight: float = 1.0
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="predicate, source form, asserted accessions, dates"
    )


class Archive(BaseModel):
    generation: dict[str, Any]
    entities: list[Entity]
    edges: list[Edge]
