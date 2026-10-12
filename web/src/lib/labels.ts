// What the page says. The store's own vocabulary (part_of, beneficial_owner, subsidiary_of, holder,
// issuer, meta keys) never reaches the screen; every label here is a word a reader uses.
import type { GNode } from "@/lib/archive";

export const TYPE_LABEL: Record<GNode["type"], string> = {
  company: "Company",
  peer: "Company",
  industry: "Industry",
  holder: "Shareholder over 5%",
  "individual-holder": "Individual shareholder",
  issuer: "Company it holds a stake in",
  subsidiary: "Subsidiary",
  jurisdiction: "Place of incorporation",
};

type Meta = Record<string, unknown>;

export function formLabel(form: unknown): string {
  const f = String(form ?? "");
  if (f.includes("13D")) return f.endsWith("/A") ? "Schedule 13D, amended" : "Schedule 13D";
  if (f.includes("13G")) return f.endsWith("/A") ? "Schedule 13G, amended" : "Schedule 13G";
  if (f === "DEF 14A") return "proxy statement";
  return f;
}

export const pctText = (p: unknown) => (p == null ? "share not stated" : `${p}%`);

export function stanceText(meta: Meta): string {
  if (meta.form === "DEF 14A") return "as the company reports it";
  if (meta.intent === "active") return "active investor";
  if (meta.intent === "passive") return "passive";
  return "";
}

/** "Holds 7.56% of 3M CO · Schedule 13G · passive · as of 2026-03-31" */
export function holdingSentence(meta: Meta, companyName: string): string {
  const bits = [`Holds ${pctText(meta.pct)} of ${companyName}`, formLabel(meta.form), stanceText(meta)];
  if (meta.as_of) bits.push(`as of ${meta.as_of}`);
  if (meta.filed) bits.push(`filed ${meta.filed}`);
  if (meta.valid_to) bits.push(`fell under 5% on ${meta.valid_to}`);
  return bits.filter(Boolean).join(" · ");
}

/** "Subsidiary of 3M CO · incorporated in Delaware · 100% owned · from the 10-K's Exhibit 21" */
export function subsidiarySentence(meta: Meta, parentName: string): string {
  const bits = [`Subsidiary of ${parentName}`];
  bits.push(meta.jurisdiction ? `incorporated in ${meta.jurisdiction}` : "place of incorporation not stated");
  if (meta.ownership_pct != null) bits.push(`${meta.ownership_pct}% owned`);
  bits.push("from the 10-K's Exhibit 21");
  return bits.join(" · ");
}

/** The tooltip on a link. */
export function edgeText(kind: string, meta: Meta | undefined, from: string, to: string): string {
  if (kind === "beneficial_owner") return holdingSentence(meta ?? {}, to);
  if (kind === "stake") return `${from} holds ${pctText(meta?.pct)} of ${to}` + (meta?.as_of ? ` · as of ${meta.as_of}` : "");
  if (kind === "subsidiary_of") return subsidiarySentence(meta ?? {}, "the company").replace(" of the company", "");
  if (kind === "jurisdiction") return `subsidiaries incorporated in ${from}`;
  if (kind === "in_industry") return `${from} is in the industry ${to}`;
  if (kind === "family") return `${from} and ${to} report their stakes together, on the same Schedule 13D/13G`;
  return "";
}

/** The tooltip on a node. */
export function nodeText(n: GNode, focusName: string): string {
  switch (n.type) {
    case "holder":
    case "individual-holder": {
      const base = n.meta ? holdingSentence(n.meta, focusName) : n.count != null ? `Holds more than 5% of ${n.count} companies here` : TYPE_LABEL[n.type];
      return n.listed ? `${base} · itself a tracked company` : base;
    }
    case "issuer":
      return `${focusName} holds ${pctText(n.meta?.pct)}` + (n.meta?.as_of ? ` · as of ${n.meta.as_of}` : "");
    case "subsidiary":
      return n.meta ? subsidiarySentence(n.meta, focusName) : "Subsidiary";
    case "jurisdiction":
      return n.id === "jur:all" ? `${n.count} subsidiaries from the 10-K's Exhibit 21; tick "subsidiaries" to open them by place of incorporation` : `${n.count} subsidiaries incorporated here`;
    case "industry":
      return n.count != null ? `${n.count} companies in this industry` : "Industry";
    case "company":
    case "peer":
      return n.count != null ? `${n.count} subsidiaries` : "Company";
  }
}
