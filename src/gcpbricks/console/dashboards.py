"""The console's Dashboards: every bundle's dashboards (gcpbricks.dashboards), found as
the specs the bundle module uploads beside its job definitions, and each widget's data, read
from the bundle's own runs, its logs and alerts, or the BigQuery its datasets name."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import requests

from gcpbricks.console import bundles as BN
from gcpbricks.console import ml as M
from gcpbricks.console import service as S
from gcpbricks.console.catalog import _bq_error, _field, _rows
from gcpbricks.dashboards import SOURCES, DashboardSpec, aggregate, narrow, window_delta

#: How far back a source reads a job's runs, newest first.
RUNS_PER_JOB = 100
#: How many executions of each job cluster the memory source reads (two metric reads each,
#: once per finished execution).
MEMORY_EXECUTIONS = 20
#: A dataset's query is refused by BigQuery past this; dashboards read prepared tables.
MAX_DATASET_BYTES = 2 * 1024 ** 3
LOG_LINES = 200
TTL = 120
_CACHE: dict[tuple, tuple[float, dict]] = {}


def _jobs(gcp) -> list[dict]:
    return [j for j in map(S._job_of, gcp.workflows()) if j]


def _summary(bucket: str, o: dict, spec: dict) -> dict:
    return {"bucket": bucket, "object": o["name"], "key": spec["key"], "title": spec["title"],
            "bundle": spec["bundle"], "target": spec["target"], "widgets": len(spec["widgets"]),
            "updated": o.get("updated", "")}


def list_dashboards(gcp) -> list[dict]:
    """Every deployed bundle's dashboards, listed under its own folder (BN.folders)."""
    folders = list(BN.folders(gcp).values())
    with ThreadPoolExecutor(8) as pool:
        listed = pool.map(lambda f: [(f[0], o) for o in gcp.list_objects(f[0], f"{f[1]}dashboards/")
                                     if o["name"].endswith(".json")], folders)
        objs = [x for xs in listed for x in xs]
        specs = list(pool.map(lambda bo: _spec_json(gcp, bo[0], bo[1]["name"]), objs))
    out = [_summary(bucket, o, spec) for (bucket, o), spec in zip(objs, specs)]
    return sorted(out, key=lambda d: (d["bundle"], d["target"], d["key"]))


def _spec_json(gcp, bucket: str, obj: str) -> dict | None:
    """A dashboard's spec, kept TTL seconds (every widget of a page reads it)."""
    key = ("spec", bucket, obj)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    spec = gcp.read_json_or_none(bucket, obj)
    _CACHE[key] = (time.monotonic(), spec)
    return spec


def dashboard(gcp, bundle: str, target: str, key: str) -> dict:
    where = BN.folders(gcp).get((bundle, target))
    obj = f"{where[1]}dashboards/{key}.json" if where else None
    spec = _spec_json(gcp, where[0], obj) if where else None
    if not spec:
        raise KeyError(f"no dashboard {key!r} in {bundle} @ {target}")
    return {**_summary(where[0], {"name": obj}, spec), "spec": spec}


# --- sources --------------------------------------------------------------------------

def _minutes(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60


def _runs(gcp, bundle: str, target: str) -> list[tuple[dict, list[dict]]]:
    """(job, its runs) for every job of the bundle on the target."""
    out = []
    for j in _jobs(gcp):
        if (j["bundle"], j["target"]) == (bundle, target):
            out.append((j, [S._run_summary(e) for e in gcp.executions(j["workflow"], RUNS_PER_JOB)]))
    return out


def _mb(quantity: str | None) -> float | None:
    """A Kubernetes memory quantity ("16Gi", "512Mi") in MB (MiB)."""
    if not quantity:
        return None
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 ** 2}
    for unit, factor in units.items():
        if quantity.endswith(unit):
            return float(quantity[: -len(unit)]) * factor
    return float(quantity) / 1024 ** 2


