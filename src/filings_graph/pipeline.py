"""The extraction pipeline: one `Strategy` value names which model reads which
sections, how many draws, and the verify rules; a `Pipeline` runs one strategy over one company's
10-K and returns a `RelationReport`. Stage outputs a strategy change does not touch (the
extraction draws) are cached on disk under runs/cache/ so a sweep pays only for what changed.

Steps: sections (deterministic, filings_graph.sections) -> mentions (typed lionagi operation, verbose
sampled) -> candidates (the graph's exact, text and dense arms; the graph never picks) ->
hypotheses (one per mention with its resolved target or none) -> verify (deterministic rules:
predicate allowed for the endpoint types, share parses and clears the disclosure threshold,
quote found in the section) -> report. Nothing here writes to the store; the build does not call it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Literal

from lionagi import Branch, iModel
from pydantic import BaseModel, Field

from filings_graph.graph import FinGraph
from filings_graph.sections import Section, cache_sections

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs"
DEFAULT_MODEL = "openrouter/openai/gpt-6-luna"
SYSTEM = "You read SEC filings (10-K sections) for a financial research analyst building a company relationship graph."
LOGS = {"persist_dir": str(RUNS / "logs"), "auto_save_on_exit": False}
Effort = Literal["none", "low", "medium", "high"]
Role = Literal[
    "customer", "supplier", "competitor", "subsidiary", "officer", "director", "acquisition", "partner"
]
Arm = Literal["exact", "text", "dense"]
THRESHOLD = {"customer": 10.0}  # ASC 280: a customer at or above 10% of revenue is disclosed


# -- typed answers (field descriptions are the instructions the model reads) ----------------


class RelationMention(BaseModel):
    """One sentence in a 10-K section that names another organization or a person in a relation
    to the filing company. Nothing is inferred beyond what the sentence says."""

    role: Role = Field(
        description="customer: the company sells to the named party. supplier: the company buys from it, "
        "or depends on it as a source. competitor: named as competing. subsidiary: owned or controlled. "
        "officer / director: a named person with a title. acquisition: bought or agreed to buy. partner: a "
        "named collaboration, licence or joint venture."
    )
    name: str = Field(
        description="The counterparty's name exactly as the section spells it, without 'Inc.' noise removed."
    )
    share: float | None = Field(
        default=None,
        description="A percent stated in the sentence for this party (of revenue, of purchases, of shares), "
        "as a number; null when none is stated.",
    )
    quote: str = Field(
        description="An EXACT verbatim substring of the section, at most 300 characters, that shows the relation. "
        "Copy characters exactly; never paraphrase."
    )
    period: str | None = Field(
        default=None, description="The fiscal year or period the sentence attributes it to, when stated."
    )


class RelationExtraction(BaseModel):
    mentions: list[RelationMention]


class SampledMention(RelationMention):
    samples: int
    frequency: float = Field(ge=0, le=1)
    quotes: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    """One graph entity reached from one mention, with the arm that reached it."""

    id: str
    name: str
    arm: Arm
    score: float | None = None
    type: str | None = None


class Hypothesis(BaseModel):
    mention: SampledMention
    target_id: str | None = Field(
        default=None, description="The resolved entity, or null (an unresolved counterparty)."
    )
    target_name: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)


class Verdict(BaseModel):
    hypothesis: Hypothesis
    verdict: Literal["CONFIRMED", "UNRESOLVED", "REJECT"]
    predicate: str | None = None
    reasons: list[str] = Field(default_factory=list)


class RelationReport(BaseModel):
    cik: str
    accession: str
    strategy: str
    sections: list[str]
    mentions: list[SampledMention]
    verdicts: list[Verdict]
    draws: int


# -- the shape --------------------------------------------------------------------------------


class Strategy(BaseModel):
    """A pipeline shape. Every LLM step is verbose-sampled: `extract_samples` draws from each of
    `extract_models`, aggregated by (role, normalized name), cut at `min_frequency`."""

    name: str
    extract_models: list[str] = Field(
        default=[DEFAULT_MODEL], description="provider/model per reader; several = cross-model."
    )
    extract_effort: Effort = "low"
    extract_samples: int = Field(default=1, ge=1)
    sections: list[str] = Field(
        default=["1", "1A"], description="10-K items read; 8 adds the concentration notes."
    )
    max_section_chars: int = Field(
        default=60_000, description="A longer section is read in windows of this size."
    )
    roles: list[Role] = Field(
        default=["customer", "supplier", "competitor"], description="Roles the reader is asked for."
    )
    min_frequency: float = Field(default=0.5, ge=0, le=1)
    arms: list[Arm] = Field(default=["exact", "text", "dense"])
    per_arm_limit: int = Field(default=5, ge=1, le=20)
    resolve_min_score: float = Field(
        default=0.02, description="Below this a text/dense hit does not resolve the mention."
    )


STRATEGIES: dict[str, Strategy] = {
    "single": Strategy(name="single"),
    "sampled": Strategy(name="sampled", extract_samples=3),
    "with-notes": Strategy(name="with-notes", sections=["1", "1A", "8"]),
}


def grid(base: Strategy, **axes: list) -> list[Strategy]:
    import itertools

    keys = list(axes)
    out = []
    for values in itertools.product(*(axes[k] for k in keys)):
        fields = dict(zip(keys, values, strict=True))
        out.append(
            base.model_copy(
                update={**fields, "name": base.name + "".join(f"-{k}={v}" for k, v in fields.items())}
            )
        )
    return out


# -- the model step ---------------------------------------------------------------------------


def chat_model(spec: str = DEFAULT_MODEL, *, effort: str = "low") -> iModel:
    provider, model = spec.split("/", 1)
    if provider == "openrouter":
        return iModel(provider=provider, model=model, reasoning={"effort": effort})
    return iModel(provider=provider, model=f"{model}-{effort}")


class DrawFailed(RuntimeError):
    pass


async def extract(
    section_text: str, *, item: str, company: str, roles: list[str], model: str, effort: str
) -> RelationExtraction:
    async with Branch(system=SYSTEM, chat_model=chat_model(model, effort=effort), log_config=LOGS) as b:
        answer = await b.communicate(
            instruction=f"List every sentence in this 10-K Item {item} of {company} that names another organization "
            f"or person in one of these relations to the company: {', '.join(roles)}.",
            guidance="Each entry follows its field descriptions. The quote is copied from the section character for "
            "character. A party named several times with the same role is one entry with the clearest quote. "
            "Generic phrases (our customers, several suppliers) without a name are not entries.",
            context={"company": company, "item": item, "section": section_text},
            response_format=RelationExtraction,
        )
    if not isinstance(answer, RelationExtraction):
        raise DrawFailed(f"the draw returned {type(answer).__name__}: {str(answer)[:300]!r}")
    return answer


def _key(m: RelationMention) -> tuple[str, str]:
    return (m.role, re.sub(r"[^a-z0-9]+", " ", m.name.lower()).strip())


def aggregate(runs: list[RelationExtraction]) -> list[SampledMention]:
    groups: dict[tuple[str, str], list[RelationMention]] = defaultdict(list)
    for run in runs:
        seen: set[tuple[str, str]] = set()
        for m in run.mentions:
            k = _key(m)
            if k not in seen:
                seen.add(k)
                groups[k].append(m)
    out = []
    for k, ms in groups.items():
        first = ms[0]
        shares = [m.share for m in ms if m.share is not None]
        out.append(
            SampledMention(
                role=first.role,
                name=first.name,
                share=max(shares) if shares else None,
                quote=first.quote,
                period=first.period,
                samples=len(runs),
                frequency=len(ms) / len(runs),
                quotes=sorted({m.quote for m in ms}),
            )
        )
    return out


# -- the pipeline -----------------------------------------------------------------------------


class Pipeline:
    def __init__(
        self, strategy: Strategy, graph: FinGraph | None, *, seed: int = 0, cache: Path = RUNS / "cache"
    ) -> None:
        self.s = strategy
        self.graph = graph
        self.seed = seed
        self.cache = cache

    def _cached(self, stage: str, key: dict):
        h = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
        return self.cache / stage / f"{h}.json"

    async def mentions(
        self, body: str, secs: list[Section], *, company: str, accession: str
    ) -> tuple[list[SampledMention], int]:
        draws: list[RelationExtraction] = []
        for item in self.s.sections:
            sec = next((x for x in secs if x.item == item), None)
            if sec is None:
                continue
            text = sec.text(body)
            windows = [
                text[i : i + self.s.max_section_chars] for i in range(0, len(text), self.s.max_section_chars)
            ] or [text]
            for w, window in enumerate(windows):
                key = {
                    "seed": self.seed,
                    "accession": accession,
                    "item": item,
                    "window": w,
                    "models": self.s.extract_models,
                    "effort": self.s.extract_effort,
                    "samples": self.s.extract_samples,
                    "roles": self.s.roles,
                }
                path = self._cached("extract", key)
                if path.exists():
                    draws.extend(RelationExtraction.model_validate(d) for d in json.loads(path.read_text()))
                    continue
                got = await asyncio.gather(
                    *(
                        extract(
                            window,
                            item=item,
                            company=company,
                            roles=self.s.roles,
                            model=m,
                            effort=self.s.extract_effort,
                        )
                        for m in self.s.extract_models
                        for _ in range(self.s.extract_samples)
                    )
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps([d.model_dump() for d in got]))
                draws.extend(got)
        n = len(self.s.extract_models) * self.s.extract_samples
        return [m for m in aggregate(draws) if m.frequency >= self.s.min_frequency] if draws else [], n

    def candidates(self, m: SampledMention) -> list[Candidate]:
        """The named arms, concatenated with their arm; the model (or verify) chooses, never the graph."""
        if self.graph is None:
            return []
        out: list[Candidate] = []
        if "exact" in self.s.arms:
            for c in self.graph.by_tag("company"):
                if re.sub(r"[^a-z0-9]+", " ", c["name"].lower()).strip() == _key(m)[1]:
                    out.append(
                        Candidate(
                            id=c["id"],
                            name=c["name"],
                            arm="exact",
                            score=1.0,
                            type=(c.get("properties") or {}).get("type"),
                        )
                    )
        if "text" in self.s.arms or "dense" in self.s.arms:
            for h in self.graph.search(m.name, limit=self.s.per_arm_limit):
                src = h.get("source")
                arm: Arm = "dense" if src == "vector" else "text"
                if arm in self.s.arms or src == "both":
                    out.append(Candidate(id=h["id"], name=h["name"], arm=arm, score=h.get("score")))
        return out

    def verify(self, h: Hypothesis, body: str) -> Verdict:
        m = h.mention
        reasons: list[str] = []
        predicate = {
            "customer": "major_customer",
            "supplier": "supplier",
            "competitor": "competitor",
            "subsidiary": "subsidiary_of",
            "officer": "officer",
            "director": "director",
            "acquisition": "acquisition",
            "partner": "partner",
        }[m.role]
        if not any(q in body for q in m.quotes):
            reasons.append("quote not found verbatim in the document")
        if m.role == "customer" and (m.share is None or m.share < THRESHOLD["customer"]):
            reasons.append(
                f"customer share {m.share} under the {THRESHOLD['customer']}% disclosure threshold or absent"
            )
        if reasons:
            return Verdict(hypothesis=h, verdict="REJECT", predicate=predicate, reasons=reasons)
        if h.target_id is None:
            return Verdict(
                hypothesis=h,
                verdict="UNRESOLVED",
                predicate=predicate,
                reasons=["no entity resolved; stays a counterparty"],
            )
        return Verdict(hypothesis=h, verdict="CONFIRMED", predicate=predicate)

    async def run(self, *, cik: str, company: str, accession: str, raw_html: str) -> RelationReport:
        body, secs = cache_sections(raw_html, RUNS / "sections" / accession.replace("-", ""))
        ments, draws = await self.mentions(body, secs, company=company, accession=accession)
        verdicts = []
        for m in ments:
            cands = self.candidates(m)
            best = next((c for c in cands if c.arm == "exact"), None) or next(
                (c for c in cands if c.score is not None and c.score >= self.s.resolve_min_score), None
            )
            h = Hypothesis(
                mention=m,
                target_id=best.id if best else None,
                target_name=best.name if best else None,
                candidates=cands,
            )
            verdicts.append(self.verify(h, body))
        return RelationReport(
            cik=cik,
            accession=accession,
            strategy=self.s.name,
            sections=[s.item for s in secs],
            mentions=ments,
            verdicts=verdicts,
            draws=draws,
        )
