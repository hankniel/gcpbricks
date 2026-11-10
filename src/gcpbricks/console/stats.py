"""A task run's metrics, as Databricks' Spark UI and query profile show a run's: what each of
its executions used (CPU, memory, network, vCPU- and GiB-seconds), what that cost at list
price, the BigQuery jobs it ran (bytes billed, slot time, each one's stage plan), and what
stands out.

  dbt_task, spark_python_task, python_wheel_task   its Cloud Run executions
  vertex_pipeline_task                             its pipeline's steps' Vertex AI custom jobs
  batch_serve_task                                 each served model's Cloud Run execution

BigQuery jobs are found by their labels: a dbt command's dbt_invocation_id (from the
run_results mlops-task keeps, task_runner.save_run_results), or the platform clients'
mlops_exec (job_labels): a project's own client is counted once it uses
job_labels.bigquery_client().
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import requests

from gcpbricks import files
from gcpbricks.console import service as S
from gcpbricks.job_labels import LABEL

#: List prices: BigQuery on-demand (US), Cloud Run jobs (us-central1, tier 1).
BQ_USD_PER_TIB = 6.25
RUN_USD_PER_VCPU_S = 0.000018
RUN_USD_PER_GIB_S = 0.000002
TIB, GIB = 2 ** 40, 2 ** 30
#: Monitoring writes a minute's samples a few minutes after it ends.
METRICS_LAG = timedelta(minutes=4)
_MEMORY_UNITS = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}

#: Finished task runs' metrics, read once per console instance.
_DONE: dict[tuple[str, str, str], dict] = {}


def cpus(limit: str | None) -> float | None:
    """A Kubernetes CPU quantity ("1", "1000m") in vCPUs."""
    if not limit:
        return None
    return float(limit[:-1]) / 1000 if limit.endswith("m") else float(limit)


def memory_bytes(limit: str | None) -> float | None:
    """A Kubernetes memory quantity ("512Mi", "2Gi", "1G") in bytes."""
    if not limit:
        return None
    m = re.fullmatch(r"([0-9.]+)([KMGT]i?|k)?", limit)
    return float(m[1]) * _MEMORY_UNITS.get(m[2] or "", 1) if m else None


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _window(start: str, end: str | None) -> tuple[str, str]:
    now = datetime.now(UTC)
    stop = min(_parse(end) + METRICS_LAG, now) if end else now
    return _iso(_parse(start) - timedelta(minutes=1)), _iso(stop)


def _value(p: dict) -> float | None:
    v = p["value"]
    x = v.get("doubleValue", v.get("int64Value"))
    return float(x) if x is not None else None


def _points(gcp, flt: str, start: str, end: str, aligner: str) -> list[tuple[str, float]]:
    """(minute, value) of every series matching flt, the largest per minute, in time order."""
    by: dict[str, float] = {}
    for ts in gcp.time_series(flt, start, end, aligner):
        for p in ts.get("points", []):
            v = _value(p)
            if v is not None:
                t = p["interval"]["endTime"][:19] + "Z"
                by[t] = max(v, by.get(t, v))
    return sorted(by.items())


def _series(points: dict[str, list[tuple[str, float]]]) -> dict:
    """{t: [minute...], <name>: [value or None per minute]}: one time axis for a chart."""
    ts = sorted({t for pts in points.values() for t, _ in pts})
    return {"t": ts, **{k: [dict(pts).get(t) for t in ts] for k, pts in points.items()}}


def _peak(pts: list[tuple[str, float]]) -> float | None:
    return max((v for _, v in pts), default=None)


def _mean(pts: list[tuple[str, float]]) -> float | None:
    return sum(v for _, v in pts) / len(pts) if pts else None


# --- compute ------------------------------------------------------------------------------

def cloud_run(gcp, name: str, execution: dict, label: str) -> dict:
    """A Cloud Run execution's use against its limits, and its cost. The job's metrics carry no
    execution label, so they are the job's within the execution's window (a job runs one
    execution at a time)."""
    res = (((execution.get("template") or {}).get("containers") or [{}])[0].get("resources") or {}).get("limits", {})
    start, end = execution.get("startTime"), execution.get("completionTime")
    tasks = execution.get("taskCount", 1)
    unit = {"label": label, "kind": "cloud_run", "name": name, "id": name.rsplit("/", 1)[-1],
            "start": start, "end": end, "service_account": (execution.get("template") or {}).get("serviceAccount"),
            "machine": {"cpu": cpus(res.get("cpu")), "memory_bytes": memory_bytes(res.get("memory")), "tasks": tasks},
            "usage": {}, "series": {"t": []}, "cost_usd": None,
            "link": f"https://console.cloud.google.com/run/jobs/executions/details/{gcp.region}/{name.rsplit('/', 1)[-1]}?project={gcp.project}"}
    if not start:
        return unit
    a, b = _window(start, end)
    flt = f'resource.type="cloud_run_job" AND resource.labels.job_name="{name.split("/")[5]}" AND metric.type="run.googleapis.com/container/'
    with ThreadPoolExecutor(6) as pool:
        got = dict(zip(["cpu", "memory", "memory_bytes", "vcpu_s", "gib_s", "net_in", "net_out"], pool.map(
            lambda m: _points(gcp, f'{flt}{m[0]}"', a, b, m[1]),
            [("cpu/utilizations", "ALIGN_PERCENTILE_99"), ("memory/utilizations", "ALIGN_PERCENTILE_99"),
             ("memory/usage", "ALIGN_PERCENTILE_99"), ("cpu/allocation_time", "ALIGN_SUM"),
             ("memory/allocation_time", "ALIGN_SUM"), ("network/received_bytes_count", "ALIGN_SUM"),
             ("network/sent_bytes_count", "ALIGN_SUM")])))
    vcpu_s, gib_s = sum(v for _, v in got["vcpu_s"]), sum(v for _, v in got["gib_s"])
    estimated = not got["vcpu_s"]
    if estimated and end:
        # No allocation samples (under a minute, or not written yet): the limits for the duration.
        secs = (_parse(end) - _parse(start)).total_seconds() * tasks
        vcpu_s = (unit["machine"]["cpu"] or 0) * secs
        gib_s = (unit["machine"]["memory_bytes"] or 0) / GIB * secs
    unit["usage"] = {"cpu_mean": _mean(got["cpu"]), "cpu_peak": _peak(got["cpu"]),
                     "memory_peak": _peak(got["memory"]), "memory_peak_bytes": _peak(got["memory_bytes"]),
                     "net_in_bytes": sum(v for _, v in got["net_in"]), "net_out_bytes": sum(v for _, v in got["net_out"]),
                     "vcpu_seconds": vcpu_s, "gib_seconds": gib_s, "estimated": estimated}
    unit["series"] = _series({k: got[k] for k in ("cpu", "memory", "net_in", "net_out")})
    unit["cost_usd"] = vcpu_s * RUN_USD_PER_VCPU_S + gib_s * RUN_USD_PER_GIB_S if (vcpu_s or gib_s) else None
    return unit


def custom_job(gcp, name: str, step: str) -> dict:
    """A pipeline step's Vertex AI custom job: its machines and what they used. No cost: Vertex
    training bills by machine type, which the console has no price list for."""
    j = gcp.ai_get(name)
    pool = (j.get("jobSpec") or {}).get("workerPoolSpecs", [{}])[0]
    ms = pool.get("machineSpec") or {}
    start, end = j.get("startTime"), j.get("endTime")
    job_id = name.rsplit("/", 1)[-1]
    # created -> start is the wait for machines (Vertex provisioning), not billed or used.
    unit = {"label": step, "kind": "custom_job", "name": name, "id": job_id, "created": j.get("createTime"),
            "start": start, "end": end,
            "state": j.get("state", "").removeprefix("JOB_STATE_"),
            "service_account": (j.get("jobSpec") or {}).get("serviceAccount"),
            "machine": {"machine_type": ms.get("machineType"), "accelerator": ms.get("acceleratorType"),
                        "accelerators": ms.get("acceleratorCount"), "replicas": int(pool.get("replicaCount") or 1)},
            "usage": {}, "series": {"t": []}, "cost_usd": None,
            "link": f"https://console.cloud.google.com/vertex-ai/locations/{gcp.region}/training/{job_id}/cpu?project={gcp.project}"}
    if not start:
        return unit
    a, b = _window(start, end)
    flt = f'resource.type="cloudml_job" AND resource.labels.job_id="{job_id}" AND metric.type="ml.googleapis.com/training/'
    with ThreadPoolExecutor(6) as p:
        got = dict(zip(["cpu", "memory", "accelerator", "accelerator_memory", "net_in", "net_out"], p.map(
            lambda m: _points(gcp, f'{flt}{m[0]}"', a, b, m[1]),
            [("cpu/utilization", "ALIGN_MAX"), ("memory/utilization", "ALIGN_MAX"),
             ("accelerator/utilization", "ALIGN_MAX"), ("accelerator/memory/utilization", "ALIGN_MAX"),
             ("network/received_bytes_count", "ALIGN_DELTA"), ("network/sent_bytes_count", "ALIGN_DELTA")])))
    if end:
        unit["usage"]["machine_hours"] = (_parse(end) - _parse(start)).total_seconds() / 3600 * unit["machine"]["replicas"]
    unit["usage"] |= {"cpu_mean": _mean(got["cpu"]), "cpu_peak": _peak(got["cpu"]), "memory_peak": _peak(got["memory"]),
                      "accelerator_mean": _mean(got["accelerator"]), "accelerator_memory_peak": _peak(got["accelerator_memory"]),
                      "net_in_bytes": sum(v for _, v in got["net_in"]), "net_out_bytes": sum(v for _, v in got["net_out"])}
    unit["series"] = _series({k: got[k] for k in ("cpu", "memory", "accelerator", "net_in", "net_out") if got[k] or k in ("cpu", "memory")})
    return unit


# --- BigQuery -----------------------------------------------------------------------------

def _bq_cost(billed: int) -> float:
    return billed / TIB * BQ_USD_PER_TIB


def _dbt_node(sql: str | None) -> str | None:
    """The node_id in dbt's query comment, which every statement a node runs carries."""
    m = re.search(r'/\*\s*(\{[^{}]*"node_id"[^{}]*\})\s*\*/', sql or "")
    try:
        return json.loads(m[1]).get("node_id") if m else None
    except ValueError:
        return None