def _memory_rows(gcp, bundle: str, target: str) -> list[dict]:
    """Each recent execution of the bundle's Cloud Run jobs on the target (labelled by the
    module), with its peak memory (service.memory)."""
    rows = []
    for job in gcp.run_jobs():
        labels = job.get("labels") or {}
        if (labels.get("mlops_bundle"), labels.get("mlops_target")) != (bundle, target):
            continue
        name = job["name"].rsplit("/", 1)[-1]
        for ex in gcp.run_executions(name, MEMORY_EXECUTIONS):
            mem = S.memory(gcp, ex["name"], ex)
            done = "completionTime" in ex
            state = ("RUNNING" if not done else "SUCCEEDED" if ex.get("succeededCount", 0) >= ex.get("taskCount", 1)
                     else "FAILED")
            rows.append({"run_job": name, "state": state, "start": ex.get("startTime"),
                         "peak_mb": mem["peak_bytes"] / 1024 ** 2 if mem["peak_bytes"] is not None else None,
                         "limit_mb": _mb(mem["limit"]), "peak_fraction": mem["peak_fraction"]})
    return rows


def _metric_rows(gcp, bundle: str, target: str) -> list[dict]:
    """One row per metric of each run of the experiments the bundle's deploy recorded."""
    record = BN.deployment(gcp, bundle, target)
    names = set((record or {}).get("resources", {}).get("experiments", {}).values())
    rows = []
    for e in M.experiments(gcp):
        if e["name"] not in names:
            continue
        for r in M.experiment(gcp, e["id"])["runs"]:
            rows += [{"experiment": e["name"], "run": r["name"], "state": r["state"], "start": r["created"],
                      "metric": k, "value": v} for k, v in r["metrics"].items()]
    return rows


def _source_rows(gcp, source: str, bundle: str, target: str) -> list[dict]:
    if source == "memory":
        return _memory_rows(gcp, bundle, target)
    if source == "model_metrics":
        return _metric_rows(gcp, bundle, target)
    rows = []
    for job, runs in _runs(gcp, bundle, target):
        if source == "runs":
            rows += [{"job": job["job"], "state": "SKIPPED" if r["skipped"] else r["state"],
                      "start": r["start"], "end": r["end"], "duration_min": _minutes(r["start"], r["end"])}
                     for r in runs]
            continue
        secs = S.task_seconds(gcp, job["workflow"], runs)
        for r in runs:
            for task, status in r["task_states"].items():
                s = secs.get(r["id"], {}).get(task)
                rows.append({"job": job["job"], "task": task, "status": status, "start": r["start"],
                             "duration_min": s / 60 if s is not None else None})
    return rows


def _dataset_rows(gcp, sql: str, start: datetime, end: datetime, filters: dict[str, str]) -> list[dict]:
    """The dataset's rows: `@start` / `@end` are the window, `@<filter>` the page's pick ('' when none)."""
    params = [{"name": n, "parameterType": {"type": "TIMESTAMP"}, "parameterValue": {"value": t.isoformat()}}
              for n, t in (("start", start), ("end", end)) if f"@{n}" in sql]
    params += [{"name": n, "parameterType": {"type": "STRING"}, "parameterValue": {"value": v}}
               for n, v in filters.items() if f"@{n}" in sql]
    try:
        r = gcp.bq_query({"query": sql, "useLegacySql": False, "parameterMode": "NAMED",
                          "queryParameters": params, "maximumBytesBilled": str(MAX_DATASET_BYTES),
                          "timeoutMs": 20000, "formatOptions": {"useInt64Timestamp": True}})
    except requests.HTTPError as e:
        raise ValueError(f"the dataset's query failed: {_bq_error(e)}") from None
    schema = [_field(f) for f in r["schema"]["fields"]]
    names = [f["name"] for f in schema]
    return [dict(zip(names, row)) for row in _rows(schema, r.get("rows", []))]


def _log_lines(gcp, query: str, start: datetime) -> list[dict]:
    flt = f'({query}) AND timestamp>="{start.isoformat()}"'
    out = []
    for e in gcp.log_entries(flt, LOG_LINES, newest_first=True):
        p = e.get("jsonPayload") or {}
        out.append({"time": e.get("timestamp"), "severity": e.get("severity", "DEFAULT"),
                    "text": e.get("textPayload") or p.get("message") or str(p or e.get("protoPayload", ""))})
    return out


