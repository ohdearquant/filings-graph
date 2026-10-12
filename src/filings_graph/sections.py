"""Split a 10-K's primary document into its items (deterministic, cached per accession).

An inline-XBRL 10-K is one HTML file of 1 to 5 MB. The text is stripped of tags, then the item
headings are found by the pattern `Item 1.`, `Item 1A.`, `Item 7.` ... at line start. The table
of contents repeats every heading once near the top, so for each item the LAST heading whose
following text is longer than a table-of-contents entry wins. Every section carries its
character offsets into the stripped text, so a quote can be located again.
"""

from __future__ import annotations

import html as htmlmod
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

ITEMS = (
    "1",
    "1A",
    "1B",
    "1C",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "7A",
    "8",
    "9",
    "9A",
    "9B",
    "9C",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
    "16",
)
_HEAD = re.compile(r"^\s*item\s+(\d{1,2}[a-c]?)\s*[.:\-–—]?\s*(.{0,120})$", re.IGNORECASE | re.MULTILINE)
_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAGS = re.compile(r"</?(?:p|div|tr|br|li|h\d|table|td|th)\b[^>]*>", re.IGNORECASE)
_HIDDEN = re.compile(
    r"<(?:ix:header|script|style)\b.*?</(?:ix:header|script|style)>", re.DOTALL | re.IGNORECASE
)


@dataclass(frozen=True)
class Section:
    item: str
    title: str
    start: int
    end: int

    def text(self, body: str) -> str:
        return body[self.start : self.end]


def strip_html(raw: str) -> str:
    """The document text, block tags as line breaks, inline XBRL header and scripts removed,
    entities decoded, runs of blank lines collapsed."""
    raw = _HIDDEN.sub(" ", raw)
    raw = _BLOCK_TAGS.sub("\n", raw)
    text = htmlmod.unescape(_TAG.sub(" ", raw))
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out)


def split_items(body: str, *, min_section_chars: int = 400) -> list[Section]:
    """Sections in document order. A heading counts only when the text up to the next heading is
    at least `min_section_chars` long (a table-of-contents line is followed by another heading
    within a few characters); for an item that appears more than once, the last such heading wins."""
    heads = [(m.start(), m.group(1).upper(), m.group(2).strip()) for m in _HEAD.finditer(body)]
    heads = [h for h in heads if h[1] in ITEMS]
    chosen: dict[str, tuple[int, str, int]] = {}
    for i, (pos, item, title) in enumerate(heads):
        nxt = heads[i + 1][0] if i + 1 < len(heads) else len(body)
        if nxt - pos >= min_section_chars:
            chosen[item] = (pos, title, nxt)
    ordered = sorted(chosen.items(), key=lambda kv: kv[1][0])
    out: list[Section] = []
    for j, (item, (pos, title, _)) in enumerate(ordered):
        end = ordered[j + 1][1][0] if j + 1 < len(ordered) else len(body)
        out.append(Section(item=item, title=title[:120], start=pos, end=end))
    return out


def sections_for(raw_html: str) -> tuple[str, list[Section]]:
    body = strip_html(raw_html)
    return body, split_items(body)


def cache_sections(raw_html: str, out: Path) -> tuple[str, list[Section]]:
    """Write `<out>.txt` (the stripped body) and `<out>.json` (the sections) once; read them back after."""
    txt, js = out.with_suffix(".txt"), out.with_suffix(".json")
    if txt.exists() and js.exists():
        body = txt.read_text()
        return body, [Section(**s) for s in json.loads(js.read_text())]
    body, secs = sections_for(raw_html)
    out.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text(body)
    js.write_text(json.dumps([asdict(s) for s in secs]))
    return body, secs
