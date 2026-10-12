"use client";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

type Hit = { uri: string; name: string; type: string; cik: string; tickers: string[]; sic?: string };

export default function SearchBox({ autoFocus = false }: { autoFocus?: boolean }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!q.trim()) { setHits([]); return; }
    const t = setTimeout(async () => {
      const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      setHits(await r.json());
      setOpen(true);
    }, 120);
    return () => clearTimeout(t);
  }, [q]);
  const go = (h: Hit) => {
    setOpen(false);
    if (h.type === "company") router.push(`/c/${h.cik}`);
    else if (h.type === "industry" && h.sic) router.push(`/i/${h.sic}`);
    else router.push(`/c/${h.cik}`);
  };
  return (
    <div className="search">
      <input
        value={q}
        autoFocus={autoFocus}
        placeholder="company, ticker, holder…"
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => hits.length && setOpen(true)}
        onKeyDown={(e) => { if (e.key === "Enter" && hits[0]) go(hits[0]); if (e.key === "Escape") setOpen(false); }}
      />
      {open && hits.length ? (
        <ul className="hits">
          {hits.map((h) => (
            <li key={h.uri} onMouseDown={() => go(h)}>
              <b>{h.name}</b> <span className="muted small">{h.tickers.join(", ")}{h.tickers.length ? " · " : ""}{h.type === "company" ? "company" : h.type === "industry" ? "industry" : h.type === "issuer" ? "company a tracked company holds a stake in" : "shareholder"}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
