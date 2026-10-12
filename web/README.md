# filings-graph web

The graph page for one filings-graph generation. Next.js 16, React 19, a force-directed canvas (react-force-graph-2d + d3-force).

```bash
cd web
pnpm install
pnpm dev --port 3300            # http://localhost:3300
FILINGS_GRAPH_ARCHIVE=/abs/path/filings-graph-v0.2.json pnpm dev --port 3300   # another generation
```

Data: the archive JSON (`../data/external/filings-graph/filings-graph-v0.1.json` by default) loaded once into memory
(`src/lib/archive.ts`); no store round trip on a page. The khive store is the system of record and the
MCP surface; the page is the reading surface for the same generation.

Pages: `/` industries by company count + search · `/c/<cik>` one company: shareholders over 5% (left),
industry (top), subsidiaries by place of incorporation (right), stakes it holds (bottom); the panel carries
the record, the 8-K events since the 10-K as a timeline, and the filings it was read from ·
`/i/<sic>` one industry: its companies and the shareholders that sit in two or more of them.

API: `/api/search?q=` · `/api/company/<cik>` (record + graph) · `/api/industry/<sic>`.

Labels: `src/lib/labels.ts` is the only place that turns store vocabulary into words on the screen
(relation and predicate names, metadata keys and entity type names never render).
