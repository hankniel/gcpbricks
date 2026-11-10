"""What the console shows and does, over a Gcp client (console.gcp).

Jobs are found, not configured: every Workflow with the env `bundle.job_env` sets (MLOPS_JOB,
MLOPS_DEFINITION...) is a bundle job, whatever repo deployed it. Its definition (tasks,
levels, settings) is read from GCS; its runs are its Workflow executions; a run's task
timeline is its mlops_task / mlops_task_execution log entries; a task's logs are the
Cloud Run executions those entries name.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from gcpbricks import runs as R
from gcpbricks.bundle import WHEEL_LAUNCHER
from gcpbricks.ml import RETIRED
from gcpbricks.targets import storage_prefix

#: KEPT: a task a repair didn't pick, ending as it did in the attempt before (its logged result).
TERMINAL_TASK = {"SUCCESS", "FAILED", "UPSTREAM_FAILED", "EXCLUDED", "REUSED", "KEPT"}


def _job_of(wf: dict) -> dict | None:
    env = wf.get("userEnvVars") or {}
    if "MLOPS_JOB" not in env:
        return None
    labels = wf.get("labels") or {}
    return {"workflow": wf["name"].rsplit("/", 1)[-1], "job": env["MLOPS_JOB"],
            # Its display name (the job's `name`); one deployed before names, its key.
            "name": env.get("MLOPS_JOB_NAME") or env["MLOPS_JOB"],
            # A job its bundle no longer declares, kept since then (bundle.retired_jobs).
            "retired": labels.get(RETIRED),
            "target": env.get("MLOPS_TARGET", ""), "bundle": labels.get("mlops_bundle", ""),
            "description": wf.get("description", ""), "labels": labels,
            "run_as": wf.get("serviceAccount", "").rsplit("/", 1)[-1],
            "bucket": env.get("MLOPS_BUCKET", ""), "definition": env.get("MLOPS_DEFINITION", ""),
            "updated": wf.get("updateTime", ""),
            # Its Workflow runs some tasks only (job.yaml from 0.5.7); an older one runs them all.
            "runs_some": 'map.get(given, "only")' in wf.get("sourceContents", "")}


def scheduler_names(job: dict) -> dict[str, str]:
    """The Cloud Scheduler entries the bundle module names for a job (terraform/modules/bundle)."""
    slug = job["job"].replace("_", "-")
    return {"schedule": f"{job['bundle']}-{slug}-{job['target']}",
            "trigger": f"{job['bundle']}-{slug}-trigger-{job['target']}"}


def _schedulers(gcp) -> dict[str, dict]:
    return {s["name"].rsplit("/", 1)[-1]: s for s in gcp.schedulers()}


def _start_summary(sched: dict | None) -> dict | None:
    if not sched:
        return None
    return {"name": sched["name"].rsplit("/", 1)[-1], "cron": sched.get("schedule"),
            "time_zone": sched.get("timeZone"), "paused": sched.get("state") == "PAUSED"}


def _run_summary(ex: dict) -> dict:
    tasks = R.task_results(ex) or {}
    counts: dict[str, int] = {}
    for t in tasks.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    result = ex.get("result") or ""
    try:
        error = json.loads((ex.get("error") or {}).get("payload") or "null")
    except ValueError:
        error = (ex.get("error") or {}).get("payload")
    run_id, repair_id = R.run_of(ex)
    return {"id": ex["name"].rsplit("/", 1)[-1], "run_id": run_id, "repair_id": repair_id, "state": ex.get("state"),
            "error_message": error.get("message", "") if isinstance(error, dict) else (error or ""),
            "skipped": '"SKIPPED"' in result, "start": ex.get("startTime"),
            # Databricks' "Succeeded with failures": every leaf task succeeded, some other task didn't.
            "with_failures": ex.get("state") == "SUCCEEDED" and any(
                t["status"] in ("FAILED", "UPSTREAM_FAILED") for t in tasks.values()),
            "end": ex.get("endTime"), "params": R.run_params(ex), "tasks": counts,
            "task_states": {k: t["status"] for k, t in tasks.items()},
            "repair": '"repair"' in (ex.get("argument") or "")}


def list_jobs(gcp, target: str | None = None, last_runs: int = 1) -> list[dict]:
    jobs = [j for wf in gcp.workflows() if (j := _job_of(wf))
            and (target is None or j["target"] == target)]
    scheds = _schedulers(gcp)

    def enrich(job: dict) -> dict:
        names = scheduler_names(job)
        runs = gcp.executions(job["workflow"], last_runs)
        return {**job, "schedule": _start_summary(scheds.get(names["schedule"])),
                "trigger": _start_summary(scheds.get(names["trigger"])),
                "runs": group_runs([_run_summary(e) for e in runs])}
    with ThreadPoolExecutor(8) as pool:
        out = list(pool.map(enrich, jobs))
    return sorted(out, key=lambda j: (j["bundle"], j["job"], j["target"]))


def _find(gcp, workflow: str) -> dict:
    job = _job_of(gcp.workflow(workflow))
    if job is None:
        raise KeyError(f"{workflow} is not a bundle job")
    return job


def _detail(t: dict) -> str:
    """What a task runs, as the task graph's second line shows it."""
    if t["type"] == "condition":
        return f'{t["left"]} {OPS.get(t["op"], t["op"])} {t["right"]}'
    if t["type"] == "cloud_run":
        return t["run_job"]
    if t["type"] == "workflow":
        arg = t.get("argument") or {}
        return arg["object"].rsplit("/", 1)[-1] if "object" in arg and "output_prefix" in arg else t["workflow"]
    if t["type"] == "for_each":
        return f'{t["inputs"]}: {_detail(t["task"])}'
    return ""


