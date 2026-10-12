"""EDGAR, read through one paced client with a disk cache.

Every byte read from sec.gov lands under `data/external/edgar/` at the same path it has on the
server, so a second run of any step reads the cache and the generation manifest can hash what
was used. The pace is the published limit, ten requests a second, with the declared User-Agent;
a 429 or a 503 waits and retries, anything else raises.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(os.environ.get("FILINGS_GRAPH_EDGAR_CACHE", ROOT / "data/external/edgar"))


def user_agent() -> str:
    """sec.gov asks every automated client to declare who is asking: set FILINGS_GRAPH_CONTACT to an address you read."""
    contact = os.environ.get("FILINGS_GRAPH_CONTACT", "").strip()
    if not contact:
        raise SystemExit(
            "set FILINGS_GRAPH_CONTACT to an email address you read; sec.gov asks every client to declare one"
        )
    return f"filings-graph/0.1 (research; {contact})"


PACE = 0.11  # seconds between requests: under ten a second
RETRY_WAITS = (2.0, 5.0, 15.0, 60.0)

SUBMISSIONS = "https://data.sec.gov/submissions/{name}"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{name}"
COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"


def cik10(cik: int | str) -> str:
    return f"{int(cik):010d}"


class Edgar:
    def __init__(self, cache: Path = CACHE, *, offline: bool = False) -> None:
        self.cache = cache
        self.offline = offline
        ua = "filings-graph/0.1 (offline)" if offline else user_agent()
        self.client = httpx.Client(headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=60)
        self._last = 0.0
        self.requests = 0  # network requests this process made
        self.hits = 0  # answered from the cache
        self.misses = 0  # requests that could not be answered from the cache
        self.skips = 0  # optional offline paths deliberately not requested because they are absent

    # -- transport ------------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        rel = url.split("://", 1)[1]
        return self.cache / rel

    def get_bytes(self, url: str, *, refresh: bool = False) -> bytes:
        path = self._cache_path(url)
        if path.exists() and not refresh:
            self.hits += 1
            return path.read_bytes()
        self.misses += 1
        if self.offline:
            raise FileNotFoundError(f"EDGAR cache miss in offline mode: {path}")
        for wait in (*RETRY_WAITS, None):
            gap = PACE - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            self.requests += 1
            r = self.client.get(url)
            if r.status_code in (429, 503) and wait is not None:
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(path)
        return r.content

    def get_json(self, url: str, *, refresh: bool = False):
        return json.loads(self.get_bytes(url, refresh=refresh))

    # -- EDGAR surfaces -------------------------------------------------------------------

    def company_tickers(self) -> list[dict]:
        return list(self.get_json(COMPANY_TICKERS).values())

    def submissions(self, cik: int | str, *, refresh: bool = False) -> dict:
        """The submissions JSON with every continuation page folded into `filings.recent`, so a
        caller sees one list of filings, newest first, under the same column names."""
        sub = self.get_json(SUBMISSIONS.format(name=f"CIK{cik10(cik)}.json"), refresh=refresh)
        recent = sub["filings"]["recent"]
        for page in sub["filings"].get("files", []):
            more = self.get_json(SUBMISSIONS.format(name=page["name"]), refresh=refresh)
            for col, values in more.items():
                if isinstance(values, list) and col in recent:
                    recent[col].extend(values)
        return sub

    def filings(self, sub: dict) -> list[dict]:
        """`filings.recent` as one dict per filing (the JSON is column-oriented)."""
        rec = sub["filings"]["recent"]
        cols = [c for c, v in rec.items() if isinstance(v, list)]
        n = len(rec["accessionNumber"])
        return [{c: rec[c][i] if i < len(rec[c]) else None for c in cols} for i in range(n)]

    def filing_index(self, cik: int | str, accession: str) -> list[dict]:
        """The files of one filing: name, type (EX-21, GRAPHIC, ...), size, last-modified."""
        acc = accession.replace("-", "")
        idx = self.get_json(ARCHIVES.format(cik=int(cik), acc=acc, name="index.json"))
        return idx.get("directory", {}).get("item", [])

    def document(self, cik: int | str, accession: str, name: str) -> bytes:
        return self.get_bytes(ARCHIVES.format(cik=int(cik), acc=accession.replace("-", ""), name=name))

    def document_url(self, cik: int | str, accession: str, name: str) -> str:
        return ARCHIVES.format(cik=int(cik), acc=accession.replace("-", ""), name=name)

    def has_cached(self, url: str) -> bool:
        return self._cache_path(url).is_file()
