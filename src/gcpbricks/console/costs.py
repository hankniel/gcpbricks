"""The console's Costs page: what the MLOps platform's queries and compute cost, day by day and
by service, and what is running (and billing) right now. Estimates at list price (console/prices.py,
from the Cloud Billing Catalog), before discounts, credits and committed use; nothing here
reads the billing account, which the console has no access to.

    bigquery    bytes billed per job the console's service account ran with mlops_exec or
                dbt's labels (INFORMATION_SCHEMA.JOBS_BY_USER)
    cloud_run   vCPU- and GiB-seconds allocated (Cloud Monitoring): bundles' jobs, the
                notifier job and the console's own service, plus its idle minimum instances
    vertex      bundles' custom jobs and pipeline steps: machine x replicas x run time;
                labelled endpoints: minimum replicas x the time deployed
    notebooks   Colab runtimes: machine x the time running, from the audit log, plus their
                disks for as long as they exist. Colab's idle shutdown is not in the audit
                log: a runtime that stopped that way has a range, from its idle timeout
                after the start (the least it ran) to its next start (the most)

A source the console may not read (a missing role) is reported in `errors`, not guessed.
Platform work is what carries the labels the platform sets (mlops_bundle, mlops_job, and
mlops_exec on a BigQuery job), named bundle/job from them; the project's other workloads are
left out.
"""
from __future__ import annotations

import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import requests

from gcpbricks.bundle import NOTIFY_JOB
from gcpbricks.console import prices as PR
from gcpbricks.console.stats import cpus, memory_bytes
from gcpbricks.job_labels import LABEL

SOURCES = ("bigquery", "cloud_run", "vertex", "notebooks")
TIB, GIB = 2 ** 40, 2 ** 30
#: How far before the window the audit log is read: a runtime started earlier may still run.
LOOKBACK = timedelta(days=14)
_NOTEBOOK_OPS = ("AssignNotebookRuntime", "StartNotebookRuntime", "StopNotebookRuntime", "DeleteNotebookRuntime")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _spread(by_day: dict[str, float], t0: datetime, t1: datetime, usd_per_hour: float, lo: datetime) -> float:
    """Adds usd_per_hour over [t0, t1] (clipped at `lo`) to each UTC day it covers; the total."""
    t0 = max(t0, lo)
    total = 0.0
    while t0 < t1:
        midnight = datetime(t0.year, t0.month, t0.day, tzinfo=UTC) + timedelta(days=1)
        end = min(t1, midnight)
        usd = (end - t0).total_seconds() / 3600 * usd_per_hour
        by_day[t0.strftime("%Y-%m-%d")] += usd
        total += usd
        t0 = end
    return total


def _is_platform(labels: dict) -> bool:
    return bool(labels.get("mlops_bundle") or labels.get("mlops_job"))


def _platform_name(labels: dict, fallback: str) -> str:
    bundle, job = labels.get("mlops_bundle"), labels.get("mlops_job")
    return f"{bundle}/{job}" if bundle and job else fallback


class _Shapes:
    """Machine types' family, cores and memory, each read once."""

    def __init__(self, gcp):
        self.gcp, self.zone = gcp, f"{gcp.region}-a"

    def __call__(self, machine_type: str) -> dict:
        return PR.machine_shape(self.gcp.machine_type(self.zone, machine_type), machine_type)


def _new() -> dict:
    return {"by_day": defaultdict(float), "items": defaultdict(lambda: {"usd": 0.0}), "running": [], "notes": []}


def _item(out: dict, name: str, kind: str, usd: float, **detail) -> None:
    it = out["items"][(kind, name)]
    it.update(name=name, kind=kind, usd=it["usd"] + usd, **{k: v for k, v in detail.items() if v is not None})


# --- BigQuery ------------------------------------------------------------------------------