def _alerts(gcp, bundle: str, target: str) -> list[dict]:
    """Open alerts of the bundle's own policies on the target (the module names them
    "<bundle> <key> (<target>)")."""
    out = []
    for a in gcp.open_alerts():
        name = (a.get("policy") or {}).get("displayName", "")
        if name.startswith(f"{bundle} ") and name.endswith(f"({target})"):
            out.append({"policy": name, "opened": a.get("openTime"), "name": a.get("name", ""),
                        "severity": (a.get("policy") or {}).get("severity", "")})
    return out


def _spec(gcp, bundle: str, target: str, key: str) -> DashboardSpec:
    d = dashboard(gcp, bundle, target, key)
    return DashboardSpec.model_validate({k: v for k, v in d["spec"].items()
                                         if k in ("window", "datasets", "filters", "widgets")})


def _cached(key: tuple, read):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    out = read()
    _CACHE[key] = (time.monotonic(), out)
    return out


def _read(gcp, spec: DashboardSpec, bundle: str, target: str, source: str | None, dataset: str | None,
          start: datetime, end: datetime, picked: dict[str, str]) -> tuple[list[dict], str | None]:
    """A source's or dataset's rows, and the field the window reads."""
    if source:
        return _source_rows(gcp, source, bundle, target), SOURCES[source]["time"]
    ds = spec.datasets[dataset]
    return _dataset_rows(gcp, ds.bigquery, start, end, {n: picked.get(n, "") for n in spec.filters}), ds.time


def widget(gcp, bundle: str, target: str, key: str, index: int, window: str | None = None,
           picked: dict[str, str] | None = None) -> dict:
    """One widget's data over `window` (else the widget's, else the page's), narrowed by the
    page's picked filter values ({filter name: value})."""
    spec = _spec(gcp, bundle, target, key)
    if not 0 <= index < len(spec.widgets):
        raise KeyError(f"{key} has no widget {index}")
    w = spec.widgets[index]
    win = window or w.window or spec.window
    end = datetime.now(UTC)
    start = end - window_delta(win)
    picked = {n: v for n, v in (picked or {}).items() if v}
    unknown = sorted(set(picked) - set(spec.filters))
    if unknown:
        raise ValueError(f"{key} has no filter {unknown}; it has {sorted(spec.filters)}")

    def read() -> dict:
        out: dict = {"type": w.type, "window": win}
        if w.type == "text":
            return out | {"text": w.text}
        if w.type == "logs":
            return out | {"lines": _log_lines(gcp, w.query, start)}
        if w.type == "alerts":
            return out | {"alerts": _alerts(gcp, bundle, target)}
        rows, time_field = _read(gcp, spec, bundle, target, w.source, w.dataset, start, end, picked)
        fields = SOURCES[w.source]["fields"] if w.source else set(rows[0]) if rows else set()
        by_field = {spec.filters[n].field: v for n, v in picked.items()}
        return out | aggregate(rows, narrow(w, by_field, fields), time_field, start if time_field else None)
    return _cached((bundle, target, key, index, win, tuple(sorted(picked.items()))), read)


def filter_values(gcp, bundle: str, target: str, key: str, name: str, window: str | None = None) -> list[str]:
    """A page filter's choices: its field's distinct values in its source or dataset over the window."""
    spec = _spec(gcp, bundle, target, key)
    if name not in spec.filters:
        raise KeyError(f"{key} has no filter {name!r}")
    f = spec.filters[name]
    win = window or spec.window
    end = datetime.now(UTC)
    start = end - window_delta(win)

    def read() -> list[str]:
        rows, _ = _read(gcp, spec, bundle, target, f.source, f.dataset, start, end, {})
        return sorted({str(r[f.field]) for r in rows if r.get(f.field) not in (None, "")})
    return _cached((bundle, target, key, "filter", name, win), read)