def task_kind(t: dict) -> str:
    """The bundle task type a rendered task came from (bundle._descriptor): what Databricks
    names a task by ("Python wheel", "dbt", "Run Job"...)."""
    if t["type"] == "cloud_run":
        args = t.get("args") or []
        if args[:3] == ["python", "-c", WHEEL_LAUNCHER]:
            return "python_wheel_task"
        if args[:2] == ["mlops-task", "dbt"]:
            return "dbt_task"
        return "spark_python_task"
    if t["type"] == "workflow":
        arg = t.get("argument") or {}
        if "pipeline" in arg and "spec_prefix" in arg:
            return "vertex_pipeline_task"
        if "models" in arg and "window_days" in arg:
            return "batch_serve_task"
        if "object" in arg and "output_prefix" in arg:
            return "notebook_task"
        return "run_job_task"
    return f'{t["type"]}_task'


OPS = {"EQUAL_TO": "==", "NOT_EQUAL": "!=", "GREATER_THAN": ">", "GREATER_THAN_OR_EQUAL": ">=",
       "LESS_THAN": "<", "LESS_THAN_OR_EQUAL": "<="}


def graph(definition: dict) -> dict:
    """Nodes and edges of a job's task graph, with each node's level for layout."""
    level_of = {k: i for i, level in enumerate(definition["levels"]) for k in level}
    nodes, edges = [], []
    for key, t in definition["tasks"].items():
        inner = t.get("task") if t["type"] == "for_each" else None
        nodes.append({"key": key, "type": t["type"], "kind": task_kind(t),
                      "inner_kind": task_kind(inner) if inner else None, "level": level_of.get(key, 0),
                      "run_if": t.get("run_if"), "workflow": t.get("workflow"),
                      "run_job": (inner or t).get("run_job"), "for_each": bool(inner),
                      "max_retries": t.get("max_retries", 0), "detail": _detail(t),
                      "depends_on": [d["task_key"] for d in t.get("depends_on", [])]})
        edges += [{"from": d["task_key"], "to": key, "outcome": d.get("outcome") or ""}
                  for d in t.get("depends_on", [])]
    return {"nodes": nodes, "edges": edges}


#: Finished runs' task durations, read once per console instance.
_TASK_SECS: dict[tuple[str, str], dict[str, float]] = {}


def _seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def task_seconds(gcp, workflow: str, runs: list[dict]) -> dict[str, dict[str, float]]:
    """{run id: {task: seconds}} for the runs' finished tasks, from one Logging query over the
    runs not yet cached (each task logs mlops_task when it starts and when it ends)."""
    todo = [r for r in runs if (workflow, r["id"]) not in _TASK_SECS and r.get("start")]
    if todo:
        flt = (f'resource.type="workflows.googleapis.com/Workflow" AND resource.labels.workflow_id="{workflow}" '
               f'AND jsonPayload.event="mlops_task"' + _since(min(r["start"] for r in todo)))
        spans: dict[str, dict[str, dict]] = {}
        for e in gcp.log_entries(flt, 1000):
            j, ex = e.get("jsonPayload") or {}, (e.get("labels") or {}).get("execution_id")
            if not ex or not j.get("task"):
                continue
            span = spans.setdefault(ex, {}).setdefault(j["task"], {})
            if j.get("status") == "STARTED":
                span.setdefault("start", e["timestamp"])
            elif j.get("status") in TERMINAL_TASK:
                span["end"] = e["timestamp"]
        for r in todo:
            secs = {t: _seconds(sp["start"], sp["end"]) for t, sp in spans.get(r["id"], {}).items()
                    if "start" in sp and "end" in sp}
            if r["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                _TASK_SECS[(workflow, r["id"])] = secs
            else:
                r["_secs"] = secs
    return {r["id"]: _TASK_SECS.get((workflow, r["id"]), r.pop("_secs", {})) for r in runs}


def job_detail(gcp, workflow: str, n_runs: int = 25) -> dict:
    job = _find(gcp, workflow)
    definition = gcp.read_json(job["bucket"], job["definition"])
    scheds = _schedulers(gcp)
    names = scheduler_names(job)
    return {**job, "parameters": definition.get("parameters", {}),
            "settings": {k: definition.get(k) for k in ("max_concurrent_runs", "queue",
                                                         "timeout_seconds", "duration_warning_seconds")},
            # The job as declared (bundle.job_settings); None for one deployed before 0.5.2.
            "declared": definition.get("settings"),
            # Its tags: the Workflow's labels neither the bundle module nor terraform set.
            "tags": {k: v for k, v in job["labels"].items() if not k.startswith(("mlops_", "goog-"))},
            "graph": graph(definition),
            # Each task's definition, for the task panel the graph opens.
            "tasks": definition["tasks"],
            "schedule": _start_summary(scheds.get(names["schedule"])),
            "trigger": _start_summary(scheds.get(names["trigger"])),
            "runs": group_runs(_with_task_seconds(gcp, workflow, [_run_summary(e) for e in gcp.executions(workflow, n_runs)]))}


def group_runs(runs: list[dict]) -> list[dict]:
    """One row per run, as Databricks lists them: a repaired run shows under its own id with its
    latest attempt's state and tasks, `execution` that attempt's, `repairs` how many."""
    attempts: dict[str, list[dict]] = {}
    for r in runs:
        attempts.setdefault(r["run_id"], []).append(r)
    rows = []
    for run_id, xs in attempts.items():
        xs.sort(key=lambda r: r["repair_id"])
        first, latest = xs[0], xs[-1]
        secs: dict[str, float] = {}
        for x in xs:
            secs.update(x.get("task_seconds") or {})
        rows.append({**latest, "id": run_id, "execution": latest["id"], "repairs": latest["repair_id"],
                     "start": first["start"] if first["repair_id"] == 0 else latest["start"],
                     **({"task_seconds": secs} if "task_seconds" in latest else {})})
    return sorted(rows, key=lambda r: r.get("start") or "", reverse=True)


def _with_task_seconds(gcp, workflow: str, runs: list[dict]) -> list[dict]:
    secs = task_seconds(gcp, workflow, runs)
    return [{**r, "task_seconds": secs.get(r["id"], {})} for r in runs]


def _since(iso: str | None) -> str:
    """A minute before `iso`: Cloud Logging finds nothing for a query with no time bound."""
    if not iso:
        return ""
    t = datetime.fromisoformat(iso) - timedelta(minutes=1)
    return f' AND timestamp>="{t.strftime("%Y-%m-%dT%H:%M:%SZ")}"'


def _run_log_filter(workflow: str, execution_id: str, start: str | None) -> str:
    return (f'resource.type="workflows.googleapis.com/Workflow" AND resource.labels.workflow_id="{workflow}" '
            f'AND labels.execution_id="{execution_id}" '
            f'AND jsonPayload.event=("mlops_run" OR "mlops_task" OR "mlops_task_execution")'
            + _since(start))


#: Finished runs never change: their detail is read once per console instance.
_FINISHED: dict[tuple[str, str], dict] = {}
#: A running one's, kept a few seconds: a page's polls (run, task, logs, pipeline) read it
#: once between them, under Cloud Logging's 60 reads a minute per project.
_LIVE: dict[tuple[str, str], tuple[float, dict]] = {}
LIVE_SECONDS = 5.0


def _kind(name: str) -> str:
    """What a task's logged execution is: a Cloud Run job's execution (a container) or a
    Workflow's (a run_job_task's child run, or a vertex_pipeline / batch_serve runner)."""
    return "workflow" if "/workflows/" in name else "cloud_run"


def _workflow_execution(name: str) -> tuple[str, str]:
    """(workflow, execution id) of projects/p/locations/r/workflows/w/executions/id."""
    parts = name.split("/")
    return parts[5], parts[7]


def _error_text(error) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or json.dumps(error))
    return "" if error is None else str(error)


