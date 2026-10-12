"""Questions against a filings-graph store, each one a named walk from `filings_graph.graph.FinGraph`.

    uv run python scripts/ask.py --config .khive/filings-graph-v0.toml company MMM
    uv run python scripts/ask.py --config .khive/filings-graph-v0.toml who-controls "Solventum"
    uv run python scripts/ask.py --config .khive/filings-graph-v0.toml holder "Vanguard"
    uv run python scripts/ask.py --config .khive/filings-graph-v0.toml holders MMM --as-of 2026-03-31

Every number printed comes back from the store on this call; nothing is read from the archive.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path

from filings_graph.graph import FinGraph
from filings_graph.khive import Khive


def find_company(g: FinGraph, key: str) -> dict | None:
    """By ticker or CIK, an exact read over the company entities (tag `company`)."""
    key = key.strip().upper()
    for c in g.by_tag("company"):
        p = c.get("properties") or {}
        if key.isdigit() and p.get("cik") == f"{int(key):010d}":
            return c
        if key in [t.upper() for t in p.get("tickers") or []]:
            return c
    return None


def cmd_company(g: FinGraph, key: str, as_of: str | None, top: int) -> None:
    c = find_company(g, key)
    if not c:
        raise SystemExit(f"no company entity for {key!r}")
    p = c["properties"]
    print(
        f"{c['name']}  cik {p.get('cik')}  tickers {p.get('tickers')}  jurisdiction {p.get('jurisdiction')}  "
        f"FYE {p.get('fiscal_year_end')}  SIC {p.get('sic')}"
    )
    # 1. what it controls: part_of edges with control: true, followed down
    subs = g.controlled_by(c["id"])
    by_j = Counter((r["edge"].get("metadata") or {}).get("jurisdiction") or "?" for r in subs)
    print(f"\ncontrols {len(subs)} entities (Exhibit 21, layer<=5); by jurisdiction:")
    for j, n in by_j.most_common(top):
        print(f"  {n:4d}  {j}")
    # 2. who holds it: part_of edges with control: false, with the time weight computed at read
    hold = g.holders(c["id"], as_of=as_of)
    names = g.names([r["id"] for r in hold])
    print(f"\nholders >5% ({len(hold)} rows{', as of ' + as_of if as_of else ''}):")
    for r in hold[:top]:
        w = f"{r['time_weight']:.2f}" if r["time_weight"] is not None else "   ?"
        pct = f"{r['pct']:6.2f}%" if r["pct"] is not None else "     ?%"
        print(
            f"  {pct}  w={w}  {r['as_of'] or '?':10}  {r['form'] or '':14} {r['intent'] or '':10}  {names.get(r['id'])}"
        )
    # 3. what it holds elsewhere: its own 13D/13G filings
    st = g.stakes(c["id"])
    if st:
        names = g.names([r["id"] for r in st])
        print(f"\nstakes it reports in other issuers ({len(st)}):")
        for r in st[:top]:
            pct = f"{r['pct']:6.2f}%" if r["pct"] is not None else "     ?%"
            print(
                f"  {pct}  {r['as_of'] or '?':10}  {r['form'] or '':14} {r['intent'] or '':8}  {names.get(r['id'])}"
                + (f"  :: {r['purpose'][:90]}" if r.get("purpose") else "")
            )
    # 4. filings that introduce facts about it
    filings = p.get("filings") or []
    forms = Counter(f.get("form") for f in filings)
    print(
        f"\nfilings on record: {len(filings)} (by form {dict(forms)}); 8-K events since the 10-K: {len(p.get('events') or [])}"
    )


def cmd_who_controls(g: FinGraph, name: str, top: int) -> None:
    hits = g.search(name, entity_type="subsidiary", limit=max(top, 20))
    # search ranks by similarity; a question about a NAME wants the rows that carry it
    exact = [h for h in hits if name.lower() in str((h.get("record") or h).get("name", "")).lower()]
    if not exact:
        near = ", ".join(str((h.get("record") or h).get("name")) for h in hits[:5])
        raise SystemExit(f"no subsidiary name contains {name!r}; nearest by similarity: {near}")
    for h in exact[:top]:
        rec = h.get("record") or h
        rid = rec.get("id") or h.get("id")
        chain = g.controllers_of(rid)
        names = g.names([r["id"] for r in chain])
        path = " -> ".join(f"{names[r['id']]} (L{r['layer']})" for r in chain)
        print(
            f"{rec.get('name', '?')}  [{(rec.get('properties') or {}).get('jurisdiction')}]  controlled by: {path or '-'}"
        )


def cmd_holder(g: FinGraph, name: str, top: int) -> None:
    hits = g.search(name, entity_type="holder", limit=5)
    if not hits:
        raise SystemExit(f"no holder matches {name!r}")
    rec = hits[0].get("record") or hits[0]
    rid = rec.get("id") or hits[0].get("id")
    rows = []
    for ed in g.edges(source_id=rid, relations=["part_of"]):
        md = ed.get("metadata") or {}
        if md.get("control") is False:
            rows.append((md.get("pct") or 0, md.get("as_of") or "", md.get("form") or "", ed["target_id"]))
    rows.sort(reverse=True)
    names = g.names([r[3] for r in rows])
    print(f"{rec.get('name')}: {len(rows)} positions >5% on record")
    for pct, as_of, form, tid in rows[:top]:
        print(f"  {pct:6.2f}%  {as_of:10}  {form:14}  {names.get(tid)}")


def cmd_activist_exits(g: FinGraph, days: int, window: int) -> None:
    """Companies where an active holder (13D or 13D/A) filed in the last `days` days and an 8-K Item
    5.02 (officer or director departure or election) landed within `window` days of that filing, either
    side. Two hops and one attachment read: holder -(part_of, control false, form 13D)-> company; the
    company's own 8-K event rows (v0.1 model: filings are attachments on the company). Every row prints
    the accession of both filings so it can be opened at EDGAR."""
    today = dt.datetime.now().astimezone().date()
    since = today - dt.timedelta(days=days)
    companies = g.by_tag("company")
    # ten companies per batch: a part_of edge carries its observations, and the binary caps a batch
    # response at 4 MiB (a batch of 100 companies was refused at op 49)
    edge_sets = []
    for i in range(0, len(companies), 10):
        edge_sets.extend(
            g.khive.batch(
                [
                    ("list", {"kind": "edge", "target_id": c["id"], "relations": ["part_of"], "limit": 1000})
                    for c in companies[i : i + 10]
                ]
            )
        )
    rows = []
    holder_ids: set[str] = set()
    for c, es in zip(companies, edge_sets, strict=True):
        items = es.get("items", es if isinstance(es, list) else [])
        events = [
            e for e in ((c.get("properties") or {}).get("events") or []) if "5.02" in (e.get("items") or [])
        ]
        for ed in items:
            md = ed.get("metadata") or {}
            if md.get("control") is not False:
                continue
            # observations carry the filing date (v0.3 edges); older stores carry one state per edge
            # with the event date only, used in its place and labelled so in the output
            obs = md.get("observations") or [
                {
                    "form": md.get("form"),
                    "filed": md.get("as_of"),
                    "pct": md.get("pct"),
                    "accession": (md.get("asserted") or [None])[-1],
                    "date_is_event": True,
                }
            ]
            for o in obs:
                if "13D" not in (o.get("form") or "") or not o.get("filed") or o["filed"] < since.isoformat():
                    continue
                f = dt.date.fromisoformat(o["filed"])
                for ev in events:
                    at = dt.date.fromisoformat(ev["at"])
                    gap = (at - f).days
                    if abs(gap) <= window:
                        holder_ids.add(ed["source_id"])
                        rows.append(
                            (
                                c["name"],
                                ed["source_id"],
                                o.get("pct"),
                                o["form"] + ("*" if o.get("date_is_event") else ""),
                                o["filed"],
                                o.get("accession"),
                                ev["at"],
                                gap,
                                ev.get("accession"),
                            )
                        )
    names = g.names(sorted(holder_ids))
    seen = set()
    rows.sort(key=lambda r: (r[4], r[0]))
    print(
        f"{len(companies)} companies read; 13D filings since {since}; 8-K Item 5.02 within {window} days: {len(rows)} pairs"
    )
    print(
        f"{'company':34} {'active holder':34} {'pct':>6} {'form':12} {'13D filed':10} {'5.02 on':10} {'gap':>5}  accessions   (* = event date, filing date not on this store's edges)"
    )
    for name, hid, pct, form, filed, acc, at, gap, eacc in rows:
        key = (name, hid, filed, at)
        if key in seen:
            continue
        seen.add(key)
        pc = f"{pct:5.1f}%" if pct is not None else "    ?%"
        print(
            f"{name[:34]:34} {(names.get(hid) or hid)[:34]:34} {pc:>6} {form:12} {filed:10} {at:10} {gap:+5d}  {acc} / {eacc}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--top", type=int, default=12)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("company")
    p.add_argument("key")
    p.add_argument("--as-of")
    p = sp.add_parser("holders")
    p.add_argument("key")
    p.add_argument("--as-of")
    p = sp.add_parser("who-controls")
    p.add_argument("name")
    p = sp.add_parser("holder")
    p.add_argument("name")
    p = sp.add_parser("json")
    p.add_argument("key")
    p = sp.add_parser("activist-exits")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--window", type=int, default=90)
    a = ap.parse_args()
    g = FinGraph(Khive(a.config))
    if a.cmd == "company":
        cmd_company(g, a.key, a.as_of, a.top)
    elif a.cmd == "holders":
        c = find_company(g, a.key)
        if not c:
            raise SystemExit(f"no company entity for {a.key!r}")
        rows = g.holders(c["id"], as_of=a.as_of)
        names = g.names([r["id"] for r in rows])
        for r in rows[: a.top]:
            print(
                f"  {r['pct'] or 0:6.2f}%  {r['as_of'] or '?':10}  {r['form'] or '':14}  {names.get(r['id'])}"
            )
    elif a.cmd == "activist-exits":
        cmd_activist_exits(g, a.days, a.window)
    elif a.cmd == "who-controls":
        cmd_who_controls(g, a.name, a.top)
    elif a.cmd == "holder":
        cmd_holder(g, a.name, a.top)
    elif a.cmd == "json":
        c = find_company(g, a.key)
        if not c:
            raise SystemExit(f"no company entity for {a.key!r}")
        print(
            json.dumps(
                {
                    "company": c,
                    "controls": g.controlled_by(c["id"]),
                    "holders": g.holders(c["id"]),
                    "stakes": g.stakes(c["id"]),
                },
                indent=1,
                default=str,
            )
        )


if __name__ == "__main__":
    main()
