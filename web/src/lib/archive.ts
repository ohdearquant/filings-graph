// The filings-graph archive (one generation: entities + edges, as filings_graph.build archive() writes it) loaded
// once into memory. Every page reads the same index; the store (khive) serves search and the
// MCP surface, the page serves the graph. Path: FILINGS_GRAPH_ARCHIVE or ../data/external/filings-graph/filings-graph-v0.1.json.
import fs from "node:fs";
import path from "node:path";

export type Props = Record<string, unknown>;
export type Ent = {
  uri: string;
  entity_kind: string;
  entity_type: string; // company | subsidiary | holder | individual-holder | issuer | industry
  name: string;
  description: string;
  properties: Props;
  tags: string[];
};
export type Edge = {
  source: string;
  target: string;
  relation: string;
  weight: number;
  metadata: Props;
};
type Index = {
  generation: Props;
  ents: Map<string, Ent>;
  out: Map<string, Edge[]>;
  inn: Map<string, Edge[]>;
  companies: Ent[];
  industries: Ent[];
  familyOf: Map<string, string>; // holder uri -> family id
  families: Map<string, string[]>; // family id -> member uris
};

function build(): Index {
  const p = process.env.FILINGS_GRAPH_ARCHIVE ?? path.resolve(process.cwd(), "../data/external/filings-graph/filings-graph-v0.1.json");
  const raw = JSON.parse(fs.readFileSync(p, "utf8")) as { generation: Props; entities: Ent[]; edges: Edge[] };
  const ents = new Map<string, Ent>();
  for (const e of raw.entities) ents.set(e.uri, e);
  const out = new Map<string, Edge[]>();
  const inn = new Map<string, Edge[]>();
  for (const ed of raw.edges) {
    (out.get(ed.source) ?? out.set(ed.source, []).get(ed.source)!).push(ed);
    (inn.get(ed.target) ?? inn.set(ed.target, []).get(ed.target)!).push(ed);
  }
  const companies = raw.entities.filter((e) => e.entity_type === "company").sort((a, b) => a.name.localeCompare(b.name));
  const industries = raw.entities.filter((e) => e.entity_type === "industry");
  // Holder families: holders named together on one Schedule 13D/13G (a joint filing lists every
  // reporting person of the group: the parent, its advisers, the funds, the people). A proxy
  // statement lists unrelated holders side by side, so its accession never joins anyone.
  const proxy = new Set<string>();
  for (const c of companies) for (const f of (c.properties.filings as { form: string; accession: string }[] | undefined) ?? []) if (f.form === "DEF 14A") proxy.add(f.accession);
  const byAcc = new Map<string, Set<string>>();
  for (const ed of raw.edges) {
    if (ed.metadata.predicate !== "beneficial_owner") continue;
    for (const acc of (ed.metadata.asserted as string[] | undefined) ?? []) {
      if (proxy.has(acc)) continue;
      (byAcc.get(acc) ?? byAcc.set(acc, new Set()).get(acc)!).add(ed.source);
    }
  }
  const parent = new Map<string, string>();
  const find = (x: string): string => { let r = parent.get(x) ?? x; while (r !== (parent.get(r) ?? r)) r = parent.get(r)!; parent.set(x, r); return r; };
  for (const members of byAcc.values()) {
    const [first, ...rest] = [...members];
    for (const m of rest) { const a = find(first), b = find(m); if (a !== b) parent.set(b, a); }
  }
  const families = new Map<string, string[]>();
  for (const x of parent.keys()) { const r = find(x); (families.get(r) ?? families.set(r, [r]).get(r)!).includes(x) || families.get(r)!.push(x); }
  const familyOf = new Map<string, string>();
  for (const [id, members] of families) for (const m of members) familyOf.set(m, id);
  return { generation: raw.generation, ents, out, inn, companies, industries, familyOf, families };
}