def run_detail(gcp, workflow: str, execution_id: str, repair_id: int | None = None) -> dict:
    """A run as Databricks shows one: an attempt of it (the original or a repair) with that
    attempt's task timeline, and the run's repair_history to pick another. `execution_id` is
    the run's (its latest attempt, or `repair_id`'s) or an attempt's (that one). A task an
    attempt reused says in `ran_in` which attempt ran it."""
    run_id, own = _run_of(gcp, workflow, execution_id)
    job = _find(gcp, workflow)
    hist = R.history(gcp, job, run_id)
    started = [h for h in hist if h["execution"]]
    pick = own if own else (repair_id if repair_id is not None else started[-1]["repair_id"])
    chosen = next((h for h in started if h["repair_id"] == pick), None)
    if chosen is None:
        raise KeyError(f"run {run_id} has no repair {pick}")
    details = {h["repair_id"]: execution_detail(gcp, workflow, h["execution"]) for h in started if h["repair_id"] <= pick}
    detail = details[pick]
    ran_in: dict[str, int] = {}
    for rid in sorted(details):
        for key, t in details[rid]["task_runs"].items():
            if t["status"] != "REUSED":
                ran_in[key] = rid
    excluded = _excluded(gcp, workflow, chosen["execution"], detail)
    tasks = {k: {**t, "ran_in": ran_in.get(k, pick), **({"excluded": excluded[k]} if k in excluded else {})}
             for k, t in detail["task_runs"].items()}
    history =[{**h, **({k: d.get(k) for k in ("state", "start", "end")} if (d := _attempt_state(gcp, workflow, h, details)) else {})}
               for h in hist]
    return {**detail, "task_runs": tasks, "run_id": run_id, "repair_id": pick,
            "latest_repair_id": hist[-1]["repair_id"], "repair_history": history}


#: A finished attempt's pipeline steps that didn't run, read once per console instance.
_EXCLUDED: dict[tuple[str, str], dict[str, list[str]]] = {}


def _excluded(gcp, workflow: str, execution_id: str, detail: dict) -> dict[str, list[str]]:
    """{task_key: its pipeline's steps a condition kept from running} for a finished attempt's
    vertex_pipeline_tasks that succeeded: a pipeline whose gate said no still succeeds, and
    this is what says so (Databricks shows such tasks as excluded)."""
    if detail.get("state") in (None, "ACTIVE", "QUEUED"):
        return {}
    if (workflow, execution_id) not in _EXCLUDED:
        ok = {k for k, t in detail["task_runs"].items() if t["status"] == "SUCCESS"}
        jobs = R.pipeline_runs(gcp.execution(workflow, execution_id), _child(gcp),
                               _children(gcp, workflow, execution_id)) if ok else {}
        out = {}
        for key, name in jobs.items():
            if key in ok and (steps := [n["label"] for n in pipeline_graph(gcp.ai_get(name))["nodes"]
                                        if n["kind"] == "step" and n["state"] == "SKIPPED"]):
                out[key] = steps
        _EXCLUDED[(workflow, execution_id)] = out
    return _EXCLUDED[(workflow, execution_id)]


