"use client";
// Force-directed neighbourhood graph on a canvas: colour = what the thing is, size = what the edge
// says (a shareholder's percent, how many subsidiaries a place holds), placement = the forces alone.
// A real hover (the pointer rests on a node or an edge) or a click lights that thing, its edges and
// its neighbours and fades everything else; merely passing over an edge changes nothing. Holding
// edges carry an arrow from the holder to the company held.
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { forceCollide, forceManyBody, forceX, forceY } from "d3-force";
import type { GLink, GNode, Graph as GraphData } from "@/lib/archive";
import { edgeText, nodeText, TYPE_LABEL } from "@/lib/labels";

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

export const COLORS: Record<GNode["type"], string> = {
  company: "#3b82f6",
  peer: "#60a5fa",
  industry: "#a855f7",
  holder: "#f97316",
  "individual-holder": "#fbbf24",
  issuer: "#14b8a6",
  subsidiary: "#22c55e",
  jurisdiction: "#94a3b8",
};
const BG = "#0b1220";
// Obsidian-style layout: nothing is placed by hand. Every node repels (more for bigger ones), a
// weak gravity holds the picture together, and each edge pulls its two ends to a distance set by
// its kind, harder the bigger the stake. Where a thing ends up is what its edges say.
const DIST: Record<string, number> = { in_industry: 170, beneficial_owner: 210, stake: 210, jurisdiction: 60, subsidiary_of: 14, family: 40 };
const pctOf = (l: { meta?: Record<string, unknown> }) => Math.min(Number(l.meta?.pct ?? 0) || 0, 25);
// edge colour as [r,g,b]; alpha depends on whether the edge is lit
const LINK_RGB: Record<string, string> = {
  in_industry: "168,85,247",
  beneficial_owner: "249,115,22",
  stake: "20,184,166",
  jurisdiction: "148,163,184",
  subsidiary_of: "34,197,94",
  family: "226,232,240",
};
const LINK_ALPHA: Record<string, number> = { in_industry: 0.5, beneficial_owner: 0.55, stake: 0.55, jurisdiction: 0.45, subsidiary_of: 0.3, family: 0.45 };
const HOLDING = new Set(["beneficial_owner", "stake"]);

type N = GNode & { x?: number; y?: number; fx?: number; fy?: number };
type L = GLink & { source: N | string; target: N | string };
const idOf = (x: N | string) => (typeof x === "string" ? x : x.id);
const nameOf = (x: N | string) => (typeof x === "string" ? x : x.name);