// bump when the index shape changes: the dev server keeps globalThis across hot reloads
const INDEX_VERSION = 3;
const g = globalThis as unknown as { __filingsGraph?: Index & { v?: number } };
export function archive(): Index {
  if (g.__filingsGraph?.v !== INDEX_VERSION) g.__filingsGraph = Object.assign(build(), { v: INDEX_VERSION });
  return g.__filingsGraph;
}

export const cikUri = (cik: string) => `urn:sec:cik:${cik.padStart(10, "0")}`;
export const cikOf = (e: Ent) => (e.properties.cik as string | undefined) ?? e.uri.replace("urn:sec:cik:", "");

// -- what one company is made of ------------------------------------------------------------

export type Holding = { ent: Ent; meta: Props };

// -- point in time -----------------------------------------------------------------------------
// The state of one holding as a reader on `asOf` could have known it (the same rule as
// filings_graph.graph.state_as_of): visibility is the FILING date, never the observation's own as-of; among
// the visible observations the newest non-suspect own filing wins, else the newest non-suspect proxy
// row; a headers-only edge is still a holder, with pct null; the exit closes the edge only once its
// own filing is public. With no date: the stored current state, exits included (valid_to set) so the
// page can fade them rather than drop them.
export type Obs = { accession: string; form: string; pct: number | null; shares?: number | null; as_of: string | null; filed: string | null;
  source?: string; table?: string; suspect?: boolean; suspect_reason?: string | null; holder_cik?: string | null };