#: Which run, and which attempt of it, an execution is: its argument never changes.
_RUN_OF: dict[tuple[str, str], tuple[str, int]] = {}


def _run_of(gcp, workflow: str, execution_id: str) -> tuple[str, int]:
    if (workflow, execution_id) not in _RUN_OF:
        _RUN_OF[(workflow, execution_id)] = R.run_of(gcp.execution(workflow, execution_id))
    return _RUN_OF[(workflow, execution_id)]


def _attempt_state(gcp, workflow: str, h: dict, details: dict) -> dict | None:
    if not h["execution"]:
        return None
    if h["repair_id"] in details:
        return details[h["repair_id"]]
    return _run_summary(gcp.execution(workflow, h["execution"]))


def execution_detail(gcp, workflow: str, execution_id: str) -> dict:
    """One execution with its task timeline: when each task started and ended, how, and the
    Cloud Run executions it started (one per attempt, or per for_each input)."""
    if (workflow, execution_id) in _FINISHED:
        return _FINISHED[(workflow, execution_id)]
    live = _LIVE.get((workflow, execution_id))
    if live and time.monotonic() - live[0] < LIVE_SECONDS:
        return live[1]
    ex = gcp.execution(workflow, execution_id)
    entries = gcp.log_entries(_run_log_filter(workflow, execution_id, ex.get("startTime")))
    tasks: dict[str, dict] = {}
    for e in entries:
        j, ts = e.get("jsonPayload") or {}, e.get("timestamp")
        key = j.get("task")
        if not key:
            continue
        t = tasks.setdefault(key, {"key": key, "status": None, "start": None, "end": None,
                                   "error": "", "executions": []})
        if j.get("event") == "mlops_task_execution":
            name = j.get("execution") or ""
            t["executions"].append({"name": name, "kind": _kind(name),
                                    "input": j.get("input") or "", "start": ts})
        elif j.get("status") == "STARTED":
            t["start"] = t["start"] or ts
            t["status"] = t["status"] or "RUNNING"
        elif j.get("status") in TERMINAL_TASK:
            t["status"], t["end"] = j["status"], ts
            if j.get("result"):
                t["result"] = j["result"]
            if j["status"] == "KEPT":
                t["status"], t["kept"] = (j.get("result") or {}).get("status") or "FAILED", True
            if j["status"] == "FAILED":
                t["error"] = _error_text(j.get("error"))
                # A runner that waited on its child before logging it names a failed child only
                # in the connector's error.
                child = ((j.get("error") or {}).get("operation") or {}).get("name", "") if isinstance(j.get("error"), dict) else ""
                if "/workflows/" in child and not any(x["name"] == child for x in t["executions"]):
                    t["executions"].append({"name": child, "kind": "workflow", "input": "",
                                            "start": (j["error"]["operation"].get("startTime") or t["start"])})
    for key, res in (R.task_results(ex) or {}).items():
        t = tasks.setdefault(key, {"key": key, "status": None, "start": None, "end": None,
                                   "error": "", "executions": []})
        t["status"] = "REUSED" if t["status"] == "REUSED" else res["status"]
        t["outcome"] = res.get("outcome", "")
    # Cancelling a run stops its Workflow before a running task logs how it ended.
    if ex.get("state") == "CANCELLED":
        for t in tasks.values():
            if t["status"] == "RUNNING":
                t["status"], t["end"] = "CANCELLED", ex.get("endTime")
    detail = {**_run_summary(ex), "workflow": workflow, "argument": ex.get("argument"),
              "error": (ex.get("error") or {}).get("payload"), "task_runs": tasks}
    if ex.get("state") in ("SUCCEEDED", "FAILED", "CANCELLED"):
        _FINISHED[(workflow, execution_id)] = detail
        _LIVE.pop((workflow, execution_id), None)
    else:
        _LIVE[(workflow, execution_id)] = (time.monotonic(), detail)
    return detail


#: Finished Cloud Run executions' memory, read once per console instance.
_MEMORY: dict[str, dict] = {}


def memory(gcp, name: str, execution: dict) -> dict:
    """A Cloud Run execution's peak memory against its limit. The job's metrics carry no
    execution label, so this is the job's peak within the execution's window (a job runs one
    execution at a time); an execution under a minute has no samples."""
    if name in _MEMORY:
        return _MEMORY[name]
    limit = ((execution.get("template") or {}).get("containers") or [{}])[0].get("resources", {}).get("limits", {}).get("memory")
    start, end = execution.get("startTime"), execution.get("completionTime")
    out = {"limit": limit, "peak_bytes": None, "peak_fraction": None}
    if start:
        job = name.split("/")[5]
        flt = f'resource.type="cloud_run_job" AND resource.labels.job_name="{job}" AND metric.type="run.googleapis.com/container/memory/'
        stop = end or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        def peak(metric: str) -> float | None:
            vals = [p["value"].get("doubleValue") for ts in gcp.time_series(f'{flt}{metric}"', start, stop, "ALIGN_PERCENTILE_99")
                    for p in ts.get("points", [])]
            vals = [v for v in vals if v is not None]
            return max(vals) if vals else None
        try:
            out |= {"peak_bytes": peak("usage"), "peak_fraction": peak("utilizations")}
        except Exception:  # noqa: BLE001 - Monitoring 5xx at times; the run's page must still load
            return out
    if end:
        _MEMORY[name] = out
    return out


