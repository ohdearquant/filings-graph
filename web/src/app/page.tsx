import Link from "next/link";
import SearchBox from "@/components/SearchBox";
import { archive, overview } from "@/lib/archive";

export const dynamic = "force-dynamic";

export default function Home() {
  const a = archive();
  const inds = overview();
  const gen = a.generation as { name?: string; built?: string; entities?: number; edges?: number; events_8k?: number };
  const max = inds[0]?.count ?? 1;
  return (
    <main className="home">
      <header>
        <h1>filings-graph</h1>
        <p className="muted">
          {a.companies.length} S&amp;P 500 companies · {gen.entities?.toLocaleString()} entities · {gen.edges?.toLocaleString()} edges ·{" "}
          {gen.events_8k?.toLocaleString()} 8-K events · generation {gen.name} built {gen.built?.slice(0, 16).replace("T", " ")}
        </p>
        <SearchBox autoFocus />
      </header>
      <section>
        <h2>Industries <span className="muted">{inds.length}, by company count</span></h2>
        <div className="tiles">
          {inds.map(({ ent, count }) => (
            <Link key={ent.uri} href={`/i/${ent.properties.sic as string}`} className="tile" style={{ ["--w" as string]: `${(count / max) * 100}%` }}>
              <span className="n">{count}</span>
              <span className="t">{ent.name.replace(/^SIC \d+ /, "")}</span>
              <span className="muted small">SIC {ent.properties.sic as string}</span>
            </Link>
          ))}
        </div>
      </section>
    </main>
  );
}
