"""Run records: what a notebook's cells are doing right now, whoever runs them (the console's
editor, Claude through mlops-notebooks-mcp), kept beside the workspace so any page that opens
the notebook later sees it:

    gs://<bucket>/workspace/.runs/<path>/<run_id>.json

One object per run, written only by the runner that owns it: so runners never contend, and the
notebook's own generation (which the editor's autosave saves over) never moves while a cell
runs. The runner writes it when a cell starts and ends, and every HEARTBEAT seconds in between,
with the running cell's outputs so far.

A record alone can't tell a silent cell from a dead runner, nor a busy kernel from a dead one:
`liveness` puts it next to the runtime's state and the kernel's own execution_state, so each
case is named instead of guessed:

    running          the runner's heartbeat is fresh
    runner_gone      no heartbeat, the kernel still busy: the cell runs on, its outputs uncaptured
    lost             no heartbeat, the kernel idle: the run ended without its result saved
    kernel_dead      the kernel died (out of memory, killed)
    kernel_gone      the kernel was shut down or restarted away
    runtime_stopped  the runtime is not RUNNING (idle shutdown, stopped, deleted)
    stale            no heartbeat, and the kernel's state couldn't be read (no notebook access)
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

from gcpbricks.console.notebooks import _bucket_prefix, _clean

#: Seconds between a runner's writes while a cell runs, with or without output.
HEARTBEAT = 10
#: Seconds without a heartbeat after which the runner is taken to be gone.
STALE = 4 * HEARTBEAT
#: Seconds a finished run's record is kept: the editor shows how the last run ended.
KEEP = 24 * 3600
#: Bytes of a running cell's outputs a heartbeat carries; the notebook gets them all at the end.
OUTPUT_BYTES = 64_000


def _now() -> float:
    return time.time()


def _prefix(path: str) -> str:
    _, base = _bucket_prefix()
    return f"{base}.runs/{_clean(path)}/"


def merge(outputs: list[dict], m: dict) -> list[dict]:
    """A cell's outputs with message `m` taken in, as Jupyter does: a stream continues the
    stream before it of the same name; clear_output empties them."""
    if m["output_type"] == "clear_output":
        return []
    last = outputs[-1] if outputs else None
    if m["output_type"] == "stream" and last and last["output_type"] == "stream" and last["name"] == m["name"]:
        return [*outputs[:-1], {**last, "text": _text(last["text"]) + m["text"]}]
    return [*outputs, m]


def _text(v) -> str:
    return "".join(v) if isinstance(v, list) else (v or "")


def cap_outputs(outputs: list[dict], limit: int = OUTPUT_BYTES) -> list[dict]:
    """The latest outputs that fit in `limit` bytes of JSON: whole outputs from the end, and the
    tail of a stream that doesn't fit whole."""
    kept, size = [], 0
    for o in reversed(outputs):
        n = len(json.dumps(o))
        if size + n <= limit:
            kept.append(o)
            size += n
            continue
        if o.get("output_type") == "stream" and limit - size > 200:
            kept.append({**o, "text": "…" + _text(o.get("text"))[-(limit - size - 100):]})
        break
    return kept[::-1]


class RunRecord:
    """A run of some of a notebook's cells, by one runner. `cells` are (index, id) in run order."""

    def __init__(self, gcp, user: str, path: str, by: str, runtime: str, kernel: str,
                 cells: list[tuple[int, str]], clock=_now):
        self.gcp, self.clock = gcp, clock
        self.path = _clean(path)
        self.run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.body = {"run_id": self.run_id, "path": self.path, "by": by, "user": user, "runtime": runtime,
                     "kernel": kernel, "state": "running", "error": None, "started": clock(), "ended": None,
                     "heartbeat": clock(),
                     "cells": [{"index": i, "id": cid, "state": "queued", "started": None, "ended": None,
                                "execution_count": None, "outputs": []} for i, cid in cells]}
        self._last_write = 0.0
        self._write()

    def _write(self) -> None:
        bucket, _ = _bucket_prefix()
        self.body["heartbeat"] = self.clock()
        self.gcp.write_object(bucket, f"{_prefix(self.path)}{self.run_id}.json", json.dumps(self.body).encode(),
                              "application/json")
        self._last_write = self.clock()

    def beat(self) -> None:
        """Alive, with nothing new: while the run waits for its kernel."""
        self._write()

    def _cell(self, index: int) -> dict:
        return next(c for c in self.body["cells"] if c["index"] == index)

    def start_cell(self, index: int) -> None:
        self._cell(index).update(state="running", started=self.clock())
        self._write()

    def progress(self, index: int, outputs: list[dict], force: bool = False) -> None:
        """The running cell's outputs so far; written at most every HEARTBEAT seconds."""
        self._cell(index)["outputs"] = cap_outputs(outputs)
        if force or self.clock() - self._last_write >= HEARTBEAT:
            self._write()

    def end_cell(self, index: int, status: str, execution_count, outputs: list[dict]) -> None:
        self._cell(index).update(state=status, ended=self.clock(), execution_count=execution_count,
                                 outputs=cap_outputs(outputs))
        self._write()

    def finish(self, state: str = "done", error: str | None = None) -> None:
        """done, failed (a cell errored, or the run broke: `error` says how) or cancelled."""
        for c in self.body["cells"]:
            if c["state"] in ("queued", "running"):
                c["state"] = "cancelled" if c["state"] == "queued" else state
        self.body.update(state=state, error=error, ended=self.clock())
        self._write()


