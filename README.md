# filings-graph

Who owns a large US company, and what it controls, is already on public record, spread over filings made months apart: Exhibit 21 of the newest 10-K lists the subsidiaries, every holder above 5 percent files a Schedule 13D or 13G, and the proxy statement prints an ownership table. filings-graph reads those filings from SEC EDGAR for a fixed list of about 500 large US filers and loads each stated fact into a local khive graph, together with the filing that stated it. Parsers read the filings, and the one model in the build is a small embedding model (all-MiniLM-L6-v2), downloaded on the first run, which khive uses to index the records for search.

```bash
FILINGS_GRAPH_CONTACT=you@example.com bash scripts/demo.sh
```

On a shared Ubuntu 24.04 machine with 16 CPUs, the first run spent 224 seconds building `kkernel`, once. After that, `bash scripts/demo.sh 2` (two companies) took 22 seconds.

What it printed at the end of that run:

```text
ABBOTT LABORATORIES  cik 0000001800  tickers ['ABT']  jurisdiction IL  FYE 1231  SIC 2834

controls 525 entities (Exhibit 21, layer<=5); by jurisdiction:
    96  Delaware
    31  United Kingdom
    26  Luxembourg
    21  Netherlands
    18  Germany
    17  Chile
    14  Ireland
    14  Malta
    13  China
    10  Bermuda
     8  Colombia
     8  Singapore

holders >5% (2 rows):
    7.70%  w=0.66  2026-03-13  DEF 14A        as reported by the company  BlackRock, Inc.
    7.50%  w=0.69  2026-03-31  SCHEDULE 13G   passive     Vanguard Capital Management

filings on record: 2 (by form {'DEF 14A': 1, '10-K': 1}); 8-K events since the 10-K: 9
```

The script builds khive's `kkernel` binary once at a pinned revision, pulls the latest filings of three companies from sec.gov, builds the graph, loads it into a SQLite store under `.khive/` and asks about the first company on the list: what it controls, by place of incorporation, and who holds more than 5 percent of it. sec.gov asks every automated client to name a contact address in its requests, and `FILINGS_GRAPH_CONTACT` is that address, the only setting the script requires.

## Requirements

uv, Python 3.12 or newer, a Rust toolchain for the one-time `kkernel` build (tested with Rust 1.98), and network access. The first run reaches sec.gov for the filings, github.com and crates.io for the `kkernel` build, pypi.org for the Python dependencies, and huggingface.co for the embedding model, which is checksum-verified and saved once under `~/.lattice/models` (about 87 MB). The script removes group and world write from the checkout and its store directory itself. `kkernel` also refuses a store with a group- or world-writable directory above the checkout; if one is, the script stops and names it.

## What is in the graph

| Record | Where it comes from |
|---|---|
| company | the universe list, with the header of its SEC submissions: name, tickers, SIC code, state of incorporation |
| subsidiary | a row of Exhibit 21 in the company's newest 10-K |
| holder | the filer of a Schedule 13D or 13G about the company, or a name in its proxy ownership table |
| issuer | a registrant in which a listed company reports a stake |
| industry | a four-digit SIC code |

A subsidiary points at its company with control; a holder points at the company it holds, above 5 percent and without control. Every holder edge keeps one observation per filing about the pair, with the accession number, the form, the percent, the share count, the date the stake was true and the date it was filed. When the holder's newest clean filing reports under 5 percent the edge closes, and the reason is recorded: sold, diluted, merger and six others, each a rule in the code.

## Asking about a past date

`--as-of` keys on the filing date, so a question about March 31 sees only what had been filed by March 31. An exit filed after that date does not count yet, and the holder shows at its last public stake.

```bash
uv run python scripts/ask.py --config .khive/<name>.toml holders MMM --as-of 2026-03-31
```

`ask.py` takes six questions: `company`, `holders`, `who-controls`, `holder`, `json` and `activist-exits`. To build more than three companies, pass a number: `FILINGS_GRAPH_CONTACT=you@example.com bash scripts/demo.sh 50`.

## The graph page

`web/` is a Next.js page over one archive file: an industry with its companies, or one company with its holders, subsidiaries and the 8-K events since its 10-K. The demo writes its archive under `data/external/filings-graph/`, `FILINGS_GRAPH_ARCHIVE` names the file the page reads, and [web/README.md](web/README.md) has the commands.

## Data

| Input | Published by | Terms | Source |
|---|---|---|---|
| SEC EDGAR filings: submissions index, 10-K with Exhibit 21, Schedules 13D and 13G, DEF 14A, 8-K | U.S. Securities and Exchange Commission | free public access; at most 10 requests a second, with a declared User-Agent | https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data |
| `data/universe.csv`, a fixed list of about 500 large US filers (CIK, ticker, name) | in the repository | Apache-2.0, like the rest of the repository | in the repository |

Every byte read from sec.gov is cached under `data/external/edgar/` at its path on the server, so a second run reads the cache. The client waits 0.11 seconds between requests and backs off on a 429 or 503.

## Limits

Exhibit 21 layouts vary a great deal and proxy tables vary more. A row the parser cannot read is left out, and a proxy row it refused stays on the company with its reason. For ownership the window runs back one year, or to the 10-K's period end if that is earlier; subsidiaries come from the newest 10-K alone. Each pull writes a new local store. [docs/DESIGN.md](docs/DESIGN.md) has every rule, with the code that applies it.

## Not investment advice

filings-graph restates what companies and holders filed with the SEC. Nothing it prints is investment advice.

## License

Apache-2.0.

## Built on khive

Under filings-graph is khive (github.com/ohdearquant/khive), an open-source typed knowledge graph on SQLite. filings-graph runs khive's own `kkernel` binary unchanged, at a pinned revision, and stores companies, holders and their links as khive entities and edges.

khive also runs hosted, as memory and a knowledge graph your agents reach over MCP: https://khive.ai/?utm_source=github&utm_campaign=filings-graph