def _bq_row(j: dict) -> dict:
    st = j.get("statistics") or {}
    q = st.get("query") or {}
    conf = j.get("configuration") or {}
    ref = j["jobReference"]
    billed = int(q.get("totalBytesBilled") or 0)
    dest = (conf.get("query") or {}).get("destinationTable") or (conf.get("load") or {}).get("destinationTable") or {}
    return {"job_id": ref["jobId"], "project": ref["projectId"], "location": ref.get("location"),
            "type": conf.get("jobType"), "statement": q.get("statementType"),
            "state": (j.get("status") or {}).get("state"), "error": ((j.get("status") or {}).get("errorResult") or {}).get("message"),
            "created": st.get("creationTime"), "start": st.get("startTime"), "end": st.get("endTime"),
            "bytes_processed": int(q.get("totalBytesProcessed") or 0), "bytes_billed": billed,
            "slot_ms": int(st.get("totalSlotMs") or q.get("totalSlotMs") or 0), "cache_hit": bool(q.get("cacheHit")),
            "spilled_bytes": sum(int(s.get("shuffleOutputBytesSpilled") or 0) for s in q.get("queryPlan") or []),
            "destination": ".".join(dest[k] for k in ("projectId", "datasetId", "tableId") if k in dest) or None,
            "labels": conf.get("labels") or {}, "cost_usd": _bq_cost(billed),
            "dbt_node": _dbt_node((conf.get("query") or {}).get("query"))}