export default function Graph({
  data, focus, focusName, selected, onSelect, onOpen, height,
}: {
  data: GraphData;
  focus?: string;
  focusName: string;
  selected?: string | null;
  onSelect?: (n: GNode | null) => void;
  onOpen?: (n: GNode) => void;
  height?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fg = useRef<any>(null);
  const [size, setSize] = useState({ w: 800, h: height ?? 600 });
  const [hover, setHover] = useState<N | null>(null);
  const [hoverLink, setHoverLink] = useState<L | null>(null);
  const [pinnedLink, setPinnedLink] = useState<L | null>(null);
  // dwell: a hover takes effect only once the pointer has rested on the thing; leaving cancels it
  // at once, so sweeping the mouse across a fan of edges lights nothing
  const dwell = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rest = useCallback((apply: () => void, ms: number) => {
    if (dwell.current) clearTimeout(dwell.current);
    dwell.current = setTimeout(apply, ms);
  }, []);
  const onNodeHover = useCallback((o: object | null) => {
    if (dwell.current) clearTimeout(dwell.current);
    if (!o) { setHover(null); return; }
    rest(() => { setHoverLink(null); setHover(o as N); }, 120);
  }, [rest]);
  const onLinkHover = useCallback((o: object | null) => {
    if (dwell.current) clearTimeout(dwell.current);
    if (!o) { setHoverLink(null); return; }
    rest(() => setHoverLink(o as L), 260);
  }, [rest]);
  const fitted = useRef(false);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: height ?? el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: height ?? el.clientHeight });
    return () => ro.disconnect();
  }, [height]);

  // a resize after the first fit re-fits (the canvas keeps the old viewport otherwise)
  useEffect(() => {
    if (fitted.current && fg.current) fg.current.zoomToFit(300, 36);
  }, [size]);

  const graph = useMemo(() => ({ nodes: data.nodes.map((n) => ({ ...n })) as N[], links: data.links.map((l) => ({ ...l })) as L[] }), [data]);
  const adj = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const l of data.links) {
      (m.get(l.source) ?? m.set(l.source, new Set()).get(l.source)!).add(l.target);
      (m.get(l.target) ?? m.set(l.target, new Set()).get(l.target)!).add(l.source);
    }
    return m;
  }, [data]);

  // what is lit: the hovered node (or the selected one when nothing is hovered) and its
  // neighbours; or a hovered edge and its two ends
  const active = hover?.id ?? selected ?? null;
  const litLink = hoverLink ?? (hover ? null : pinnedLink);
  const lit = useMemo(() => {
    if (litLink) return new Set([idOf(litLink.source), idOf(litLink.target)]);
    if (!active) return null;
    return new Set([active, ...(adj.get(active) ?? [])]);
  }, [active, adj, litLink]);
  const linkLit = useCallback((l: L) => {
    if (litLink) return l === litLink;
    if (!active) return true;
    return idOf(l.source) === active || idOf(l.target) === active;
  }, [active, litLink]);

  // The forces are set when the canvas mounts (the renderer is a dynamic import, so a plain effect
  // runs before the ref exists) and again whenever the data changes.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const configure = useCallback((g: any) => {
    fitted.current = false;
    // labelled nodes need room for their label, so they repel harder and collide wider
    const charge = (d: unknown) => { const n = d as N; return n.type === "subsidiary" ? -4 : n.type === "jurisdiction" ? -14 * Math.sqrt(n.val) : -30 * Math.sqrt(n.val); };
    g.d3Force("charge", forceManyBody().strength(charge).distanceMax(500));
    g.d3Force("center", null);
    g.d3Force("x", forceX(0).strength((d: unknown) => ((d as N).type === "subsidiary" ? 0.01 : 0.04)));
    g.d3Force("y", forceY(0).strength((d: unknown) => ((d as N).type === "subsidiary" ? 0.01 : 0.04)));
    g.d3Force("collide", forceCollide((d: unknown) => { const n = d as N; return Math.sqrt(n.val) + (n.type === "subsidiary" ? 1.5 : n.type === "jurisdiction" ? 8 : 18); }).iterations(2));
    const link = g.d3Force("link");
    if (link) {
      link.distance((l: L) => {
        if (l.kind === "jurisdiction") { const src = l.source as unknown as N | string; return 48 + Math.sqrt(typeof src === "object" ? src.count ?? 1 : 1) * 10; }
        if (HOLDING.has(l.kind)) return DIST[l.kind] - pctOf(l) * 4;
        return DIST[l.kind] ?? 60;
      }).strength((l: L) => {
        if (l.kind === "subsidiary_of") return 0.9;
        if (l.kind === "jurisdiction") return 0.5;
        if (l.kind === "family") return 0.6;
        if (HOLDING.has(l.kind)) return 0.15 + pctOf(l) / 50;
        return 0.3;
      });
    }
    g.d3ReheatSimulation();
  }, []);

  useEffect(() => {
    let tries = 0;
    const tick = () => { if (fg.current) configure(fg.current); else if (tries++ < 120) requestAnimationFrame(tick); };
    tick();
  }, [graph, configure]);

  const draw = useCallback((node: N, ctx: CanvasRenderingContext2D, scale: number) => {
    const r = Math.sqrt(node.val);
    const x = node.x ?? 0, y = node.y ?? 0;
    const on = !lit || lit.has(node.id);
    const isActive = node.id === active || node.id === focus;
    const color = COLORS[node.type];
    ctx.globalAlpha = !on ? 0.12 : node.type === "subsidiary" ? 0.8 : node.meta?.valid_to ? 0.5 : 1;
    // halo behind a lit node
    if (on && (isActive || (lit && node.type !== "subsidiary"))) {
      ctx.beginPath();
      ctx.arc(x, y, r + (isActive ? 4 : 2.5) / Math.sqrt(scale), 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.globalAlpha = (isActive ? 0.28 : 0.16) * (on ? 1 : 0);
      ctx.fill();
      ctx.globalAlpha = !on ? 0.12 : 1;
    }
    // the disc, with a soft rim
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = Math.max(0.6 / scale, 0.25);
    ctx.strokeStyle = isActive ? "#f8fafc" : "rgba(255,255,255,0.28)";
    if (isActive) ctx.lineWidth = 1.5 / scale;
    ctx.stroke();
    const showLabel = node.type !== "subsidiary" ? on : scale > 3.2 && on;
    if (!showLabel) { ctx.globalAlpha = 1; return; }
    const fs = node.type === "company" || node.type === "industry" ? Math.max(11 / scale, 3.2) : node.type === "subsidiary" ? 9 / scale : node.type === "jurisdiction" ? Math.max(8 / scale, 2) : Math.max(10.5 / scale, 2.6);
    ctx.font = `${node.type === "company" || isActive ? "600 " : ""}${fs}px ui-sans-serif, system-ui`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const short = node.name.length > 42 && node.type !== "company" ? node.name.slice(0, 40) + "…" : node.name;
    const label = node.type === "jurisdiction" ? (node.id === "jur:all" ? node.name : `${node.name} (${node.count})`) : node.type === "holder" || node.type === "individual-holder" || node.type === "issuer"
      ? `${short}${node.meta?.valid_to ? " (exited)" : node.meta?.pct != null ? ` ${node.meta.pct}%` : ""}` : short;
    // dark halo under the text so it reads over edges
    ctx.lineJoin = "round";
    ctx.lineWidth = 3 / scale;
    ctx.strokeStyle = BG;
    ctx.strokeText(label, x, y + r + 2);
    ctx.fillStyle = node.type === "subsidiary" ? "rgba(226,232,240,0.85)" : node.type === "jurisdiction" ? "rgba(203,213,225,0.7)" : isActive ? "#ffffff" : "#e2e8f0";
    ctx.fillText(label, x, y + r + 2);
    ctx.globalAlpha = 1;
  }, [focus, active, lit]);

  const paintArea = useCallback((node: N, color: string, ctx: CanvasRenderingContext2D) => {
    ctx.beginPath();
    ctx.arc(node.x ?? 0, node.y ?? 0, Math.sqrt(node.val) + 2, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
  }, []);

  const linkColor = useCallback((o: object) => {
    const l = o as L;
    const rgb = LINK_RGB[l.kind] ?? "148,163,184";
    if (!lit) return `rgba(${rgb},${LINK_ALPHA[l.kind] ?? 0.4})`;
    return linkLit(l) ? `rgba(${rgb},0.95)` : `rgba(${rgb},0.05)`;
  }, [lit, linkLit]);
  const linkWidth = useCallback((o: object) => { const l = o as L; const w = l.width ?? 0.6; return lit && linkLit(l) ? w + 1.2 : w; }, [lit, linkLit]);

  return (
    <div ref={box} className="graph-box" style={height ? { height } : undefined}>
      <ForceGraph2D
        ref={fg}
        width={size.w}
        height={size.h}
        graphData={graph}
        backgroundColor={BG}
        nodeCanvasObject={draw as unknown as (node: object, ctx: CanvasRenderingContext2D, scale: number) => void}
        nodePointerAreaPaint={paintArea as unknown as (node: object, color: string, ctx: CanvasRenderingContext2D) => void}
        nodeLabel={(o: object) => { const n = o as N; return `<div class="tip"><b>${n.name}</b><br/><span>${TYPE_LABEL[n.type]}</span><br/>${nodeText(n, focusName)}</div>`; }}
        linkColor={linkColor}
        linkWidth={linkWidth}
        linkDirectionalArrowLength={(o: object) => (HOLDING.has((o as L).kind) ? 4 : 0)}
        linkLineDash={(o: object) => { const l = o as L; return l.kind === "family" ? [3, 2] : HOLDING.has(l.kind) && l.meta?.valid_to ? [5, 4] : null; }}
        linkDirectionalArrowRelPos={1}
        linkDirectionalArrowColor={linkColor}
        linkLabel={(o: object) => { const l = o as L; return `<div class="tip">${edgeText(l.kind, l.meta, nameOf(l.source), nameOf(l.target))}</div>`; }}
        onNodeHover={onNodeHover}
        onLinkHover={onLinkHover}
        linkHoverPrecision={2}
        onNodeClick={(o: object) => { setPinnedLink(null); onSelect?.(o as N); }}
        onLinkClick={(o: object) => { setPinnedLink(o as L); onSelect?.(null); }}
        onNodeRightClick={(o: object) => onOpen?.(o as N)}
        onBackgroundClick={() => { setPinnedLink(null); onSelect?.(null); }}
        cooldownTicks={260}
        warmupTicks={80}
        onEngineStop={() => {
          if (!fitted.current && fg.current) {
            fitted.current = true;
            fg.current.zoomToFit(500, 36);
          }
        }}
        enableNodeDrag={true}
      />
    </div>
  );
}
