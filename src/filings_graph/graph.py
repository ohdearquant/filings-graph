"""Reading the filings-graph store: one method per native verb, plus the two walks the design names
(who controls X, who holds X). No ranking and no merging of judgments happens here."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from filings_graph.khive import Khive


def _observation_order(o: dict) -> tuple[str, str, str]:
    return (o.get("as_of") or "", o.get("filed") or "", o.get("accession") or "")


def state_as_of(md: dict, as_of: str | None) -> dict | None:
    """The state of one `beneficial_owner` edge as a reader on `as_of` could have known it, or None
    when nothing about it was public yet (or the exit was). With no date, the stored current state.

    Visibility is the filing date: an observation whose `filed` is after `as_of` does not exist for
    that reader, whatever its own `as_of` says. Among the visible ones the state is the newest
    non-suspect own filing, else the newest non-suspect proxy row, in the builder's order
    (`_ownership_metadata`); an edge whose visible observations carry no percent (headers-only, or
    all suspect) is still a holder, with `pct` None. The exit closes the edge only from `exit_filed`."""
    if not as_of:
        if md.get("valid_to"):
            return None  # the holder reported falling under the threshold
        return {
            "pct": md.get("pct"),
            "as_of": md.get("last_asserted") or md.get("as_of"),
            "form": md.get("form") or "",
        }
    visible = [o for o in md.get("observations") or [] if o.get("filed") and o["filed"] <= as_of]
    if not visible:
        return None
    exit_filed = md.get("exit_filed")
    if exit_filed and exit_filed <= as_of:
        return None
    clean = [o for o in visible if not o.get("suspect")]
    own = [o for o in clean if o.get("source") != "proxy-table"]
    proxy = [o for o in clean if o.get("source") == "proxy-table"]
    pool = own or proxy or visible
    state = max(pool, key=_observation_order)
    return {
        "pct": state.get("pct") if pool is not visible else None,
        "as_of": state.get("as_of"),
        "form": state.get("form") or "",
    }


class FinGraph:
    def __init__(self, khive: Khive) -> None:
        self.khive = khive

    # -- native reads -----------------------------------------------------------------------

    def search(self, query: str, *, entity_type: str | None = None, limit: int = 10) -> list[dict]:
        return self.khive.call(
            "search",
            kind="entity",
            query=query,
            limit=limit,
            properties={"type": entity_type} if entity_type else None,
        )

    def get(self, id: str) -> dict:
        return self.khive.call("get", id=id)

    def neighbors(
        self, node_id: str, *, relations: list[str] | None = None, direction: str = "both", limit: int = 1000
    ) -> dict:
        return self.khive.call(
            "neighbors",
            node_id=node_id,
            relations=relations,
            direction=direction,
            limit=limit,
            projection="record",
        )

    def edges(
        self,
        *,
        source_id: str | None = None,
        target_id: str | None = None,
        relations: list[str] | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Edge records with their metadata (neighbors does not project metadata)."""
        out: list[dict] = []
        offset = 0
        while True:
            r = self.khive.call(
                "list",
                kind="edge",
                source_id=source_id,
                target_id=target_id,
                relations=relations,
                limit=limit,
                offset=offset,
            )
            items = r.get("items", r if isinstance(r, list) else [])
            out.extend(items)
            if not r.get("has_more") if isinstance(r, dict) else True:
                break
            offset += len(items)
        return out

    def context(
        self,
        query: str | None = None,
        *,
        entity_ids: list[str] | None = None,
        hops: int = 1,
        relations: list[str] | None = None,
    ) -> dict:
        return self.khive.call("context", query=query, entity_ids=entity_ids, hops=hops, relations=relations)

    # -- the two walks ----------------------------------------------------------------------

    def controlled_by(self, company_id: str, *, depth: int = 5) -> list[dict]:
        """What `company_id` controls: incoming `part_of` edges with `control: true`, followed
        down through subsidiaries up to `depth` layers. Each row carries the edge metadata."""
        out: list[dict] = []
        frontier = [(company_id, 0)]
        seen = {company_id}
        while frontier:
            node, layer = frontier.pop()
            if layer >= depth:
                continue
            for ed in self.edges(target_id=node, relations=["part_of"]):
                md = ed.get("metadata") or {}
                if not md.get("control"):
                    continue
                src = ed["source_id"]
                out.append({"id": src, "layer": layer + 1, "via": node, "edge": ed})
                if src not in seen:
                    seen.add(src)
                    frontier.append((src, layer + 1))
        return out

    def controllers_of(self, node_id: str, *, depth: int = 5) -> list[dict]:
        """Who controls `node_id`: outgoing `part_of` edges with `control: true`, followed up."""
        out: list[dict] = []
        frontier = [(node_id, 0)]
        seen = {node_id}
        while frontier:
            node, layer = frontier.pop()
            if layer >= depth:
                continue
            for ed in self.edges(source_id=node, relations=["part_of"]):
                md = ed.get("metadata") or {}
                if not md.get("control"):
                    continue
                tgt = ed["target_id"]
                out.append({"id": tgt, "layer": layer + 1, "via": node, "edge": ed})
                if tgt not in seen:
                    seen.add(tgt)
                    frontier.append((tgt, layer + 1))
        return out

    def holders(
        self, company_id: str, *, as_of: str | None = None, half_life_days: float = 365.0
    ) -> list[dict]:
        """Who holds more than 5% of `company_id`: incoming `part_of` edges with `control: false`,
        each with the percent, the as-of date and a time weight `exp(-ln2 * age / half_life)` computed
        here, never stored (the design's decay rule).

        Point in time (`as_of` given) keys on the FILING date, the day a filing became public, never on
        the observation's own `as_of`, the day the stake was true (DESIGN.md "Point in time"; the
        acceptance stamp is not on the edge yet, `filed` is the index date and the nearest field). The
        state shown is the newest observation filed on or before the date, own filings before the
        company's proxy, as the builder picks the current state; an exit counts only once its filing
        is public (`exit_filed`), so a holder whose exit was filed after the date is still shown at the
        last observation that was."""
        today = dt.date.fromisoformat(as_of) if as_of else dt.datetime.now().astimezone().date()
        rows = []
        for ed in self.edges(target_id=company_id, relations=["part_of"]):
            md = ed.get("metadata") or {}
            if md.get("control") is not False:
                continue
            state = state_as_of(md, as_of)
            if state is None:
                continue
            la = state["as_of"]
            age = (today - dt.date.fromisoformat(la)).days if la else None
            w = math.exp(-math.log(2) * age / half_life_days) if age is not None else None
            form = state["form"]
            # intent follows the latest assertion's form (a merged edge keeps the newest form)
            intent = (
                "active" if "13D" in form else "passive" if "13G" in form else "as reported by the company"
            )
            rows.append(
                {
                    "id": ed["source_id"],
                    "pct": state["pct"],
                    "as_of": la,
                    "form": form,
                    "intent": intent,
                    "age_days": age,
                    "time_weight": w,
                    "edge_id": ed.get("id"),
                }
            )
        rows.sort(key=lambda r: (-(r["pct"] or 0), r["as_of"] or ""))
        return rows

    def by_tag(self, tag: str) -> list[dict]:
        """Every entity carrying `tag` (company, subsidiary, holder, filing), paged through list."""
        out: list[dict] = []
        offset = 0
        while True:
            r = self.khive.call("list", kind="entity", tags=[tag], limit=1000, offset=offset)
            items = r.get("items", [])
            out.extend(items)
            if not r.get("has_more"):
                return out
            offset += len(items)

    def company(self, cik: str) -> dict | None:
        """The company entity by CIK, an exact read (search with a properties filter answers from a
        bounded candidate window and misses exact names)."""
        cik = f"{int(cik):010d}"
        return next(
            (c for c in self.by_tag("company") if (c.get("properties") or {}).get("cik") == cik), None
        )

    def stakes(self, company_id: str) -> list[dict]:
        """What `company_id` itself reports holding more than 5% in other issuers (its own 13D/13G
        filings): outgoing `part_of` edges with `control: false`."""
        rows = []
        for ed in self.edges(source_id=company_id, relations=["part_of"]):
            md = ed.get("metadata") or {}
            if md.get("control") is not False:
                continue
            rows.append(
                {
                    "id": ed["target_id"],
                    "pct": md.get("pct"),
                    "as_of": md.get("as_of"),
                    "form": md.get("form"),
                    "intent": md.get("intent"),
                    "purpose": md.get("purpose"),
                    "edge_id": ed.get("id"),
                }
            )
        rows.sort(key=lambda r: (-(r["pct"] or 0), r["as_of"] or ""))
        return rows

    def names(self, ids: list[str]) -> dict[str, Any]:
        recs = self.khive.batch([("get", {"id": i}) for i in ids]) if ids else []
        return {i: r.get("name") for i, r in zip(ids, recs, strict=True)}