def bigquery_jobs(gcp, labels: dict[str, set[str]], start: str, end: str | None) -> list[dict]:
    """The console project's BigQuery jobs created in [start, end] with any of these label
    values, every user's when the console may list them (bigquery.jobs.listAll), else its own."""
    if not any(labels.values()):
        return []
    a, b = _window(start, end)
    params = {"projection": "full", "maxResults": 1000,
              "minCreationTime": str(int(_parse(a).timestamp() * 1000)),
              "maxCreationTime": str(int(_parse(b).timestamp() * 1000))}
    out, token, every_user = [], None, True
    while True:
        try:
            page = gcp.bq_get("jobs", **params, **({"allUsers": "true"} if every_user else {}),
                              **({"pageToken": token} if token else {}))
        except requests.HTTPError as e:
            if every_user and e.response is not None and e.response.status_code == 403:
                every_user, token, out = False, None, []
                continue
            raise
        for j in page.get("jobs", []):
            got = (j.get("configuration") or {}).get("labels") or {}
            if any(got.get(k) in vals for k, vals in labels.items()):
                out.append(_bq_row(j))
        token = page.get("nextPageToken")
        if not token:
            return sorted(out, key=lambda r: int(r["created"] or 0))


def _bq_totals(jobs: list[dict]) -> dict:
    return {"jobs": len(jobs), "bytes_processed": sum(j["bytes_processed"] for j in jobs),
            "bytes_billed": sum(j["bytes_billed"] for j in jobs), "slot_ms": sum(j["slot_ms"] for j in jobs),
            "cost_usd": sum(j["cost_usd"] for j in jobs)}


