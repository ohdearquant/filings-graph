import Link from "next/link";
import { notFound } from "next/navigation";
import CompanyView from "@/components/CompanyView";
import SearchBox from "@/components/SearchBox";
import { companyGraph, companyRecord, DATE_RE } from "@/lib/archive";

export const dynamic = "force-dynamic";

export default async function CompanyPage({ params, searchParams }: { params: Promise<{ cik: string }>; searchParams: Promise<{ as_of?: string; subs?: string }> }) {
  const { cik } = await params;
  const sp = await searchParams;
  const asOf = sp.as_of && DATE_RE.test(sp.as_of) ? sp.as_of : null;
  const subs = sp.subs === "1";
  const record = companyRecord(cik, asOf);
  const graph = companyGraph(cik, asOf, subs);
  if (!record || !graph) notFound();
  return (
    <main className="page">
      <nav className="bar">
        <Link href="/">filings-graph</Link>
        <span className="muted">/</span>
        {record.industry ? <Link href={`/i/${record.industry.properties.sic as string}`}>{record.industry.name}</Link> : null}
        <span className="muted">/</span>
        <b>{record.ent.name}</b>
        <SearchBox />
      </nav>
      <CompanyView record={record} graph={graph} cik={cik} asOf={asOf} subs={subs} />
    </main>
  );
}
