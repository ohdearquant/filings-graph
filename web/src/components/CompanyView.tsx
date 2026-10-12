"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import Link from "next/link";
import Graph, { COLORS } from "@/components/Graph";
import { CompanyHeader, Events, Filings, Holders, Stakes, Subsidiaries } from "@/components/Record";
import type { Ent, GNode, Graph as GraphData, Holding } from "@/lib/archive";
import { nodeText, TYPE_LABEL } from "@/lib/labels";

export type CompanyPayload = {
  ent: Ent; subsidiaries: Holding[]; holders: Holding[]; stakes: Holding[]; industry: Ent | null; peers: number; as_of: string | null;
};

const LEGEND: GNode["type"][] = ["company", "industry", "holder", "individual-holder", "issuer", "jurisdiction", "subsidiary"];

export default function CompanyView({ record, graph, cik, asOf, subs }: { record: CompanyPayload; graph: GraphData; cik: string; asOf: string | null; subs: boolean }) {
  const router = useRouter();
  const [sel, setSel] = useState<GNode | null>(null);
  const [date, setDate] = useState(asOf ?? "");
  const go = (d: string | null, s: boolean) => {
    const q = new URLSearchParams();
    if (d) q.set("as_of", d);
    if (s) q.set("subs", "1");
    const qs = q.toString();
    router.push(`/c/${cik}${qs ? `?${qs}` : ""}`);
  };
  const open = (n: GNode) => {
    if (n.type === "industry" && n.sic) router.push(`/i/${n.sic}`);
    else if ((n.type === "company" || n.type === "issuer" || n.listed) && n.cik) router.push(`/c/${n.cik}`);
  };
  const p = record.ent.properties as { events?: never[]; filings?: never[] };
  const canOpen = (n: GNode) => (n.type === "industry" && !!n.sic) || ((n.type === "company" || n.type === "issuer" || !!n.listed) && !!n.cik);
  const live = record.holders.filter((h) => !h.meta.valid_to).length;
  return (
    <div className="split">
      <div className="pane graph-pane">
        <Graph data={graph} focus={record.ent.uri} focusName={record.ent.name} selected={sel?.id} onSelect={setSel} onOpen={open} />
        <div className="toolbar">
          <label>
            <span>as of</span>
            <input type="date" value={date} max={new Date().toISOString().slice(0, 10)} min="2024-01-01"
              onChange={(e) => setDate(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") go(date || null, subs); }} />
          </label>
          <button onClick={() => go(date || null, subs)} disabled={(date || null) === asOf}>show</button>
          {asOf ? <button className="ghost" onClick={() => { setDate(""); go(null, subs); }}>today</button> : null}
          <label className="check"><input type="checkbox" checked={subs} onChange={(e) => go(asOf, e.target.checked)} /> subsidiaries</label>
          <span className="muted small">
            {asOf ? `what a reader could see on ${asOf}: filings made public by then, keyed on the filing date` : `current state: ${live} shareholders over 5%, exits faded`}
          </span>
        </div>
        <div className="legend">
          {LEGEND.map((t) => (
            <span key={t}><i style={{ background: COLORS[t] }} />{TYPE_LABEL[t]}</span>
          ))}
          <span><b className="edge-key arrow" />holds more than 5% of</span>
          <span><b className="edge-key dash" />report together (one 13D/13G)</span>
          <span className="muted">hover: light up · click: details · right-click: open</span>
        </div>
      </div>
      <div className="pane panel">
        {sel && sel.id !== record.ent.uri ? (
          <section className="selected">
            <h2><i style={{ background: COLORS[sel.type] }} className="dot" />{sel.name}</h2>
            <p>{TYPE_LABEL[sel.type]}. {nodeText(sel, record.ent.name)}.</p>
            {sel.meta?.accession ? (
              <p className="small muted">read from {String(sel.meta.form)} {String(sel.meta.accession)}{sel.meta.filed ? `, filed ${String(sel.meta.filed)}` : ""} ·{" "}
                <a href={`https://www.sec.gov/Archives/edgar/data/${Number(cik)}/${String(sel.meta.accession).replace(/-/g, "")}/`} target="_blank" rel="noreferrer">on EDGAR</a></p>
            ) : null}
            {sel.also?.length ? (
              <div className="also">
                <h3>Also holds more than 5% of{asOf ? ` (as of ${asOf})` : ""}</h3>
                <ul className="plain dense">
                  {sel.also.map((x) => (
                    <li key={x.cik ?? x.name} className={x.exited ? "exited" : ""}>
                      {x.cik ? <Link href={`/c/${x.cik}${asOf ? `?as_of=${asOf}` : ""}`}>{x.name}</Link> : x.name}
                      {" "}<b>{x.pct != null ? `${x.pct}%` : "share not stated"}</b>{x.exited ? <span className="muted small"> · fell under 5% on {x.exited}</span> : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {canOpen(sel) ? <button onClick={() => open(sel)}>open</button> : null}
          </section>
        ) : null}
        <CompanyHeader ent={record.ent} peers={record.peers} />
        <p className="desc">{record.ent.description}</p>
        <Holders holders={record.holders} cik={cik} asOf={asOf} />
        <Stakes stakes={record.stakes} />
        <Events events={p.events ?? []} />
        <Subsidiaries subsidiaries={record.subsidiaries} />
        <Filings filings={p.filings ?? []} />
      </div>
    </div>
  );
}
