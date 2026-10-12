"""A small client that calls the stock khive binary's CLI (`kkernel exec`), one warm daemon per
store config."""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import time
from pathlib import Path
from typing import Any

BINARY = Path(os.environ.get("FILINGS_GRAPH_KKERNEL", Path.home() / ".cargo/bin/kkernel"))
PACKS = "kg,memory"
MAX_OPS = 100  # the binary refuses a longer batch
BUSY = ("embedding worker queue is full", "storage_admission_timeout")
BUSY_WAITS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0)


class Khive:
    def __init__(
        self,
        config: str | Path,
        *,
        binary: str | Path = BINARY,
        namespace: str = "local",
        actor: str = "filings-graph",
        timeout: float = 600,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        config = Path(config).resolve(strict=True)
        self.command = [
            str(Path(binary).resolve(strict=True)),
            "exec",
            "--config",
            str(config),
            "--presentation",
            "verbose",
            "--output-format",
            "json",
            "--strict",
            "--actor",
            actor,
            "--expect-actor",
            actor,
        ]
        self.namespace = namespace
        self.timeout = timeout
        # a plain string prefix, never a Path whose suffix is replaced: `with_suffix` on
        # `khived-filings-graph-v0.1` swaps the `.1` for `.sock` and lands on filings-graph-v0's daemon, which then
        # refuses the mismatched config (the strict mode keeps it from loading the wrong store)
        self.rendezvous = str(config.with_name(f"khived-{config.stem}"))

    def env(self) -> dict[str, str]:
        return os.environ | {
            "KHIVE_PACKS": PACKS,
            # One warm daemon per config; the rendezvous files sit beside the config so this
            # store never collides with another khive on the machine. A mismatch is an error,
            # never a silent in-process load of the store.
            "KHIVE_SOCKET": self.rendezvous + ".sock",
            "KHIVE_PID": self.rendezvous + ".pid",
            "KHIVE_LOCK": self.rendezvous + ".lock",
            "KHIVE_DAEMON_STRICT": "1",
            # The marker is read from one global path by default (~/.khive/khived.supervisor),
            # not from the socket it claims, so a launchd-supervised khive daemon on the same
            # machine makes the spawn of THIS private daemon refuse ("supervised_daemon_socket_absent").
            # The marker for this rendezvous lives beside it; absent means no supervisor.
            "KHIVE_SUPERVISOR_MARKER": self.rendezvous + ".supervisor",
            "KHIVE_REQUEST_READ_TIMEOUT_SECS": str(int(self.timeout)),
        }

    def _run(self, ops_json: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.command, ops_json],
            env=self.env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=self.timeout,
            check=False,
        )

    def reindex(self, db: str | Path) -> dict:
        """Embed every entity and note of the store for the configured engines. The bulk `create`
        path writes no vectors (search is text-only until this runs), so a load ends here.
        `--db` must name the declared backend explicitly; `--no-knowledge` skips the corpus pass."""
        cmd = [
            self.command[0],
            "reindex",
            "--config",
            self.command[3],
            "--db",
            str(Path(db).resolve(strict=True)),
            "--no-knowledge",
        ]
        process = subprocess.run(
            cmd,
            env=self.env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=self.timeout,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"kkernel reindex exit {process.returncode}: {process.stderr.strip()[-800:]}")
        last = [ln for ln in process.stdout.splitlines() if ln.startswith("{")]
        return json.loads(last[-1]) if last else {}

    def call(self, verb: str, **args: Any) -> Any:
        return self.batch([(verb, args)])[0]

    def batch(self, ops: list[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Several verbs in one request, answers in order; a longer list goes as consecutive
        requests of MAX_OPS."""
        if len(ops) > MAX_OPS:
            return [r for i in range(0, len(ops), MAX_OPS) for r in self.batch(ops[i : i + MAX_OPS])]
        payload = [
            {
                "tool": verb,
                "args": {"namespace": self.namespace, **{k: v for k, v in args.items() if v is not None}},
            }
            for verb, args in ops
        ]
        ops_json = json.dumps(payload, allow_nan=False)
        for wait in (*BUSY_WAITS, None):
            process = self._run(ops_json)
            if process.returncode == 0 or wait is None or not _all_busy(process.stdout, len(ops)):
                break
            time.sleep(wait * (1 + random.random()))
        if process.returncode != 0:
            try:
                failed = [
                    f"{r['tool']}#{i}: {r.get('error')}"
                    for i, r in enumerate(json.loads(process.stdout)["results"])
                    if r.get("ok") is not True
                ]
                detail = f"{len(failed)}/{len(ops)} ops failed: " + " | ".join(failed[:6])
            except (ValueError, KeyError, TypeError):
                detail = process.stdout.strip()[-2000:]
            raise RuntimeError(
                f"kkernel exit {process.returncode}: {detail} | {process.stderr.strip()[-600:]}"
            )
        envelope = json.loads(process.stdout)
        results = envelope["results"]
        if envelope["status"] != "success" or len(results) != len(ops):
            raise RuntimeError(f"Unexpected khive response to {[verb for verb, _ in ops]}")
        out = []
        for (verb, _), result in zip(ops, results, strict=True):
            if result["tool"] != verb:
                raise RuntimeError(f"Unexpected khive response to {verb}")
            if result["ok"] is not True:
                raise RuntimeError(f"{verb}: {result['error']}")
            out.append(result["result"])
        return out


def _all_busy(stdout: str, n_ops: int) -> bool:
    try:
        results = json.loads(stdout)["results"]
    except (ValueError, KeyError, TypeError):
        return False
    return len(results) == n_ops and all(
        r.get("ok") is not True and any(b in str(r.get("error")) for b in BUSY) for r in results
    )