def _attempt(gcp, x: dict) -> dict:
    """One execution a task started, with its own state: a container's, or a child run's
    (and, when the child is a bundle job, the job, so the console links to that run)."""
    if x["kind"] == "workflow":
        wf, ex_id = _workflow_execution(x["name"])
        ex = gcp.execution(wf, ex_id)
        child = _job_of(gcp.workflow(wf))
        return {**x, "state": ex.get("state"), "end": ex.get("endTime"), "workflow": wf,
                "id": ex_id, "job": child["job"] if child else None}
    ex = gcp.run_execution(x["name"])
    if "completionTime" not in ex:
        state = "RUNNING"
    elif ex.get("succeededCount", 0) >= ex.get("taskCount", 1):
        state = "SUCCEEDED"
    else:
        state = "CANCELLED" if ex.get("cancelledCount") else "FAILED"
    return {**x, "state": state, "end": ex.get("completionTime"),
            "id": x["name"].rsplit("/", 1)[-1], "job": None, "workflow": None, "memory": memory(gcp, x["name"], ex)}


def task_run(gcp, workflow: str, execution_id: str, task: str) -> dict:
    """A task of a run, as Databricks' task run page shows it: what it is (its definition),
    how it went, and every execution it started (one per attempt, or per for_each input)."""
    job = _find(gcp, workflow)
    definition = gcp.read_json(job["bucket"], job["definition"])
    if task not in definition["tasks"]:
        raise KeyError(f"{job['job']} has no task {task}")
    node = next(n for n in graph(definition)["nodes"] if n["key"] == task)
    detail = run_detail(gcp, workflow, execution_id)
    t = detail["task_runs"].get(task) or {"key": task, "status": None, "start": None,
                                          "end": None, "error": "", "executions": []}
    run = {k: detail.get(k) for k in ("id", "state", "start", "end", "params", "repair", "skipped", "with_failures",
                                      "run_id", "repair_id", "latest_repair_id")}
    # The task in each attempt of the run, as Databricks' task run history lists it.
    history = []
    for h in detail["repair_history"]:
        if not h["execution"]:
            continue
        x = execution_detail(gcp, workflow, h["execution"])["task_runs"].get(task) or {}
        history.append({"repair_id": h["repair_id"], "execution": h["execution"], "status": x.get("status"),
                        "start": x.get("start"), "end": x.get("end")})
    with ThreadPoolExecutor(8) as pool:
        attempts = list(pool.map(lambda x: _attempt(gcp, x), t["executions"]))
    return {**t, "job": job["job"], "job_name": job["name"], "workflow": workflow, "run": run, "node": node,
            "spec": definition["tasks"][task], "attempts": attempts, "history": history}


#: A Vertex pipeline task's state as the console names states.
_STEP_STATE = {"SUCCEEDED": "SUCCESS", "FAILED": "FAILED", "RUNNING": "RUNNING", "PENDING": "QUEUED",
               "QUEUED": "QUEUED", "NOT_TRIGGERED": "SKIPPED", "SKIPPED": "SKIPPED", "CANCELLED": "CANCELLED",
               "CANCEL_PENDING": "CANCELLED", "CANCELLING": "CANCELLED"}


def _condition_text(cond: str) -> str:
    """inputs.parameter_values['pipelinechannel--gate-op-promote'] == true -> promote == true."""
    return re.sub(r"inputs\.parameter_values\['pipelinechannel--(?:[a-z0-9]+-)*?([a-z0-9_]+)'\]", r"\1", cond)


def pipeline_graph(job: dict) -> dict:
    """A Vertex pipeline run's steps as the GCP pipeline view draws them: every task of its
    spec, a condition's tasks after it, each with its state (a task under a condition that
    didn't hold is SKIPPED), and a level for layout."""
    spec = job.get("pipelineSpec") or {}
    comps = spec.get("components", {})
    details = {t["taskName"]: t for t in (job.get("jobDetail") or {}).get("taskDetails", []) if "taskName" in t}
    nodes, edges = [], []

    def walk(dag: dict, parent: str | None, skipped: bool) -> None:
        for key, t in (dag.get("tasks") or {}).items():
            sub = comps.get(t["componentRef"]["name"], {}).get("dag")
            d = details.get(key) or {}
            state = _STEP_STATE.get(d.get("state", ""), "SKIPPED" if skipped else "QUEUED")
            cond = (t.get("triggerPolicy") or {}).get("condition", "")
            nodes.append({"key": key, "label": (t.get("taskInfo") or {}).get("name") or key,
                          "kind": "condition" if sub is not None else "step",
                          "condition": _condition_text(cond), "state": state, "start": d.get("startTime"),
                          "end": d.get("endTime"), "error": (d.get("error") or {}).get("message", ""),
                          "parent": parent})
            deps = t.get("dependentTasks") or []
            edges.extend({"from": dep, "to": key} for dep in deps)
            if parent and not deps:
                edges.append({"from": parent, "to": key})
            if sub is not None:
                walk(sub, key, skipped or state == "SKIPPED")
    walk((spec.get("root") or {}).get("dag") or {}, None, False)
    into: dict[str, list[str]] = {}
    for e in edges:
        into.setdefault(e["to"], []).append(e["from"])
    level: dict[str, int] = {}

    def depth(k: str) -> int:
        if k not in level:
            level[k] = 1 + max((depth(f) for f in into.get(k, [])), default=-1)
        return level[k]
    for n in nodes:
        n["level"] = depth(n["key"])
    return {"nodes": nodes, "edges": edges}