def list_runs(gcp, path: str, now: float | None = None) -> list[dict]:
    """The notebook's run records, newest first; finished ones older than KEEP are deleted."""
    bucket, _ = _bucket_prefix()
    now = now or _now()
    _, items = gcp.list_dir(bucket, _prefix(path))
    runs = []
    for it in items:
        got = gcp.read_object(bucket, it["name"])
        if not got:
            continue
        r = json.loads(got[0])
        if r.get("state") != "running" and now - (r.get("ended") or r.get("heartbeat") or 0) > KEEP:
            gcp.delete_object(bucket, it["name"])
            continue
        runs.append(r)
    return sorted(runs, key=lambda r: r.get("started") or 0, reverse=True)


def liveness(run: dict, runtime_state: str | None, kernel: dict | None, now: float | None = None) -> str:
    """How a run stands, from its record, the runtime's state and the kernel's status (None:
    the kernel is not on the runtime). A finished run is its own state."""
    if run.get("state") != "running":
        return run.get("state") or "done"
    if runtime_state is not None and runtime_state != "RUNNING":
        return "runtime_stopped"
    if kernel is None:
        return "kernel_gone"
    if kernel.get("execution_state") == "dead":
        return "kernel_dead"
    if (now or _now()) - (run.get("heartbeat") or 0) <= STALE:
        return "running"
    return {"busy": "runner_gone", "idle": "lost"}.get(kernel.get("execution_state") or "", "stale")


def explain(state: str, runtime: dict | None = None, idle: str | None = None) -> str:
    """One sentence a person (or Claude) can act on, for a liveness state."""
    name = (runtime or {}).get("name", "the runtime")
    return {
        "running": "running",
        "runner_gone": (f"the cell still runs on {name}'s kernel, but whoever ran it (a closed tab, a stopped "
                        "Claude session) is gone: outputs from now on are not captured. Interrupt it or let it finish."),
        "lost": "the kernel is idle but the run's result was never saved: it ended while its runner was gone",
        "kernel_dead": f"{name}'s kernel died during the run: usually out of memory. Restart the kernel",
        "kernel_gone": f"the kernel the run used is no longer on {name}: it was shut down or restarted",
        "stale": "no word from the run's runner, and the kernel's state couldn't be read: allow notebook access to see it",
        "runtime_stopped": (f"{name} is {(runtime or {}).get('state', 'not running')}"
                            + (f": Colab stops a runtime after {idle} with no kernel activity and no notebook "
                               "connected, and a detached process does not count" if idle else "")
                            + ". Start it again: variables are lost, its disk is kept"),
    }.get(state, state)


def explain_loss(runtime: dict | None, kernel: dict | None) -> str:
    """Why a running cell's connection closed, from what the runtime and the kernel say now
    (`kernel` None: not on the runtime; {"execution_state": "unknown"}: couldn't be read)."""
    state = liveness({"state": "running", "heartbeat": 0}, (runtime or {}).get("state", "DELETED"), kernel)
    if state == "runner_gone":
        return ("the connection to the kernel dropped but the cell still runs there: its outputs from now on "
                "are not captured; interrupt the kernel to stop it")
    if state == "lost":
        return "the connection to the kernel dropped and the kernel is idle again: the cell's result was lost"
    return explain(state, runtime, (runtime or {}).get("idle"))


def with_liveness(runs: list[dict], runtime_for, kernel_for, now: float | None = None) -> list[dict]:
    """Each running record with `live` (a liveness state) and `why`; `runtime_for(id)` the
    runtime dict or None, `kernel_for(runtime_id, kernel_id)` the kernel's status, None when
    the kernel is not on the runtime; raising, the kernel's state is unknown. Each runtime and
    kernel is asked once."""
    rts, ks = {}, {}
    out = []
    for r in runs:
        if r.get("state") != "running":
            out.append({**r, "live": r.get("state"), "why": r.get("error")})
            continue
        rid, kid = r.get("runtime"), r.get("kernel")
        if rid not in rts:
            rts[rid] = runtime_for(rid)
        rt = rts[rid]
        state = rt["state"] if rt else "DELETED"
        k = None
        if state == "RUNNING":
            if (rid, kid) not in ks:
                try:
                    ks[(rid, kid)] = kernel_for(rid, kid)
                except Exception:  # noqa: BLE001 - no notebook access, the proxy refused: not known
                    ks[(rid, kid)] = {"execution_state": "unknown"}
            k = ks[(rid, kid)]
        live = liveness(r, state, k, now)
        out.append({**r, "live": live, "why": None if live == "running" else explain(live, rt, (rt or {}).get("idle")),
                    "kernel_state": (k or {}).get("execution_state"), "kernel_activity": (k or {}).get("last_activity")})
    return out
