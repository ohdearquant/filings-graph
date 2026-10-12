import { NextResponse } from "next/server";
import { companyGraph, companyRecord, DATE_RE } from "@/lib/archive";

export const dynamic = "force-dynamic";

// GET /api/company/<cik>?as_of=YYYY-MM-DD&subs=1
// The company as a reader on `as_of` could have known it from the filings public by then (no date:
// the current state, exits flagged). `subs=1` adds the subsidiaries to the graph payload.
export async function GET(req: Request, { params }: { params: Promise<{ cik: string }> }) {
  const { cik } = await params;
  const u = new URL(req.url);
  const asOf = u.searchParams.get("as_of");
  if (asOf && !DATE_RE.test(asOf)) return NextResponse.json({ error: "as_of must be YYYY-MM-DD" }, { status: 400 });
  const subs = u.searchParams.get("subs") === "1";
  const record = companyRecord(cik, asOf);
  if (!record) return NextResponse.json({ error: "no such company" }, { status: 404 });
  return NextResponse.json({ as_of: asOf ?? null, record, graph: companyGraph(cik, asOf, subs) });
}