def notebook_run(gcp, workflow: str, execution_id: str, task: str) -> dict:
    """The notebook execution a notebook_task's run started (named "<job>-<target>-<time>",
    created after the task began) and, once it ran, its executed notebook."""
    t = task_run(gcp, workflow, execution_id, task)
    if task_kind(t["spec"]) != "notebook_task":
        raise ValueError(f"{task} is a {task_kind(t['spec'])}, not a notebook_task")
    arg = t["spec"]["argument"]
    since = t["start"] or t["run"]["start"]
    flt = f'displayName=~"^{re.escape(arg["job"] + "-" + arg["target"])}-[0-9]+$"'
    found = [j for j in gcp.ai_get("notebookExecutionJobs", filter=flt, orderBy="create_time desc", pageSize=20)
             .get("notebookExecutionJobs", []) if not since or (j.get("createTime") or "") >= since[:19]]
    if not found:
        return {"run": None, "notebook": None}
    j = found[-1]
    run_id = j["name"].rsplit("/", 1)[-1]
    state = j.get("jobState", "").removeprefix("JOB_STATE_")
    notebook = None
    if state in ("SUCCEEDED", "FAILED"):
        try:
            notebook = gcp.read_json(arg["bucket"], f'{arg["output_prefix"]}/{run_id}/content.ipynb')
        except Exception:  # noqa: BLE001 - an execution that failed early writes no notebook
            notebook = None
    return {"run": {"name": j["name"], "id": run_id, "state": _STEP_STATE.get(state, state),
                    "start": j.get("createTime"), "end": j.get("updateTime") if state in ("SUCCEEDED", "FAILED", "CANCELLED") else None,
                    "error": (j.get("status") or {}).get("message", ""),
                    "link": f"https://console.cloud.google.com/vertex-ai/colab/execution-jobs?project={gcp.project}"},
            "notebook": notebook}


def pipeline_run(gcp, workflow: str, execution_id: str, task: str) -> dict:
    """The Vertex pipeline run a vertex_pipeline_task's run started (the runner labels it with
    the task's job and target), with its steps; {"run": None} before it starts or when the
    runner skipped it."""
    t = task_run(gcp, workflow, execution_id, task)
    if task_kind(t["spec"]) != "vertex_pipeline_task":
        raise ValueError(f"{task} is a {task_kind(t['spec'])}, not a vertex_pipeline_task")
    arg = t["spec"]["argument"]
    since = t["start"] or t["run"]["start"]
    flt = (f'labels.mlops_job="{arg["job"]}" AND labels.mlops_target="{arg["target"]}"'
           + (f' AND create_time>="{since}"' if since else ""))
    found = gcp.ai_get("pipelineJobs", filter=flt, orderBy="create_time", pageSize=1).get("pipelineJobs") or []
    if not found:
        return {"run": None}
    job = gcp.ai_get(found[0]["name"])
    run_id = job["name"].rsplit("/", 1)[-1]
    return {"run": {"name": job["name"], "id": run_id, "state": _STEP_STATE.get(job.get("state", "").removeprefix("PIPELINE_STATE_"), job.get("state")),
                    "start": job.get("startTime"), "end": job.get("endTime"),
                    "error": (job.get("error") or {}).get("message", ""),
                    "link": f"https://console.cloud.google.com/vertex-ai/pipelines/locations/{gcp.region}/runs/{run_id}?project={gcp.project}"},
            **pipeline_graph(job)}


def _container_logs(gcp, executions: list[dict], since: str | None, n: int) -> list[dict]:
    inputs = {x["name"].rsplit("/", 1)[-1]: x["input"] for x in executions}
    names = " OR ".join(f'"{s}"' for s in inputs)
    flt = (f'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"=({names})'
           + _since(since))
    out = []
    for e in gcp.log_entries(flt, n):
        payload = e.get("textPayload", e.get("jsonPayload"))
        if not payload or not str(payload).strip():
            continue
        text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        short = (e.get("labels") or {}).get("run.googleapis.com/execution_name", "")
        out.append({"time": e.get("timestamp"), "severity": e.get("severity", "DEFAULT"),
                    "execution": short, "input": inputs.get(short, ""), "task": "", "text": text})
    return out


def _workflow_logs(gcp, workflow: str, execution_id: str, since: str | None, n: int) -> list[dict]:
    """A runner Workflow's own log lines (what it logged with sys.log)."""
    flt = (f'resource.type="workflows.googleapis.com/Workflow" AND resource.labels.workflow_id="{workflow}" '
           f'AND labels.execution_id="{execution_id}"' + _since(since))
    out = []
    for e in gcp.log_entries(flt, n):
        payload = e.get("jsonPayload")
        text = e.get("textPayload") if "textPayload" in e else json.dumps(payload, separators=(",", ":"))
        out.append({"time": e.get("timestamp"), "severity": e.get("severity", "DEFAULT"),
                    "execution": execution_id, "input": "", "task": "", "text": text})
    return out