def bigquery(gcp, prices: PR.Prices, start: datetime, end: datetime, location: str = "us-central1") -> dict:
    out = _new()
    price = prices.bigquery_tib(location)
    if price is None and location.startswith("us-"):
        # The catalog prices on-demand analysis for the US multi-region, not per US region.
        price = prices.bigquery_tib("us")
        out["notes"].append(f"BigQuery: jobs in {location} priced at the US multi-region's on-demand rate.")
    if price is None:
        raise LookupError(f"the catalog has no BigQuery analysis price for {location}")
    sql = f"""
SELECT FORMAT_DATE('%F', DATE(creation_time)) AS day,
  (SELECT value FROM UNNEST(labels) WHERE key = '{LABEL}') AS exec,
  EXISTS(SELECT 1 FROM UNNEST(labels) WHERE key = 'dbt_invocation_id') AS dbt,
  COUNT(*) AS jobs, SUM(IFNULL(total_bytes_billed, 0)) AS billed
FROM `region-{location}`.INFORMATION_SCHEMA.JOBS_BY_USER
WHERE creation_time >= TIMESTAMP('{_iso(start)}') AND creation_time < TIMESTAMP('{_iso(end)}')
  AND job_type = 'QUERY' AND IFNULL(statement_type, '') != 'SCRIPT'
GROUP BY day, exec, dbt
HAVING exec IS NOT NULL OR dbt"""
    r = gcp.bq_query({"query": sql, "useLegacySql": False, "timeoutMs": 30000, "maxResults": 50000})
    jobs_named = _job_names(gcp)
    for row in r.get("rows", []):
        day, exec_, _dbt, jobs, billed = (c["v"] for c in row["f"])
        usd = int(billed or 0) / TIB * price["usd"]
        out["by_day"][day] += usd
        if exec_:
            # a Cloud Run execution is "<job>-<5 chars>"; a Vertex AI custom job is a number
            job = re.sub(r"-[a-z0-9]{5}$", "", exec_)
            name, kind = jobs_named.get(job, job if not exec_.isdigit() else f"Vertex AI job {exec_}"), "platform job"
        else:
            name, kind = "dbt", "dbt"
        _item(out, name, kind, usd)
        it = out["items"][(kind, name)]
        it["jobs"] = it.get("jobs", 0) + int(jobs)
        it["tib_billed"] = it.get("tib_billed", 0) + int(billed or 0) / TIB
    out["prices"] = [price]
    return out


def _job_names(gcp) -> dict[str, str]:
    """Cloud Run job -> bundle/job, from its labels."""
    return {(short := j["name"].rsplit("/", 1)[1]): _platform_name(j.get("labels") or {}, short) for j in gcp.run_jobs()}


# --- Cloud Run -----------------------------------------------------------------------------