def dbt_results(gcp, files_uri: str, executions: list[str]) -> list[dict]:
    """The run_results mlops-task kept for these executions, oldest command first."""
    out = []
    for ex in executions:
        bucket, _, prefix = files.runs_uri(files_uri, ex).removeprefix("gs://").partition("/")
        objs = sorted(gcp.list_objects(bucket, f"{prefix}/", "**/dbt-*.json"),
                      key=lambda o: int(re.search(r"dbt-(\d+)\.json$", o["name"])[1]))
        out += [gcp.read_json(bucket, o["name"]) | {"execution": ex} for o in objs]
    return out


def dbt_models(results: list[dict], jobs: list[dict]) -> list[dict]:
    """A row per node a dbt command ran, with the bytes, slots and cost of every BigQuery job it
    ran (run_results names only its last, e.g. a table's temp-table DROP); it opens the costliest."""
    by_id = {j["job_id"]: j for j in jobs}
    by_node: dict[tuple, list[dict]] = {}
    for j in jobs:
        if j.get("dbt_node"):
            by_node.setdefault((j["labels"].get("dbt_invocation_id"), j["dbt_node"]), []).append(j)
    rows = []
    for cmd in results:
        for r in cmd["results"]:
            ar = r.get("adapter_response") or {}
            js = by_node.get((cmd.get("invocation_id"), r["unique_id"])) or (
                [by_id[ar["job_id"]]] if ar.get("job_id") in by_id else [])
            top = max(js, key=lambda j: (j["bytes_billed"], j["slot_ms"])) if js else None
            rows.append({"unique_id": r["unique_id"], "name": (r["unique_id"] or "").split(".")[-1],
                         "resource": (r["unique_id"] or "").split(".")[0], "command": cmd["command"],
                         "status": r["status"], "seconds": r.get("execution_time"), "start": r.get("started_at"),
                         "end": r.get("completed_at"), "relation": r.get("relation_name"), "message": r.get("message"),
                         "rows": ar.get("rows_affected"), "jobs": len(js),
                         "job_id": top["job_id"] if top else ar.get("job_id"),
                         "project": top["project"] if top else ar.get("project_id"),
                         "location": top["location"] if top else ar.get("location"),
                         "bytes_processed": sum(j["bytes_processed"] for j in js) if js else ar.get("bytes_processed"),
                         "bytes_billed": sum(j["bytes_billed"] for j in js) if js else None,
                         "slot_ms": sum(j["slot_ms"] for j in js) if js else None,
                         "cache_hit": all(j["cache_hit"] for j in js) if js else None,
                         "spilled_bytes": sum(j["spilled_bytes"] for j in js) if js else None,
                         "cost_usd": sum(j["cost_usd"] for j in js) if js else None})
    return rows