def task_logs(gcp, workflow: str, execution_id: str, task: str, n: int = 1000) -> list[dict]:
    """What a task of a run printed, oldest first: its containers' output; for a task that ran
    a bundle job, that run's tasks' output (each line names its task); for one that ran a
    runner Workflow (vertex_pipeline, batch_serve), what that Workflow logged."""
    detail = execution_detail(gcp, workflow, execution_id)
    t = detail["task_runs"].get(task)
    if not t or not t["executions"]:
        return []
    since = t["start"] or detail["start"]
    containers = [x for x in t["executions"] if x["kind"] == "cloud_run"]
    out = _container_logs(gcp, containers, since, n) if containers else []
    for x in t["executions"]:
        if x["kind"] != "workflow":
            continue
        wf, child_id = _workflow_execution(x["name"])
        if _job_of(gcp.workflow(wf)) is None:
            out += [{**line, "input": x["input"]} for line in _workflow_logs(gcp, wf, child_id, since, n)]
            continue
        for key in execution_detail(gcp, wf, child_id)["task_runs"]:
            out += [{**line, "task": f"{key} / {line['task']}" if line["task"] else key,
                     "input": line["input"] or x["input"]}
                    for line in task_logs(gcp, wf, child_id, key, n)]
    return sorted(out, key=lambda line: line["time"] or "")


# --- actions: each one audited with the signed-in user ------------------------------------

def _audit(gcp, user: str, action: str, workflow: str, **detail) -> None:
    gcp.audit({"event": "mlops_console_action", "user": user, "action": action,
               "workflow": workflow, **detail})


def run_now(gcp, user: str, workflow: str, params: dict[str, str], only: list[str] | None = None) -> dict:
    """Starts a run with these parameters; `only`: of these tasks alone (Databricks' run-now only)."""
    job = _find(gcp, workflow)
    if job["retired"]:
        raise ValueError(f"{job['name']} is retired: its bundle no longer declares it, so it doesn't run")
    definition = gcp.read_json(job["bucket"], job["definition"])
    unknown = sorted(set(params) - set(definition.get("parameters", {})))
    if unknown:
        raise ValueError(f"{job['job']} has parameters {sorted(definition.get('parameters', {}))}, not {unknown}")
    stray = sorted(set(only or []) - set(definition["tasks"]))
    if stray:
        raise ValueError(f"{job['job']} has no task(s) {stray}")
    if only and not job["runs_some"]:
        raise ValueError(f"{workflow} was deployed before a run could pick its tasks, so it would run them all; "
                         "redeploy its bundle first")
    argument = {**({"params": params} if params else {}), **({"only": {k: True for k in only}} if only else {})}
    started = gcp.start(workflow, argument)
    _audit(gcp, user, "run", workflow, execution=started.get("name"), params=params, only=sorted(only or []))
    return {"execution": started["name"].rsplit("/", 1)[-1]}


def _child(gcp):
    return lambda name: gcp.execution(*_workflow_execution(name))


def repair_plan(gcp, workflow: str, execution_id: str) -> dict:
    """What a repair of the run can change, from its latest attempt: {models: {task_key:
    [{model, status}]}}, its batch_serve_tasks' models (a FAILED one resumes; any can run
    again in full), and {parameters: the job's defaults, original: the run's own values,
    overrides: what its repairs override}, with the latest_repair_id to repair. `only`: the
    tasks a run of some tasks ran ([]: all); a repair leaves the rest out again."""
    job = _find(gcp, workflow)
    run_id, _ = _run_of(gcp, workflow, execution_id)
    hist = [h for h in R.history(gcp, job, run_id) if h["execution"]]
    latest = hist[-1]
    latest_ex = gcp.execution(workflow, latest["execution"])
    served = R.serve_runs(latest_ex, _child(gcp))
    pipelines = R.pipeline_runs(latest_ex, _child(gcp), _children(gcp, workflow, latest["execution"]))
    definition = gcp.read_json(job["bucket"], job["definition"])
    return {"models": {k: [{"model": m["model"], "status": m["status"]} for m in v["models"]]
                       for k, v in served.items()},
            # Its steps as the pipeline ran them: a repair reruns the failed ones in Vertex.
            "pipelines": {k: {"pipeline_job": name, "steps": _pipeline_steps(gcp, name)} for k, name in pipelines.items()},
            "parameters": definition.get("parameters", {}),
            "original": R.run_params(gcp.execution(workflow, run_id)),
            "overrides": latest.get("overrides") or {}, "latest_repair_id": latest["repair_id"],
            "only": sorted(json.loads(latest_ex.get("argument") or "{}").get("only") or {}),
            "execution": latest["execution"]}


def _children(gcp, workflow: str, execution_id: str) -> dict[str, str]:
    """{task_key: the child run it started} from the run's logs, for runs whose results don't name it."""
    return {k: t["executions"][-1]["name"] for k, t in execution_detail(gcp, workflow, execution_id)["task_runs"].items()
            if t["executions"] and t["executions"][-1]["kind"] == "workflow"}


def _logged_results(gcp, job: dict, workflow: str, execution_id: str) -> dict[str, dict]:
    """A cancelled run's {task_key: {status, outcome}} from its logs, as Databricks repairs one:
    a task that succeeded is reused with the result it logged (a runner older than that logged
    none, so it runs again), and every other task, cancelled or never started, runs again."""
    ran = execution_detail(gcp, workflow, execution_id)["task_runs"]
    out = {}
    for key in gcp.read_json(job["bucket"], job["definition"])["tasks"]:
        t = ran.get(key) or {}
        status = t.get("status")
        if status in ("SUCCESS", "REUSED") and t.get("result"):
            out[key] = t["result"]
        else:
            out[key] = {"status": "CANCELLED" if status in (None, "SUCCESS", "REUSED") else status, "outcome": ""}
    return out