def cloud_run(gcp, prices: PR.Prices, start: datetime, end: datetime, now: datetime,
              console: str | None = None) -> dict:
    """`console`: the console's own Cloud Run service, the one service counted."""
    out = _new()
    jobs = {j["name"].rsplit("/", 1)[1]: j for j in gcp.run_jobs()}
    jobs = {n: j for n, j in jobs.items() if _is_platform(j.get("labels") or {}) or n == NOTIFY_JOB}
    services = {n: s for s in gcp.run_services() if (n := s["name"].rsplit("/", 1)[1]) == console}
    p_job, p_req, p_min, p_inst = (prices.cloud_run(k) for k in ("job", "service", "service_min", "service_instance"))

    def usage(rtype: str, label: str) -> dict:
        """name -> day -> [vCPU-seconds, GiB-seconds]"""
        got: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
        for i, metric in enumerate(("cpu/allocation_time", "memory/allocation_time")):
            for ts in gcp.time_series(f'resource.type="{rtype}" AND metric.type="run.googleapis.com/container/{metric}"',
                                      _iso(start), _iso(end), "ALIGN_SUM", period="3600s"):
                name = ts["resource"]["labels"].get(label, "")
                for p in ts.get("points", []):
                    v = p["value"].get("doubleValue", p["value"].get("int64Value"))
                    day = (_ts(p["interval"]["endTime"]) - timedelta(seconds=1)).strftime("%Y-%m-%d")
                    got[name][day][i] += float(v or 0)
        return got

    for name, days in usage("cloud_run_job", "job_name").items():
        if name not in jobs:
            continue
        labels = (jobs.get(name) or {}).get("labels") or {}
        total = 0.0
        for day, (vcpu_s, gib_s) in days.items():
            usd = vcpu_s * p_job["usd_per_vcpu_s"] + gib_s * p_job["usd_per_gib_s"]
            out["by_day"][day] += usd
            total += usd
        _item(out, _platform_name(labels, name), "job", total)

    for name, days in usage("cloud_run_revision", "service_name").items():
        if name not in services:
            continue
        res = ((services.get(name) or {}).get("template") or {}).get("containers", [{}])[0].get("resources") or {}
        price = p_req if res.get("cpuIdle", True) else p_inst
        total = 0.0
        for day, (vcpu_s, gib_s) in days.items():
            usd = vcpu_s * price["usd_per_vcpu_s"] + gib_s * price["usd_per_gib_s"]
            out["by_day"][day] += usd
            total += usd
        _item(out, name, "service", total)

    # A request-based service's minimum instances bill while idle too, at their own rate.
    for name, s in services.items():
        t = s.get("template") or {}
        res = (t.get("containers") or [{}])[0].get("resources") or {}
        n = int((t.get("scaling") or {}).get("minInstanceCount") or 0)
        if not n:
            continue
        cpu, gib = cpus(res.get("limits", {}).get("cpu")) or 0, (memory_bytes(res.get("limits", {}).get("memory")) or 0) / GIB
        price = p_min if res.get("cpuIdle", True) else p_inst
        per_hour = n * 3600 * (cpu * price["usd_per_vcpu_s"] + gib * price["usd_per_gib_s"])
        since = _ts(s.get("updateTime") or s.get("createTime") or _iso(start))
        total = _spread(out["by_day"], since, now, per_hour, start)
        _item(out, name, "service", total, min_instances=n)
        out["running"].append({"source": "cloud_run", "name": name, "kind": "service", "usd_per_hour": per_hour,
                               "since": _iso(since), "detail": f"{n} minimum instance(s) of {cpu:g} vCPU, {gib:g} GiB, always on"})
    out["notes"].append("Cloud Run: a request-based service's idle minimum instances are counted from its last "
                        "deploy at today's minimum.")
    out["prices"] = [*p_job["skus"], *p_req["skus"], *p_min["skus"]]
    return out


# --- Vertex AI -----------------------------------------------------------------------------

