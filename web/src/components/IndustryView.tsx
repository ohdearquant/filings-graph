"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import Graph, { COLORS } from "@/components/Graph";
import type { Ent, GNode, Graph as GraphData } from "@/lib/archive";
import { TYPE_LABEL } from "@/lib/labels";

export default function IndustryView({ ent, companies, graph }: { ent: Ent; companies: Ent[]; graph: GraphData }) {
  const router = useRouter();
  const [sel, setSel] = useState<GNode | null>(null);
  const open = (n: GNode) => { if ((n.type === "company" || n.listed) && n.cik) router.push(`/c/${n.cik}`); };
  return (
    <div className="split">
      <div className="pane graph-pane">
        <Graph data={graph} focusName={ent.name} selected={sel?.id} onSelect={setSel} onOpen={open} />
        <div className="legend">
          <span><i style={{ background: COLORS.company }} />Company (bigger = more subsidiaries)</span>
          <span><i style={{ background: COLORS.holder }} />Shareholder in two or more of them (bigger = more of them)</span>
          <span><b className="edge-key arrow" />holds more than 5% of</span>
          <span><b className="edge-key dash" />report together (one 13D/13G)</span>
          <span className="muted">hover: light up · click: details · right-click: open</span>
        </div>
      </div>
      <div className="pane panel">
        <h1>{ent.name}</h1>
        <p className="desc">{ent.description}</p>
        {sel ? (
          <section className="selected">
            <h2><i style={{ background: COLORS[sel.type] }} className="dot" />{sel.name}</h2>
            <p>
              {TYPE_LABEL[sel.type]}.{" "}
              {sel.type === "company" ? `${sel.count} subsidiaries.` : `Holds more than 5% of ${sel.count} companies in this industry.`}
              {sel.listed ? " Itself one of the tracked companies." : ""}
            </p>
            {(sel.type === "company" || sel.listed) && sel.cik ? <button onClick={() => open(sel)}>open</button> : null}
          </section>
        ) : null}
        <section>
          <h2>Companies <span className="muted">{companies.length}</span></h2>
          <ul className="plain">
            {companies.map((c) => (
              <li key={c.uri}>
                <Link href={`/c/${c.properties.cik as string}`}>{c.name}</Link>
                <span className="muted small"> {((c.properties.tickers as string[]) ?? []).join(", ")} · {c.properties.hq as string}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