def _pipeline_steps(gcp, name: str) -> list[dict]:
    """A Vertex pipeline run's steps (not the run's own root task), each with its state."""
    root = name.rsplit("/", 1)[-1]
    return [{"name": t["taskName"], "state": t.get("state", "")}
            for t in (gcp.ai_get(name).get("jobDetail") or {}).get("taskDetails", []) if t.get("taskName") != root]


def repair(gcp, user: str, workflow: str, execution_id: str, models: list[str] | None = None,
           tasks: list[str] | None = None, params: dict[str, str | None] | None = None,
           latest_repair_id: int | None = None, steps: dict[str, list[str]] | None = None,
           fresh: list[str] | None = None, keep: list[str] | None = None) -> dict:
    """Databricks' repair run: a new attempt of the run (runs.start_repair)."""
    job = _find(gcp, workflow)
    run_id, _ = _run_of(gcp, workflow, execution_id)
    latest = [h for h in R.history(gcp, job, run_id) if h["execution"]][-1]
    logged = (_logged_results(gcp, job, workflow, latest["execution"])
              if gcp.execution(workflow, latest["execution"]).get("state") == "CANCELLED" else None)
    started = R.start_repair(gcp, job, execution_id, user=user, latest_repair_id=latest_repair_id,
                             tasks=tasks, models=models, params=params, child=_child(gcp), steps=steps,
                             fresh=fresh, children=_children(gcp, workflow, latest["execution"]), logged=logged,
                             keep=keep)
    argument = started["argument"]
    reused = sorted(k for k, v in argument["repair"].items() if v.get("status") == "SUCCESS")
    kept = sorted(keep or [])
    repaired = R.repaired_models(argument)
    _audit(gcp, user, "repair", workflow, of=started["run_id"], repair_id=started["repair_id"],
           execution=started["execution"], reused=reused, kept=kept, models=repaired, params=argument.get("params", {}))
    return {"run_id": started["run_id"], "repair_id": started["repair_id"], "execution": started["execution"],
            "reused": reused, "models": repaired, "pipelines": R.repaired_pipelines(argument)}


def cancel(gcp, user: str, workflow: str, execution_id: str) -> dict:
    """Cancels the run and every Cloud Run execution it started that is still going:
    cancelling a Workflow alone leaves its containers running."""
    _find(gcp, workflow)
    detail = execution_detail(gcp, workflow, execution_id)
    if detail["state"] == "ACTIVE":
        gcp.cancel(workflow, execution_id)
    stopped = []
    for t in detail["task_runs"].values():
        for x in t["executions"]:
            if x["kind"] == "workflow":
                wf, child_id = _workflow_execution(x["name"])
                if gcp.execution(wf, child_id).get("state") in ("ACTIVE", "QUEUED"):
                    gcp.cancel(wf, child_id)
                    stopped.append(child_id)
            elif "completionTime" not in gcp.run_execution(x["name"]):
                gcp.cancel_run_execution(x["name"])
                stopped.append(x["name"].rsplit("/", 1)[-1])
    _audit(gcp, user, "cancel", workflow, execution=execution_id, containers=stopped)
    return {"cancelled": execution_id, "containers": stopped}


def delete_job(gcp, user: str, workflow: str) -> dict:
    """Deletes a retired job: first from its bundle's deployment record, so no later deploy
    keeps it, then its Workflow (and with it its runs) and its definition. A job with a run
    still going stays."""
    job = _find(gcp, workflow)
    if not job["retired"]:
        raise ValueError(f"{job['name']} is not retired: only a job its bundle no longer declares can be deleted")
    if any(e.get("state") == "ACTIVE" for e in gcp.executions(workflow, 20)):
        raise ValueError(f"{job['name']} has a run still going: cancel it or let it finish first")
    record_obj = f"{storage_prefix(job['target'])}.bundle/{job['bundle']}/deployment.json"
    got = gcp.read_object(job["bucket"], record_obj)
    if got is not None:
        record, generation = json.loads(got[0]), got[1]
        if (record.get("retired_jobs") or {}).pop(job["job"], None) is not None:
            # Over the generation read: a deploy that wrote it since makes this fail, not undo it.
            gcp.write_object(job["bucket"], record_obj, json.dumps(record, indent=2, sort_keys=True).encode(),
                             "application/json", if_generation=generation)
    gcp.delete_workflow(workflow)
    gcp.delete_object(job["bucket"], job["definition"])
    _audit(gcp, user, "delete_job", workflow, job=job["job"])
    return {"deleted": workflow}


def set_paused(gcp, user: str, workflow: str, paused: bool) -> dict:
    """Pauses or resumes the job's schedule and trigger, whichever it has."""
    job = _find(gcp, workflow)
    scheds = _schedulers(gcp)
    changed = [n for n in scheduler_names(job).values() if n in scheds]
    for n in changed:
        gcp.set_scheduler(n, paused)
    if not changed:
        raise ValueError(f"{job['job']} has no schedule or trigger to {'pause' if paused else 'resume'}")
    _audit(gcp, user, "pause" if paused else "resume", workflow, schedulers=changed)
    return {"paused": paused, "schedulers": changed}