def bigquery_job(gcp, project: str, location: str, job_id: str) -> dict:
    """One BigQuery job as its query profile shows it: totals, the SQL, and each stage's
    timings (average and slowest worker), rows, shuffle and spill."""
    j = gcp.bq_get(f"jobs/{job_id}", project=project, location=location)
    q = (j.get("statistics") or {}).get("query") or {}
    stages = [{"id": s.get("id"), "name": s.get("name"), "status": s.get("status"),
               "start_ms": int(s.get("startMs") or 0), "end_ms": int(s.get("endMs") or 0),
               **{f"{k}_{agg}": int(s.get(f"{k}Ms{agg.capitalize()}") or 0)
                  for k in ("wait", "read", "compute", "write") for agg in ("avg", "max")},
               "records_read": int(s.get("recordsRead") or 0), "records_written": int(s.get("recordsWritten") or 0),
               "shuffle_bytes": int(s.get("shuffleOutputBytes") or 0),
               "spilled_bytes": int(s.get("shuffleOutputBytesSpilled") or 0), "slot_ms": int(s.get("slotMs") or 0),
               "parallel_inputs": int(s.get("parallelInputs") or 0), "inputs": s.get("inputStages") or [],
               "steps": [{"kind": st.get("kind"), "substeps": (st.get("substeps") or [])[:8]} for st in s.get("steps") or []]}
              for s in q.get("queryPlan") or []]
    return {**_bq_row(j), "query": ((j.get("configuration") or {}).get("query") or {}).get("query"),
            "partitions": int(q.get("totalPartitionsProcessed") or 0),
            "referenced": [".".join(t[k] for k in ("projectId", "datasetId", "tableId")) for t in q.get("referencedTables") or []],
            "stages": stages, "insights": stage_insights(stages),
            "link": f"https://console.cloud.google.com/bigquery?project={project}&j=bq:{location}:{job_id}&page=queryresults"}


# --- what stands out ----------------------------------------------------------------------

def stage_insights(stages: list[dict]) -> list[dict]:
    out = []
    for s in stages:
        if s["spilled_bytes"]:
            out.append({"level": "warning", "text": f"{s['name']} spilled {s['spilled_bytes'] / GIB:.2f} GiB of shuffle to disk: "
                        "too little memory per slot for its data. Filter or aggregate earlier, or join on a smaller side first."})
        busy = s["compute_avg"] + s["read_avg"]
        if s["compute_max"] + s["read_max"] > 5 * max(busy, 1) and s["compute_max"] + s["read_max"] > 2000:
            out.append({"level": "warning", "text": f"{s['name']}: its slowest worker took {(s['compute_max'] + s['read_max']) / 1000:.1f}s "
                        f"against {busy / 1000:.1f}s on average: skewed data. A join or GROUP BY key with a few very common values "
                        "(often NULL) puts most rows on one worker."})
    return out


def insights(units: list[dict], jobs: list[dict], models: list[dict]) -> list[dict]:
    out = []
    for u in units:
        use, m = u["usage"], u["machine"]
        if use.get("memory_peak") is not None and use["memory_peak"] >= 0.9:
            out.append({"level": "warning", "text": f"{u['label']} peaked at {use['memory_peak']:.0%} of its memory: "
                        "one larger input runs it out (exit 137). Give it more memory, or read in chunks."})
        elif use.get("memory_peak") is not None and use["memory_peak"] < 0.3 and (m.get("memory_bytes") or 0) > 2 * GIB:
            out.append({"level": "info", "text": f"{u['label']} used at most {use['memory_peak']:.0%} of its memory: "
                        "a smaller limit costs less."})
        if use.get("cpu_mean") is not None and use["cpu_mean"] < 0.2 and ((m.get("cpu") or 0) >= 2 or m.get("machine_type")):
            out.append({"level": "info", "text": f"{u['label']} kept its CPUs {use['cpu_mean']:.0%} busy on average: "
                        "it waits on I/O (often BigQuery) more than it computes; fewer or smaller CPUs would do."})
        if use.get("accelerator_mean") is not None and use["accelerator_mean"] < 0.3:
            out.append({"level": "info", "text": f"{u['label']}'s accelerators were {use['accelerator_mean']:.0%} busy on average: "
                        "the input pipeline, not the GPU, sets its pace."})
    total = sum(j["cost_usd"] for j in jobs)
    if models and total > 0.01:
        top = max((r for r in models if r["cost_usd"]), key=lambda r: r["cost_usd"], default=None)
        if top and top["cost_usd"] >= 0.5 * total:
            out.append({"level": "info", "text": f"{top['name']} is {top['cost_usd'] / total:.0%} of this run's BigQuery cost "
                        f"({top['bytes_billed'] / GIB:.1f} GiB billed): partition or cluster what it reads, or make it incremental."})
    for j in jobs:
        if j["spilled_bytes"]:
            out.append({"level": "warning", "text": f"BigQuery job {j['job_id']} spilled shuffle to disk: open it for the stage."})
    return out


