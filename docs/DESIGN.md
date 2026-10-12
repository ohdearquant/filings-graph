# Design

filings-graph reads public SEC filings for a fixed list of large US companies and loads what they say about ownership and control into a local khive store. This page describes what it builds, where each fact comes from, and where it stops. Every statement here can be checked in the code named beside it.

## What it builds

Entities, all stored as khive entities with the kind of record in `properties.type`:

| Type | What it is | Identity |
| --- | --- | --- |
| `company` | a company on the universe list | `urn:sec:cik:<cik>` |
| `subsidiary` | a row of the company's Exhibit 21 | `urn:filings-graph:sub:<company cik>:<normalized name>` |
| `holder`, `individual-holder` | a filer of a Schedule 13D or 13G, or a name in a proxy ownership table | `urn:sec:cik:<cik>` when a CIK is known, else `urn:filings-graph:org:<name>` or `urn:filings-graph:person:<name>` |
| `issuer` | a registrant in which a listed company reports a stake, or a predecessor registrant | `urn:sec:cik:<cik>` |
| `industry` | a four-digit SIC code | `urn:sec:sic:<code>` |

Edges all use the base khive relation `part_of`, except the industry link, which is `instance_of`. The kind of fact is in `metadata.predicate`:

| Predicate | Direction | Meaning |
| --- | --- | --- |
| `in_industry` | company to industry | the SIC code in the company's submissions header |
| `subsidiary_of` | subsidiary to company | listed in the Exhibit 21 of the company's newest 10-K; `control: true` |
| `beneficial_owner` | holder to company (or issuer) | reported ownership above 5 percent; `control: false` |

Filings are not nodes. The newest 10-K and proxy statement are listed on the company (`properties.filings`), and the 8-Ks filed since the 10-K are listed as dated events (`properties.events`), each with its items and its accession number. Code: `build.archive`, `build.py` from the loop over `pulled`.

## The universe

`data/universe.csv` has three columns, `cik`, `ticker` and `name`. It is a fixed list of about 500 large US filers, written once and read as it is. Nothing in the build derives it. A company that is not on the list is never pulled. It can still appear as an `issuer` when a listed company files a 13D or 13G about it, and as a `holder` when it files one about a listed company. Code: `build.universe`.

## Where each fact comes from

For each company the pull reads the SEC submissions index, then these documents (`build.pull_company`):

- **Header.** Name, tickers, exchanges, SIC code and description, state of incorporation, LEI, EIN, fiscal year end, former names. When the header has no state of incorporation, the 10-K cover page's inline XBRL tag is used.
- **Exhibit 21.** The filing index of the newest 10-K (form exactly `10-K`) is read, and every document whose type starts with `EX-21` is parsed into rows of name, jurisdiction and, when the exhibit states one, an ownership percent. The row keeps the raw line it came from. A row that names the registrant itself is dropped.
- **Schedule 13D and 13G.** Every 13D, 13G and amendment in the company's submissions since the earlier of one year ago and the 10-K's period end, so the window is never shorter than twelve months. The filing's index headers say whether the company is the subject or the filer. Where the company is the subject, the filing is read in full: the structured XML for filings from late 2024 on, the headers and a pattern match on the cover page for older ones. Where the company is the filer and the subject is also on the list, the filing is read the same way and becomes an outbound stake. Where the subject is not on the list, only the headers are read, and at most 300 such stakes are kept per company.
- **Proxy statement.** The newest DEF 14A is parsed for the beneficial-ownership tables required by Item 403 of Regulation S-K (`proxy.py`). A table is read when its header names a percent column and a holder column. Only rows at 5 percent or more become edges. Rows the parser refuses are kept on the company under `proxy_rejected` with the reason.
- **8-K.** Every 8-K filed on or after the 10-K's filing date, with its item numbers.

A company whose filing history continues under a predecessor CIK (a holding-company reorganization, for example) can be given a predecessors file with `pull --predecessors`. The filings under both CIKs are then read as one history. Without that file each CIK stands alone.

## Edge metadata

A `subsidiary_of` edge carries the Exhibit 21 accession in `asserted`, the percent and jurisdiction when stated, the raw line, and `valid_from` and `last_asserted` set to the 10-K's period end.

A `beneficial_owner` edge keeps every filing that said something about the pair, not only the newest. `metadata.observations` has one entry per filing with the accession, the form, percent, share count, the date the stake was true (`as_of`), the date the filing was made (`filed`), where the number was read from (`xml`, `headers-only` or `proxy-table`), and the share class. The edge's own `pct`, `shares`, `as_of` and `form` are copied from the newest clean observation, taken from the holder's own filings when there are any and from the company's proxy otherwise. The edge weight is the percent over 100 plus 0.5, capped at 1.

Edges are merged by source, target, relation and share class. The accession lists are joined and the newest assertion decides the current state. Share class is the CUSIP when a filing gives one and a normalized title otherwise.

An observation from a holder's own filing is flagged `suspect` when it reports 5 percent or more with no share count or fewer than 1,000 shares, or when its event date is more than 400 days before its filing date. Proxy rows carry their own `suspect` flag from the table parser. Suspect observations stay on the edge. They are skipped when the current state is chosen.

An edge is closed when the newest clean own filing reports under 5 percent or ticks the "5 percent or less" box. `valid_to` is that filing's event date and `exit_filed` is its filing date. `exit_kind` says why, in this order of tests (`build._exit_kind`):