def vertex(gcp, prices: PR.Prices, shape, start: datetime, end: datetime, now: datetime) -> dict:
    out = _new()
    used = {}
    for j in gcp.ai_list("customJobs", "customJobs", filter=f'create_time>"{_iso(start - timedelta(days=1))}"', pageSize=100):
        if not j.get("startTime") or not _is_platform(j.get("labels") or {}):
            continue
        t0 = _ts(j["startTime"])
        running = j.get("state") in ("JOB_STATE_RUNNING", "JOB_STATE_PENDING", "JOB_STATE_QUEUED")
        t1 = now if running else _ts(j.get("endTime") or j.get("updateTime") or j["startTime"])
        per_hour, missing = 0.0, []
        for pool in j.get("jobSpec", {}).get("workerPoolSpecs", []):
            m = pool.get("machineSpec") or {}
            p = prices.vertex_hour("training", shape(m["machineType"]), PR.gpu_name(m.get("acceleratorType")),
                                   int(m.get("acceleratorCount") or 0))
            if p["usd_per_hour"] is None:
                missing.append(m["machineType"])
                continue
            per_hour += p["usd_per_hour"] * int(pool.get("replicaCount") or 1)
            used.update({s["sku"]: s for s in p["skus"]})
        labels = j.get("labels") or {}
        kind = "pipeline step" if "vertex-ai-pipelines-run-billing-id" in labels else "custom job"
        name = _platform_name(labels, j.get("displayName", ""))
        total = _spread(out["by_day"], t0, min(t1, end), per_hour, start)
        _item(out, name, kind, total, unpriced=", ".join(missing) or None)
        if running:
            out["running"].append({"source": "vertex", "name": name, "kind": kind, "usd_per_hour": per_hour,
                                   "since": j["startTime"], "detail": j.get("displayName")})
    for e in gcp.ai_list("endpoints", "endpoints"):
        if not _is_platform(e.get("labels") or {}):
            continue
        for dm in e.get("deployedModels", []):
            res = dm.get("dedicatedResources") or {}
            m = res.get("machineSpec") or {}
            if not m.get("machineType"):
                continue
            p = prices.vertex_hour("prediction", shape(m["machineType"]), PR.gpu_name(m.get("acceleratorType")),
                                   int(m.get("acceleratorCount") or 0))
            n = int(res.get("minReplicaCount") or 1)
            per_hour = (p["usd_per_hour"] or 0) * n
            used.update({s["sku"]: s for s in p["skus"]})
            total = _spread(out["by_day"], _ts(dm.get("createTime") or e["createTime"]), now, per_hour, start)
            name = f"{e.get('displayName')} / {dm.get('displayName') or dm.get('id')}"
            _item(out, name, "endpoint", total, min_replicas=n)
            out["running"].append({"source": "vertex", "name": name, "kind": "endpoint", "usd_per_hour": per_hour,
                                   "since": dm.get("createTime"), "detail": f"{n} x {m['machineType']} (minimum replicas)"})
    out["notes"].append("Vertex AI: endpoints at their minimum replicas; a pipeline run's own $0.03 fee is left out.")
    out["prices"] = list(used.values())
    return out


# --- notebooks -----------------------------------------------------------------------------

def _runtime_id(entry: dict) -> str | None:
    m = re.search(r"notebookRuntimes/(\d+)", (entry.get("operation") or {}).get("id", "")
                  or entry["protoPayload"].get("resourceName", ""))
    return m[1] if m else None


def runtime_events(entries: list[dict]) -> tuple[dict[str, list[tuple[datetime, str]]], dict[str, str]]:
    """Audit entries -> runtime id -> [(time, "up" | "down" | "gone")] in time order, and the
    template each runtime was assigned from. An operation counts when it completed without an
    error (its last entry); a refused one (out of GPUs, already running) does not."""
    events, templates = defaultdict(list), {}
    for e in entries:
        p, op = e["protoPayload"], e.get("operation") or {}
        rid = _runtime_id(e)
        if not rid:
            continue
        method = p["methodName"].rsplit(".", 1)[-1]
        tpl = (p.get("request") or {}).get("notebookRuntimeTemplate")
        if tpl:
            templates[rid] = tpl.rsplit("/", 1)[-1]
        done = op.get("last") or (not op.get("first") and not op)
        if not done or (p.get("status") or {}).get("code"):
            continue
        kind = {"AssignNotebookRuntime": "up", "StartNotebookRuntime": "up", "StopNotebookRuntime": "down",
                "DeleteNotebookRuntime": "gone"}.get(method)
        if kind:
            events[rid].append((_ts(e["timestamp"]), kind))
    return {k: sorted(v) for k, v in events.items()}, templates


def runtime_intervals(events: list[tuple[datetime, str]], idle: timedelta | None, state: str | None,
                      updated: datetime | None, now: datetime) -> list[dict]:
    """[{start, end, upper}]: the times a runtime ran. `end` is the least it can have run to,
    `upper` the most: they differ only where it stopped without a logged stop (idle shutdown)."""
    out, up = [], None
    for t, kind in events:
        if kind == "up":
            if up is not None:  # up again without a logged stop: it idled out in between
                low = min(up + idle, t) if idle else up
                out.append({"start": up, "end": low, "upper": t})
            up = t
        elif up is not None:
            out.append({"start": up, "end": t, "upper": t})
            up = None
    if up is not None:
        if state == "RUNNING":
            out.append({"start": up, "end": now, "upper": now})
        elif updated and updated > up:  # stopped since, by idle shutdown: the runtime says when
            out.append({"start": up, "end": updated, "upper": updated})
        else:
            low = min(up + idle, now) if idle else up
            out.append({"start": up, "end": low, "upper": now})
    return out