# --- a task run ---------------------------------------------------------------------------

def _files_uri(spec: dict) -> str | None:
    args = spec.get("args") or []
    return args[args.index("--files") + 1] if "--files" in args else None


def _served_executions(gcp, spec: dict, attempt: dict) -> list[tuple[str, dict]]:
    """(model, execution) of each served model's Cloud Run job the batch_serve run started."""
    if not attempt.get("start"):
        return []
    lo, hi = attempt["start"], attempt.get("end") or "9999"
    out = []
    for m in (spec.get("argument") or {}).get("models") or []:
        if not m.get("job"):
            continue
        for x in gcp.run_executions(m["job"], 20):
            if lo <= (x.get("createTime") or "") <= hi:
                out.append((m["model"], x))
    return out


def task_stats(gcp, workflow: str, execution_id: str, task: str) -> dict:
    """What a task run's executions used and cost, its BigQuery jobs (a dbt_task's per model),
    and what stands out."""
    key = (workflow, execution_id, task)
    if key in _DONE:
        return _DONE[key]
    t = S.task_run(gcp, workflow, execution_id, task)
    kind = S.task_kind(t["spec"])
    units: list[dict] = []
    if kind == "vertex_pipeline_task":
        run = S.pipeline_run(gcp, workflow, execution_id, task)["run"]
        if run:
            details = (gcp.ai_get(run["name"]).get("jobDetail") or {}).get("taskDetails", [])
            mains = [(d["taskName"], ((d.get("executorDetail") or {}).get("containerDetail") or {}).get("mainJob")
                      or ((d.get("executorDetail") or {}).get("customJobDetail") or {}).get("job")) for d in details]
            with ThreadPoolExecutor(4) as pool:
                units = list(pool.map(lambda m: custom_job(gcp, m[1], m[0]), [m for m in mains if m[1]]))
    elif kind == "batch_serve_task":
        served = [s for a in t["attempts"] for s in _served_executions(gcp, t["spec"], a)]
        with ThreadPoolExecutor(4) as pool:
            units = list(pool.map(lambda s: cloud_run(gcp, s[1]["name"], s[1], s[0]), served))
    else:
        cr = [a for a in t["attempts"] if a["kind"] == "cloud_run"]
        with ThreadPoolExecutor(4) as pool:
            units = list(pool.map(lambda a: cloud_run(gcp, a["name"], gcp.run_execution(a["name"]),
                                                      a.get("input") or a["name"].rsplit("/", 1)[-1]), cr))
    results = dbt_results(gcp, _files_uri(t["spec"]), [u["id"] for u in units]) if kind == "dbt_task" and _files_uri(t["spec"]) else []
    start = t.get("start") or t["run"]["start"]
    jobs = bigquery_jobs(gcp, {"dbt_invocation_id": {r["invocation_id"] for r in results if r.get("invocation_id")},
                               LABEL: {u["id"] for u in units}}, start, t.get("end")) if start else []
    models = dbt_models(results, jobs)
    compute = sum(u["cost_usd"] or 0 for u in units)
    bq = _bq_totals(jobs)
    out = {"kind": kind, "units": units, "bigquery": {"jobs": jobs, "totals": bq}, "dbt": models,
           "totals": {"compute_usd": compute if any(u["cost_usd"] for u in units) else None,
                      "bigquery_usd": bq["cost_usd"], "seconds": S._seconds(t["start"], t["end"]) if t.get("start") and t.get("end") else None},
           "insights": insights(units, jobs, models),
           "prices": {"bq_usd_per_tib": BQ_USD_PER_TIB, "run_usd_per_vcpu_s": RUN_USD_PER_VCPU_S,
                      "run_usd_per_gib_s": RUN_USD_PER_GIB_S}}
    if t.get("end") and datetime.now(UTC) - _parse(t["end"]) > METRICS_LAG:
        _DONE[key] = out
    return out