| `exit_kind` | Test |
| --- | --- |
| `filer_split` | the filing's text says the position was transferred, reallocated or is now reported separately, or a differently registered filer with a similar name reports about the same share count on the same day (13G) |
| `transferred` | the same-day match, for 13D |
| `merger` | the holder reports zero shares and the 13D text describes a merger or acquisition that disposed of, cancelled, converted or exchanged them |
| `sold` | shares fell by more than 1 percent from the previous filing |
| `diluted` | shares did not fall by more than 1 percent and the percent fell |
| `drift` | shares are unchanged within 1 percent and the percent is between 4.5 and 5 |
| `threshold_flag` | the filing ticks the "5 percent or less" box while reporting 5 percent or more |
| `unknown` | none of the above |

A ninth value, `departed`, is set when a holder is named in an earlier filing by a filer and left out of a later one that still names another of the same members, and the holder has no newer live filing of its own.

## Point in time

A reader asking about a past date can only know what had been filed by then. `graph.state_as_of(metadata, as_of)` keys on the filing date and ignores the event date:

- an observation counts only if its `filed` is on or before the date;
- among the visible observations the state is the newest clean own filing, else the newest clean proxy row, else the newest visible one with no percent;
- the edge is closed only from `exit_filed`, so a holder whose exit was filed after the date is still shown at the last stake that was public;
- with no date given, the stored current state is used, and a closed edge is not a current holder.

The tests in `tests/test_graph_as_of.py` pin these rules on edge metadata as the archive stores it.

## Reading the store

`graph.FinGraph` has one method per khive read verb (`search`, `get`, `neighbors`, `edges`, `context`) and a few named walks:

- `controlled_by` and `controllers_of` follow `part_of` edges with `control: true` down or up, to a depth of 5 by default.
- `holders(company, as_of=...)` lists incoming `control: false` edges with their percent, date, form and intent (`active` for a 13D, `passive` for a 13G, "as reported by the company" for a proxy row). Each row also has a time weight, `exp(-ln 2 * age / half_life)` with a half-life of 365 days, computed at read time and never stored.
- `stakes(company)` lists what the company itself reports holding in other registrants.

`scripts/ask.py` puts these behind a command line: `company`, `holders`, `who-controls`, `holder`, `json` and `activist-exits`. Every number it prints comes back from the store on that call.

Nothing in the build ranks holders, merges judgments or calls a model. `pipeline.py` holds an unfinished extraction stage that reads 10-K sections with a model. The build does not call it and this page does not describe it.

## Stages

`python -m filings_graph.build` has five commands:

1. `pull` writes one JSON file per company. A company that already has a file is skipped. `--refresh` rebuilds the files from the cache alone, without touching the network.
2. `archive` derives entities and edges from those files alone and writes one immutable archive file with a manifest. The manifest names the SHA-256 of the universe file and of the pulled files, and counts entities and edges by type and predicate. It refuses to replace an existing archive.
3. `config` writes a khive store config for a new SQLite file and refuses to reuse a store.
4. `load` creates entities in slices of 100 and edges in slices of 1,000 through the stock `kkernel` binary, then embeds every entity so text search works.
5. `all` runs pull, archive, config and load for a named generation.

The binary is `kkernel` from khive, found through `FILINGS_GRAPH_KKERNEL` or `~/.cargo/bin/kkernel`. `scripts/demo.sh` builds it at a pinned revision when it is absent, runs `all` for a few companies, and asks one question of the result.

## EDGAR access

Every byte read from sec.gov is cached under `data/external/edgar/` (or `FILINGS_GRAPH_EDGAR_CACHE`) at the same path it has on the server. A second run reads the cache. The client waits 0.11 seconds between requests, which is under the published limit of ten a second, retries a 429 or 503 after 2, 5, 15 and 60 seconds, and declares a User-Agent with the contact in `FILINGS_GRAPH_CONTACT`. It refuses to start without one, because sec.gov asks every automated client to say who is asking. An offline rebuild reads only the cache and fails on a miss. Code: `edgar.py`.

## The page

`web/` is a Next.js page over one archive file. `/` lists industries and searches companies, `/c/<cik>` shows one company with its holders, subsidiaries by place of incorporation, stakes and 8-K timeline, and `/i/<sic>` shows one industry with the holders that sit in two or more of its companies. It reads the archive file directly and does not call the store.

## Limits

- The parsers are deterministic and have known misses. Exhibit 21 layouts vary a great deal, proxy tables more so, and older 13D and 13G filings have no structured data. A row the parser could not read is absent, not guessed. Proxy rows keep their raw text, and suspect or rejected rows are recorded rather than dropped.
- A percent from a proxy table is what the company printed, as of the date in the table's heading, else the proxy's record date, else the filing date.
- Holders are keyed by CIK. A bare brand name in a proxy table (four large asset managers) is resolved to that manager's CIK, which is a fixed mapping in the code. Other names are matched by legal name to a CIK seen in a 13D or 13G, preferring one that filed about the same company, and stay name-keyed when nothing matches.
- The ownership window is one year or since the 10-K's period end, whichever reaches further back. A holder that last filed before the window does not appear.
- Subsidiaries come from the newest 10-K only. A subsidiary dropped from a later exhibit is not tracked as an exit.
- The 8-K events are the items EDGAR lists. The text of an 8-K is not read.
- `tests/` has 54 tests. 24 run on the repository alone. 30 read real filings and the pulled files they were written against, and are skipped with a stated reason when `data/external/` does not hold them. A fresh pull will differ from those files where newer amendments have been filed.
- The store is local and there is no incremental update: a new generation is a new store.