export type State = { pct: number | null; as_of: string | null; form: string; filed: string | null; accession: string | null; exited: string | null };
const cmpObs = (a: Obs, b: Obs) => {
  for (const k of ["as_of", "filed", "accession"] as const) {
    const x = a[k] ?? "", y = b[k] ?? "";
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
};
export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export function stateAsOf(meta: Props, asOf?: string | null): State | null {
  if (!asOf) {
    return { pct: (meta.pct as number | null) ?? null, as_of: ((meta.last_asserted ?? meta.as_of) as string | null) ?? null, form: (meta.form as string | null) ?? "",
      filed: (meta.filed as string | null) ?? null, accession: (meta.accession as string | null) ?? null, exited: (meta.valid_to as string | null) ?? null };
  }
  const obs = (meta.observations as Obs[] | undefined) ?? [];
  const visible = obs.filter((o) => o.filed && o.filed <= asOf);
  if (!visible.length) return null;
  const exitFiled = meta.exit_filed as string | null | undefined;
  if (exitFiled && exitFiled <= asOf) return null;
  const clean = visible.filter((o) => !o.suspect);
  const own = clean.filter((o) => o.source !== "proxy-table");
  const proxy = clean.filter((o) => o.source === "proxy-table");
  const pool = own.length ? own : proxy.length ? proxy : visible;
  const state = pool.reduce((a, b) => (cmpObs(b, a) > 0 ? b : a));
  return { pct: pool === visible ? null : state.pct, as_of: state.as_of, form: state.form ?? "", filed: state.filed, accession: state.accession, exited: null };
}
/** The edge metadata with the point-in-time state laid over it: the fields the page reads (pct,
 * as_of, form, filed, accession, valid_to, intent) are the state's; observations stay for the panel. */
export function effective(meta: Props, s: State): Props {
  return { ...meta, pct: s.pct, as_of: s.as_of, form: s.form, filed: s.filed, accession: s.accession, valid_to: s.exited,
    intent: s.form.includes("13D") ? "active" : s.form.includes("13G") ? "passive" : null };
}
export type Also = { name: string; cik?: string; pct: number | null; exited: string | null; form: string };
/** The other tracked companies one shareholder holds more than 5% of, as of the same date. */
export function alsoHolds(holderUri: string, asOf: string | null | undefined, exclude: string, limit = 12): Also[] {
  const a = archive();
  const rows: Also[] = [];
  for (const ed of a.out.get(holderUri) ?? []) {
    if (ed.metadata.predicate !== "beneficial_owner" || ed.target === exclude) continue;
    const t = a.ents.get(ed.target);
    if (!t || t.entity_type !== "company") continue;
    const s = stateAsOf(ed.metadata, asOf);
    if (!s) continue;
    rows.push({ name: t.name, cik: cikOf(t), pct: s.pct, exited: s.exited, form: s.form });
  }
  rows.sort((x, y) => (y.pct ?? 0) - (x.pct ?? 0) || x.name.localeCompare(y.name));
  return rows.slice(0, limit);
}

export function companyRecord(cik: string, asOf?: string | null) {
  const a = archive();
  const ent = a.ents.get(cikUri(cik));
  if (!ent) return null;
  const inn = a.inn.get(ent.uri) ?? [];
  const out = a.out.get(ent.uri) ?? [];
  const subsidiaries: Holding[] = inn
    .filter((ed) => ed.metadata.predicate === "subsidiary_of")
    .map((ed) => ({ ent: a.ents.get(ed.source)!, meta: ed.metadata }));
  const byPct = (x: Holding, y: Holding) => ((y.meta.pct as number) ?? 0) - ((x.meta.pct as number) ?? 0) || x.ent.name.localeCompare(y.ent.name);
  const holders: Holding[] = [];
  for (const ed of inn) {
    if (ed.metadata.predicate !== "beneficial_owner") continue;
    const st = stateAsOf(ed.metadata, asOf);
    if (!st) continue;
    holders.push({ ent: a.ents.get(ed.source)!, meta: effective(ed.metadata, st) });
  }
  holders.sort(byPct);
  const stakes: Holding[] = [];
  for (const ed of out) {
    if (ed.metadata.predicate !== "beneficial_owner") continue;
    const st = stateAsOf(ed.metadata, asOf);
    if (!st) continue;
    stakes.push({ ent: a.ents.get(ed.target)!, meta: effective(ed.metadata, st) });
  }
  stakes.sort(byPct);
  const indEdge = out.find((ed) => ed.metadata.predicate === "in_industry");
  const industry = indEdge ? a.ents.get(indEdge.target)! : null;
  const peers = industry ? (a.inn.get(industry.uri) ?? []).length : 0;
  return { ent, subsidiaries, holders, stakes, industry, peers, as_of: asOf ?? null };
}

export function industryRecord(sic: string) {
  const a = archive();
  const ent = a.ents.get(`urn:sec:sic:${sic}`);
  if (!ent) return null;
  const companies = (a.inn.get(ent.uri) ?? []).map((ed) => a.ents.get(ed.source)!).sort((x, y) => x.name.localeCompare(y.name));
  return { ent, companies };
}

export function overview() {
  const a = archive();
  return a.industries
    .map((e) => ({ ent: e, count: (a.inn.get(e.uri) ?? []).length }))
    .sort((x, y) => y.count - x.count || x.ent.name.localeCompare(y.ent.name));
}

export function search(q: string, limit = 20) {
  const a = archive();
  const s = q.trim().toLowerCase();
  if (!s) return [];
  const hit = (e: Ent) => {
    const tickers = ((e.properties.tickers as string[] | undefined) ?? []).map((t) => t.toLowerCase());
    if (tickers.includes(s)) return 0;
    const n = e.name.toLowerCase();
    if (n.startsWith(s)) return 1;
    if (n.includes(s)) return 2;
    return -1;
  };
  const rows: { e: Ent; rank: number }[] = [];
  for (const e of a.ents.values()) {
    if (e.entity_type === "subsidiary") continue; // 65k rows; the company page lists them
    const r = hit(e);
    if (r >= 0) rows.push({ e, rank: r + (e.entity_type === "company" ? 0 : 3) });
  }
  rows.sort((x, y) => x.rank - y.rank || x.e.name.localeCompare(y.e.name));
  return rows.slice(0, limit).map(({ e }) => ({
    uri: e.uri, name: e.name, type: e.entity_type, cik: cikOf(e), tickers: (e.properties.tickers as string[] | undefined) ?? [],
    sic: e.properties.sic as string | undefined,
  }));
}

// -- graph payloads for the renderer ---------------------------------------------------------

export type GNode = {
  id: string;
  name: string;
  type: "company" | "industry" | "holder" | "individual-holder" | "issuer" | "subsidiary" | "jurisdiction" | "peer";
  val: number; // area, the renderer takes sqrt
  cik?: string;
  sic?: string;
  meta?: Props; // the edge that put it here (pct, as_of, form, ownership_pct ...)
  count?: number; // jurisdiction hubs
  listed?: boolean; // a shareholder that is itself one of the tracked companies (it has a page)
  side?: "left" | "right" | "top" | "bottom" | "center";
  also?: Also[]; // a shareholder's other holdings over 5% in tracked companies, same date
};
export type GLink = { source: string; target: string; kind: string; meta?: Props; width?: number };
export type Graph = { nodes: GNode[]; links: GLink[] };

/** Dashed links between the shareholders on a page that report together: each member present is
 * tied to the member of its family that holds the most here (a star, not a clique). */
function familyLinks(nodes: GNode[], links: GLink[]) {
  const a = archive();
  const weight = new Map<string, number>();
  for (const l of links) if (l.kind === "beneficial_owner") weight.set(l.source, (weight.get(l.source) ?? 0) + 1);
  const present = new Map<string, string[]>();
  for (const n of nodes) {
    if (n.type !== "holder" && n.type !== "individual-holder") continue;
    const fam = a.familyOf.get(n.id);
    if (fam) (present.get(fam) ?? present.set(fam, []).get(fam)!).push(n.id);
  }
  const out: GLink[] = [];
  for (const ids of present.values()) {
    if (ids.length < 2) continue;
    const hub = [...ids].sort((x, y) => (weight.get(y) ?? 0) - (weight.get(x) ?? 0))[0];
    for (const id of ids) if (id !== hub) out.push({ source: id, target: hub, kind: "family", width: 0.8 });
  }
  return out;
}

const isListed = (e: Ent) => e.entity_type === "company";

export function companyGraph(cik: string, asOf?: string | null, subs = false): Graph | null {
  const r = companyRecord(cik, asOf);
  if (!r) return null;
  const nodes: GNode[] = [];
  const links: GLink[] = [];
  const c = r.ent;
  nodes.push({ id: c.uri, name: c.name, type: "company", val: 90, cik, side: "center" });
  if (r.industry) {
    nodes.push({ id: r.industry.uri, name: r.industry.name, type: "industry", val: 30, sic: r.industry.properties.sic as string,
      count: r.peers, side: "top" });
    links.push({ source: c.uri, target: r.industry.uri, kind: "in_industry" });
  }
  for (const h of r.holders) {
    const pct = (h.meta.pct as number | null) ?? 0;
    nodes.push({ id: h.ent.uri, name: h.ent.name, type: h.ent.entity_type === "individual-holder" ? "individual-holder" : "holder",
      val: 12 + pct * 3, cik: h.ent.properties.cik as string | undefined, meta: h.meta, side: "left", listed: isListed(h.ent),
      also: alsoHolds(h.ent.uri, asOf, c.uri) });
    links.push({ source: h.ent.uri, target: c.uri, kind: "beneficial_owner", meta: h.meta, width: 0.8 + pct / 4 });
  }
  links.push(...familyLinks(nodes, links));
  for (const s of r.stakes) {
    const pct = (s.meta.pct as number | null) ?? 0;
    nodes.push({ id: s.ent.uri, name: s.ent.name, type: "issuer", val: 8 + pct * 2.5, cik: s.ent.properties.cik as string | undefined,
      meta: s.meta, side: "bottom" });
    links.push({ source: c.uri, target: s.ent.uri, kind: "stake", meta: s.meta, width: 0.8 + pct / 4 });
  }
  // subsidiaries hang off one hub per jurisdiction, so 600 rows read as a dozen clusters
  const byJur = new Map<string, Holding[]>();
  for (const s of r.subsidiaries) {
    const j = (s.meta.jurisdiction as string | null) ?? "(jurisdiction not printed)";
    (byJur.get(j) ?? byJur.set(j, []).get(j)!).push(s);
  }
  if (!subs && r.subsidiaries.length) {
    // one quiet node for the whole subsidiary tree: the ownership picture comes first, the
    // places of incorporation and the rows open with the "subsidiaries" toggle
    const hub = "jur:all";
    nodes.push({ id: hub, name: `${r.subsidiaries.length} subsidiaries in ${byJur.size} places`, type: "jurisdiction", val: 10 + Math.sqrt(r.subsidiaries.length), count: r.subsidiaries.length, side: "right" });
    links.push({ source: hub, target: c.uri, kind: "jurisdiction", width: 0.5 + Math.log2(r.subsidiaries.length + 1) / 2 });
    return { nodes, links };
  }
  for (const [j, rows] of [...byJur.entries()].sort((x, y) => y[1].length - x[1].length)) {
    const hub = `jur:${j}`;
    nodes.push({ id: hub, name: j, type: "jurisdiction", val: 6 + Math.sqrt(rows.length) * 4, count: rows.length, side: "right" });
    links.push({ source: hub, target: c.uri, kind: "jurisdiction", width: 0.5 + Math.log2(rows.length + 1) / 2 });
    for (const s of rows) {
      const pct = s.meta.ownership_pct as number | null;
      nodes.push({ id: s.ent.uri, name: s.ent.name, type: "subsidiary", val: pct != null ? 3 + pct / 25 : 2.2, meta: s.meta, side: "right" });
      links.push({ source: s.ent.uri, target: hub, kind: "subsidiary_of", meta: s.meta, width: 0.4 });
    }
  }
  return { nodes, links };
}

export function industryGraph(sic: string): Graph | null {
  const a = archive();
  const r = industryRecord(sic);
  if (!r) return null;
  // the page is the industry, so the industry itself is not a node: its companies, the
  // shareholders two or more of them share, and any stake one of them holds in another
  const nodes: GNode[] = [];
  const links: GLink[] = [];
  const onPage = new Set(r.companies.map((c) => c.uri));
  const holderPos = new Map<string, { ent: Ent; rows: { company: Ent; meta: Props }[] }>();
  for (const c of r.companies) {
    const subs = (a.inn.get(c.uri) ?? []).filter((ed) => ed.metadata.predicate === "subsidiary_of").length;
    nodes.push({ id: c.uri, name: c.name, type: "company", val: 14 + Math.sqrt(subs) * 2, cik: cikOf(c), count: subs });
    for (const ed of a.inn.get(c.uri) ?? []) {
      if (ed.metadata.predicate !== "beneficial_owner") continue;
      const h = a.ents.get(ed.source)!;
      if (onPage.has(h.uri)) {
        // one company of the industry holding more than 5% of another: always drawn
        const pct = (ed.metadata.pct as number | null) ?? 0;
        links.push({ source: h.uri, target: c.uri, kind: "beneficial_owner", meta: ed.metadata, width: 1 + pct / 4 });
        continue;
      }
      (holderPos.get(h.uri) ?? holderPos.set(h.uri, { ent: h, rows: [] }).get(h.uri)!).rows.push({ company: c, meta: ed.metadata });
    }
  }
  const shared = [...holderPos.values()].filter(({ rows }) => rows.length >= 2 || r.companies.length === 1).sort((x, y) => y.rows.length - x.rows.length);
  for (const { ent: h, rows } of shared) {
    nodes.push({ id: h.uri, name: h.name, type: h.entity_type === "individual-holder" ? "individual-holder" : "holder",
      val: 6 + rows.length * 3, cik: h.properties.cik as string | undefined, count: rows.length, listed: isListed(h) });
    for (const row of rows) {
      const pct = (row.meta.pct as number | null) ?? 0;
      links.push({ source: h.uri, target: row.company.uri, kind: "beneficial_owner", meta: row.meta, width: 0.5 + pct / 5 });
    }
  }
  links.push(...familyLinks(nodes, links));
  return { nodes, links };
}
