// The company's own record: who it is, what happened since the 10-K (the 8-K rows), who holds more
// than 5% of it, its subsidiaries by place of incorporation, the stakes it holds in others, and the
// filings all of this was read from.
import Link from "next/link";
import type { Ent, Holding } from "@/lib/archive";
import { formLabel, stanceText } from "@/lib/labels";

type Ev = { at: string; items: string[]; titles: string[]; accession: string; url: string };
type Filing = { form: string; filed: string; period?: string | null; accession: string; url: string };

export function CompanyHeader({ ent, peers }: { ent: Ent; peers: number }) {
  const p = ent.properties as Record<string, string | string[] | null | undefined>;
  const row = (k: string, v?: string | string[] | null) => (v && (Array.isArray(v) ? v.length : true) ? (
    <div className="kv"><span>{k}</span><b>{Array.isArray(v) ? v.join(", ") : v}</b></div>
  ) : null);
  return (
    <section>
      <h1>{ent.name}</h1>
      <div className="kvs">
        {row("ticker", p.tickers)}
        {row("SEC id (CIK)", p.cik)}
        {row("incorporated in", p.jurisdiction)}
        {row("fiscal year ends", p.fiscal_year_end ? `${(p.fiscal_year_end as string).slice(0, 2)}/${(p.fiscal_year_end as string).slice(2)}` : null)}
        {p.sic ? <div className="kv"><span>industry</span><b><Link href={`/i/${p.sic}`}>SIC {p.sic as string}</Link> · {peers} companies</b></div> : null}
        {row("formerly", p.former_names)}
      </div>
    </section>
  );
}