def notebooks(gcp, prices: PR.Prices, shape, start: datetime, end: datetime, now: datetime) -> dict:
    out = _new()
    out["by_day_upper"] = defaultdict(float)
    runtimes = {r["name"].rsplit("/", 1)[1]: r for r in gcp.ai_list("notebookRuntimes", "notebookRuntimes")}
    templates = {t["name"].rsplit("/", 1)[1]: t for t in gcp.ai_list("notebookRuntimeTemplates", "notebookRuntimeTemplates")}
    methods = " OR ".join(f'"{m}"' for m in _NOTEBOOK_OPS)
    entries = gcp.log_entries_all(f'protoPayload.serviceName="aiplatform.googleapis.com" AND '
                                  f'protoPayload.methodName:({methods}) AND timestamp>="{_iso(start - LOOKBACK)}"')
    events, assigned = runtime_events(entries)
    used = {}
    for rid in set(events) | set(runtimes):
        rt = runtimes.get(rid) or {}
        spec = rt or templates.get(assigned.get(rid, ""), {})
        m = spec.get("machineSpec") or {}
        owner = rt.get("runtimeUser", "")
        name = rt.get("displayName") or f"runtime {rid} (deleted)"
        idle_cfg = spec.get("idleShutdownConfig") or {}
        idle = (None if idle_cfg.get("idleShutdownDisabled") or not idle_cfg.get("idleTimeout")
                else timedelta(seconds=float(idle_cfg["idleTimeout"].rstrip("s"))))
        if not m.get("machineType"):
            _item(out, name, "runtime", 0.0, owner=owner or None, unpriced="its machine is unknown (template deleted)")
            continue
        p = prices.colab_hour(shape(m["machineType"]), PR.gpu_name(m.get("acceleratorType")),
                              int(m.get("acceleratorCount") or 0))
        used.update({s["sku"]: s for s in p["skus"]})
        per_hour = p["usd_per_hour"]
        if per_hour is None:
            _item(out, name, "runtime", 0.0, owner=owner or None, unpriced=m["machineType"])
            continue
        updated = _ts(rt["updateTime"]) if rt.get("updateTime") else None
        low = high = 0.0
        for iv in runtime_intervals(events.get(rid, []), idle, rt.get("runtimeState"), updated, now):
            if iv["upper"] <= start:
                continue
            low += _spread(out["by_day"], iv["start"], min(iv["end"], end), per_hour, start)
            high += _spread(out["by_day_upper"], iv["start"], min(iv["upper"], end), per_hour, start)
        # The data disk bills from creation to deletion, running or not. A runtime no longer listed
        # without a logged delete went some time after its last logged event.
        disk = spec.get("dataPersistentDiskSpec") or {}
        evs = events.get(rid, [])
        gone = next((t for t, k in evs if k == "gone"), None)
        if gone is None and not rt and evs:
            gone_low, gone_high = evs[-1][0], now
        else:
            gone_low = gone_high = gone or now
        made = _ts(rt["createTime"]) if rt.get("createTime") else next((t for t, k in evs if k == "up"), None)
        disk_usd = disk_upper = 0.0
        if disk.get("diskSizeGb") and made:
            d = prices.colab_disk_month(disk.get("diskType", "pd-standard"), float(disk["diskSizeGb"]))
            used.update({s["sku"]: s for s in d["skus"]})
            if d["usd_per_month"] is not None:
                per_hour_disk = d["usd_per_month"] / PR.HOURS_PER_MONTH
                disk_usd = _spread(out["by_day"], made, gone_low, per_hour_disk, start)
                disk_upper = _spread(out["by_day_upper"], made, gone_high, per_hour_disk, start)
        _item(out, name, "runtime", low + disk_usd, usd_upper=high + disk_upper, owner=owner or None,
              machine=m["machineType"] + (f" + {m.get('acceleratorCount')} x {PR.gpu_name(m.get('acceleratorType'))}"
                                          if PR.gpu_name(m.get("acceleratorType")) else ""),
              usd_per_hour=per_hour, disk_usd=disk_usd or None)
        if rt.get("runtimeState") == "RUNNING":
            last_up = max((t for t, k in events.get(rid, []) if k == "up"), default=None)
            out["running"].append({"source": "notebooks", "name": name, "kind": "runtime", "usd_per_hour": per_hour,
                                   "since": _iso(last_up) if last_up else rt.get("updateTime"), "owner": owner,
                                   "detail": f"{m['machineType']}, idle shutdown {idle_cfg.get('idleTimeout', 'off')}"})
    out["notes"].append("Notebooks: Colab's idle shutdown is not in the audit log, so a runtime that stopped that way "
                        "counts from its start to its idle timeout (the least it ran); the upper figure runs it to its "
                        "next start. Disks bill while a runtime exists, stopped or not.")
    out["prices"] = list(used.values())
    return out


