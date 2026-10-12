import { NextResponse } from "next/server";
import { industryGraph, industryRecord } from "@/lib/archive";

export const dynamic = "force-dynamic";

export async function GET(_req: Request, { params }: { params: Promise<{ sic: string }> }) {
  const { sic } = await params;
  const r = industryRecord(sic);
  if (!r) return NextResponse.json({ error: "no such industry" }, { status: 404 });
  return NextResponse.json({ ...r, graph: industryGraph(sic) });
}
