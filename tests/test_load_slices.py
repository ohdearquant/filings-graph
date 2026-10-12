"""load() must keep every kkernel request under the per-argument limit of Linux (131,072 bytes; the ops JSON is
one argv element). A full link slice of 1000 edges runs to ~300 KB, so slices are cut by bytes as well as count."""

import json

from filings_graph import build
from filings_graph.models import Archive, Edge, Entity

LINUX_ARG_LIMIT = 131_072


class FakeKhive:
    """Stands in for the kkernel client: records the size of the JSON each call would put on argv."""

    def __init__(self, config):
        self.command = ["kkernel", "exec", "--config", str(config)]
        self.sizes: list[tuple[str, int]] = []
        self.linked = 0
        self.created = 0

    def call(self, verb, **args):
        payload = [{"tool": verb, "args": {"namespace": "local", **args}}]
        self.sizes.append((verb, len(json.dumps(payload).encode())))
        if verb == "create":
            items = args["items"]
            rows = [
                {"id": f"id-{self.created + i}", "properties": {"uri": item["properties"]["uri"]}}
                for i, item in enumerate(items)
            ]
            self.created += len(items)
            return {"entities": rows}
        self.linked += len(args["links"])
        return {"created": len(args["links"])}

    def reindex(self, db):
        return {"entities_processed": self.created, "models_used": [], "errors_skipped": 0}


def _archive(tmp_path, n_entities, n_edges):
    entities = [
        Entity(
            uri=f"u:{i}",
            entity_kind="org",
            entity_type="company",
            name=f"Company {i}",
            description="x" * 400,
            properties={"uri": f"u:{i}"},
        )
        for i in range(n_entities)
    ]
    edges = [
        Edge(
            source=f"u:{i % n_entities}",
            target=f"u:{(i + 1) % n_entities}",
            relation="part_of",
            metadata={"predicate": "subsidiary", "accession": f"0000000000-26-{i:06d}", "note": "y" * 60},
        )
        for i in range(n_edges)
    ]
    path = tmp_path / "gen.json"
    path.write_text(Archive(generation={}, entities=entities, edges=edges).model_dump_json())
    return path


def test_slices_cut_by_bytes_and_by_count():
    rows = [{"k": "v" * 900} for _ in range(300)]
    slices = build._slices(rows, 1000)
    assert [r for s in slices for r in s] == rows
    assert len(slices) > 1
    assert all(len(json.dumps(s).encode()) < build.ARGV_CAP + 2 * len(s) for s in slices)
    assert max(len(s) for s in build._slices([{"k": 1}] * 50, 7)) == 7


def test_load_keeps_every_request_under_the_linux_argument_limit(tmp_path):
    archive_path = _archive(tmp_path, n_entities=400, n_edges=2500)
    config = tmp_path / "gen.toml"
    config.write_text(
        '[[backends]]\nname = "main"\nkind = "sqlite"\npath = "' + str(tmp_path / "s.db") + '"\n'
    )
    khive = FakeKhive(config)
    ids = build.load(khive, archive_path, ids_out=tmp_path / "gen.ids.json")
    assert len(ids) == 400
    assert khive.created == 400
    assert khive.linked == 2500
    verbs = {verb for verb, _ in khive.sizes}
    assert verbs == {"create", "link"}
    assert max(size for _, size in khive.sizes) < LINUX_ARG_LIMIT