# --- the page ------------------------------------------------------------------------------

def report(gcp, days: int = 30, now: datetime | None = None, bq_location: str = "us-central1",
           console: str | None = None) -> dict:
    """The Costs page for the last `days` UTC days (today included); `console` is the console's
    own Cloud Run service."""
    now = now or datetime.now(UTC)
    start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=days - 1)
    skus_of, price_source = PR.catalog(gcp)
    prices = PR.Prices(skus_of, gcp.region)
    shape = _Shapes(gcp)
    jobs = {"bigquery": lambda: bigquery(gcp, prices, start, now, bq_location),
            "cloud_run": lambda: cloud_run(gcp, prices, start, now, now, console),
            "vertex": lambda: vertex(gcp, prices, shape, start, now, now),
            "notebooks": lambda: notebooks(gcp, prices, shape, start, now, now)}
    got, errors = {}, {}
    with ThreadPoolExecutor(len(jobs)) as pool:
        futures = {k: pool.submit(f) for k, f in jobs.items()}
        for k, f in futures.items():
            try:
                got[k] = f.result()
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                why = "the console may not read it (a missing role)" if code == 403 else str(e)
                errors[k] = f"HTTP {code}: {why}"
            except Exception as e:  # noqa: BLE001 - one source failing leaves the page up
                errors[k] = f"{type(e).__name__}: {e}"
    day_names = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    rows = [{"day": d, **{s: round(got[s]["by_day"].get(d, 0.0), 4) for s in got}} for d in day_names]
    if "notebooks" in got:
        for r in rows:
            r["notebooks_upper"] = round(got["notebooks"]["by_day_upper"].get(r["day"], 0.0), 4)
    items = [{"source": s, **it} for s in got for it in got[s]["items"].values()]
    totals = {s: round(sum(r[s] for r in rows), 2) for s in got}
    return {"window": {"start": _iso(start), "end": _iso(now), "days": days},
            "days": rows, "totals": {**totals, "all": round(sum(totals.values()), 2),
                                     **({"notebooks_upper": round(sum(r["notebooks_upper"] for r in rows), 2)}
                                        if "notebooks" in got else {})},
            "items": sorted(items, key=lambda i: -i["usd"]),
            "running": sorted([r for s in got for r in got[s]["running"]], key=lambda r: -(r["usd_per_hour"] or 0)),
            "errors": errors, "notes": [n for s in got for n in got[s]["notes"]], "price_source": price_source,
            "prices": sorted({p["sku"]: p for s in got for p in got[s].get("prices", [])}.values(),
                             key=lambda p: p["description"])}