export function Events({ events }: { events: Ev[] }) {
  if (!events?.length) return <section><h2>Since the 10-K</h2><p className="muted">Nothing filed on Form 8-K since the 10-K.</p></section>;
  return (
    <section>
      <h2>Since the 10-K <span className="muted">{events.length} events, newest first</span></h2>
      <ol className="timeline">
        {[...events].reverse().map((e) => (
          <li key={e.accession}>
            <time>{e.at}</time>
            <div>
              {e.items.map((it, i) => (
                <div key={it} className="item"><code>{it}</code> {e.titles[i]}</div>
              ))}
              <a href={e.url} target="_blank" rel="noreferrer" className="muted small">the 8-K on EDGAR</a>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function Filings({ filings }: { filings: Filing[] }) {
  return (
    <section>
      <h2>Read from</h2>
      <ul className="plain">
        {filings?.map((f) => (
          <li key={f.accession}>
            <b>{f.form === "DEF 14A" ? "Proxy statement" : f.form === "10-K" ? "Annual report (10-K)" : f.form}</b>, filed {f.filed}
            {f.period ? ` for the year to ${f.period}` : ""} · <a href={f.url} target="_blank" rel="noreferrer">open on EDGAR</a>
          </li>
        ))}
      </ul>
    </section>
  );
}

type Obs = { accession: string; form: string; pct: number | null; shares?: number | null; as_of: string | null; filed: string | null; source?: string; table?: string; suspect?: boolean; suspect_reason?: string | null };
const edgar = (cik: string, accession: string) => `https://www.sec.gov/Archives/edgar/data/${Number(cik)}/${accession.replace(/-/g, "")}/`;

export function Holders({ holders, cik, asOf, title = "Shareholders over 5%" }: { holders: Holding[]; cik: string; asOf?: string | null; title?: string }) {
  if (!holders.length) return <section><h2>{title}</h2><p className="muted">{asOf ? `Nothing about this company's holders was public by ${asOf}.` : "None reported in the last twelve months."}</p></section>;
  const max = Math.max(...holders.map((h) => (h.meta.pct as number) ?? 0), 5);
  const live = holders.filter((h) => !h.meta.valid_to).length;
  return (
    <section>
      <h2>{title} <span className="muted">{live}{holders.length > live ? ` live, ${holders.length - live} exited` : ""}{asOf ? ` · as of ${asOf}` : ""}</span></h2>
      <table className="bars">
        <tbody>
          {holders.map((h) => {
            const pct = h.meta.pct as number | null;
            const exited = !!h.meta.valid_to;
            const obs = ((h.meta.observations as Obs[] | undefined) ?? []).filter((o) => !asOf || (o.filed && o.filed <= asOf));
            const acc = h.meta.accession as string | null;
            return (
              <tr key={h.ent.uri} className={exited ? "exited" : ""}>
                <td className="name">
                  {h.ent.properties.cik && h.ent.entity_type === "company" ? <Link href={`/c/${h.ent.properties.cik as string}${asOf ? `?as_of=${asOf}` : ""}`}>{h.ent.name}</Link> : h.ent.name}
                </td>
                <td className="bar"><div style={{ width: `${((pct ?? 0) / max) * 100}%` }} className={`fill ${h.meta.intent === "active" ? "active" : ""}`} /></td>
                <td className="num">{pct != null ? `${pct}%` : "?"}</td>
                <td className="muted small">
                  {formLabel(h.meta.form)}{stanceText(h.meta) ? `, ${stanceText(h.meta)}` : ""}{h.meta.as_of ? `, as of ${h.meta.as_of as string}` : ""}
                  {h.meta.filed ? `, filed ${h.meta.filed as string}` : ""}
                  {exited ? `; fell under 5% on ${h.meta.valid_to as string}` : ""}
                  {acc ? <> · <a href={edgar(cik, acc)} target="_blank" rel="noreferrer">{acc}</a></> : null}
                  {obs.length > 1 ? (
                    <details className="obs">
                      <summary>{obs.length} filings behind this row</summary>
                      <ul className="plain dense">
                        {[...obs].sort((a, b) => (b.filed ?? "").localeCompare(a.filed ?? "")).map((o) => (
                          <li key={o.accession + (o.table ?? "")} className={o.suspect ? "suspect" : ""}>
                            {o.filed ?? "?"} · {formLabel(o.form)} · <b>{o.pct != null ? `${o.pct}%` : "no percent"}</b>{o.as_of ? ` as of ${o.as_of}` : ""}
                            {o.source === "proxy-table" ? " · proxy table" : ""} · <a href={edgar(cik, o.accession)} target="_blank" rel="noreferrer">{o.accession}</a>
                            {o.suspect ? <span className="muted"> · set aside: {o.suspect_reason}</span> : null}
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

export function Stakes({ stakes }: { stakes: Holding[] }) {
  if (!stakes.length) return null;
  return (
    <section>
      <h2>Companies it holds more than 5% of <span className="muted">{stakes.length}</span></h2>
      <ul className="plain">
        {stakes.map((s) => (
          <li key={s.ent.uri}>
            {s.ent.properties.cik && s.ent.tags?.includes("company") ? <Link href={`/c/${s.ent.properties.cik as string}`}>{s.ent.name}</Link> : s.ent.name}
            {" "}<b>{s.meta.pct != null ? `${s.meta.pct}%` : "share not stated"}</b>
            <span className="muted small"> {formLabel(s.meta.form)}{s.meta.as_of ? `, as of ${s.meta.as_of as string}` : ""}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function Subsidiaries({ subsidiaries }: { subsidiaries: Holding[] }) {
  const byJur = new Map<string, Holding[]>();
  for (const s of subsidiaries) {
    const j = (s.meta.jurisdiction as string | null) ?? "place of incorporation not stated";
    (byJur.get(j) ?? byJur.set(j, []).get(j)!).push(s);
  }
  const groups = [...byJur.entries()].sort((a, b) => b[1].length - a[1].length);
  return (
    <section>
      <h2>Subsidiaries <span className="muted">{subsidiaries.length}, incorporated in {groups.length} places</span></h2>
      {groups.map(([j, rows]) => (
        <details key={j} open={rows.length >= 20 ? false : groups.length <= 4}>
          <summary><b>{j}</b> <span className="muted">{rows.length}</span></summary>
          <ul className="plain dense">
            {rows.map((s) => (
              <li key={s.ent.uri}>{s.ent.name}{s.meta.ownership_pct != null ? <span className="muted"> · {s.meta.ownership_pct as number}% owned</span> : null}</li>
            ))}
          </ul>
        </details>
      ))}
    </section>
  );
}
