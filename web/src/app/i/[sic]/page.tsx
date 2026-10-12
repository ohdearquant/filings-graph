import Link from "next/link";
import { notFound } from "next/navigation";
import IndustryView from "@/components/IndustryView";
import SearchBox from "@/components/SearchBox";
import { industryGraph, industryRecord } from "@/lib/archive";

export const dynamic = "force-dynamic";

export default async function IndustryPage({ params }: { params: Promise<{ sic: string }> }) {
  const { sic } = await params;
  const r = industryRecord(sic);
  const graph = industryGraph(sic);
  if (!r || !graph) notFound();
  return (
    <main className="page">
      <nav className="bar">
        <Link href="/">filings-graph</Link>
        <span className="muted">/</span>
        <b>{r.ent.name}</b>
        <SearchBox />
      </nav>
      <IndustryView ent={r.ent} companies={r.companies} graph={graph} />
    </main>
  );
}
