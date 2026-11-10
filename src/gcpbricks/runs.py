"""What a job run's Workflow execution says about itself, for the CLI and the console."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import requests


def task_results(execution: dict) -> dict[str, dict] | None:
    """{task_key: {status, outcome}} from a finished run (workflows/job.yaml returns them, or
    raises them with a failure); None for a run that ended before its tasks ran."""
    try:
        if execution.get("state") == "SUCCEEDED":
            return json.loads(execution.get("result") or "{}").get("tasks")
        return json.loads(execution["error"]["payload"]).get("tasks")
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def run_params(execution: dict) -> dict:
    return json.loads(execution.get("argument") or "{}").get("params") or {}


def serve_summary(child: dict) -> dict | None:
    """A batch_serve_task's run as workflows/batch_serve.yaml reports it, {run_id, started_ms,
    models: [{model, from_date, to_date, run_id, status}]}; None for any other run, or one
    that ended before it said (cancelled, timed out, or deployed before it did)."""
    try:
        if child.get("state") == "SUCCEEDED":
            out = json.loads(child.get("result") or "{}")
        else:
            out = json.loads(child["error"]["payload"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return out if isinstance(out, dict) and {"run_id", "started_ms", "models"} <= out.keys() else None


def serve_runs(execution: dict, child) -> dict[str, dict]:
    """{task_key: serve_summary} for the run's batch_serve_tasks; child(name) reads a run."""
    out = {}
    for key, res in (task_results(execution) or {}).items():
        name = res.get("execution") or ""
        if "/workflows/" in name and (summary := serve_summary(child(name))):
            out[key] = summary
    return out


_PIPELINE_JOB = re.compile(r"projects/[^/\s\"]+/locations/[^/\s\"]+/pipelineJobs/[^\s\"\\]+")


def pipeline_runs(execution: dict, child, children: dict[str, str] | None = None) -> dict[str, str]:
    """{task_key: the Vertex pipeline run} of the run's vertex_pipeline_tasks, as their runner
    (workflows/vertex_pipeline.yaml) names it in its result or failure. children: task_key ->
    the runner's execution, for a run whose results don't carry it (an older runner)."""
    out = {}
    for key, res in (task_results(execution) or {}).items():
        name = res.get("execution") or (children or {}).get(key, "")
        if not name.split("/workflows/")[-1].split("/")[0].endswith("-vertex-pipeline"):
            continue
        ex = child(name)
        m = _PIPELINE_JOB.search(((ex.get("error") or {}).get("payload") or "") + (ex.get("result") or ""))
        if m:
            out[key] = m.group(0)
    return out


def repair_argument(execution: dict, models: list[str] | None = None, child=None,
                    tasks: list[str] | None = None, steps: dict[str, list[str]] | None = None,
                    fresh_pipelines: list[str] | None = None, children: dict[str, str] | None = None,
                    logged: dict[str, dict] | None = None, keep: list[str] | None = None) -> dict:
    """A repair of a failed or cancelled run, or of one that succeeded with failures (a task failed, but no leaf
    did): its parameters, and its tasks that succeeded, to reuse, but for
    `tasks`, which run again (as Databricks' repair picks tasks). A batch_serve_task runs again
    only its failed models, resuming them (the run's run_id, start and windows, so their jobs
    skip the chunks they finished), and `models` again in full, whether they failed or not; one
    in `tasks` without any of `models` runs every model again in full. A vertex_pipeline_task
    that failed reruns its pipeline as a Vertex rerun (its steps that succeeded reused), plus
    its `steps` asked again; one in `fresh_pipelines`, or in `tasks` without `steps`, starts a new
    pipeline. child(name) reads a task's run; without it a batch_serve_task that failed runs
    every model again, from the start, and a vertex_pipeline_task a new pipeline. `logged`: the
    task results its logs hold, for a cancelled run, whose execution returns none. `keep`: tasks
    that didn't succeed, not to run again but to end as they did, as Databricks' repair runs only
    the tasks picked (its dependents still see it failed)."""
    results = task_results(execution) or logged
    with_failures = execution.get("state") == "SUCCEEDED" and any(
        v.get("status") in ("FAILED", "UPSTREAM_FAILED") for v in (results or {}).values())
    if execution.get("state") not in ("FAILED", "CANCELLED") and not with_failures:
        raise ValueError("only a FAILED or CANCELLED run, or one that succeeded with failures, can be repaired; "
                         f"this one is {execution.get('state')}")
    if results is None:
        raise ValueError("this run failed before its tasks ran (no task results); run the job again")
    rerun = set(tasks or ())
    if rerun - set(results):
        raise ValueError(f"this run has no task(s) {sorted(rerun - set(results))}")
    kept = set(keep or ())
    if bad := sorted(k for k in kept if k not in results or results[k].get("status") == "SUCCESS" or k in rerun):
        raise ValueError(f"only a task that didn't succeed, and isn't run again, can be kept as it ended: {bad}")
    params = run_params(execution)
    only = json.loads(execution.get("argument") or "{}").get("only")
    reuse = {k: v for k, v in results.items() if v.get("status") == "SUCCESS" and k not in rerun}
    asked, found = set(models or ()), set()
    for key, summary in (serve_runs(execution, child) if child else {}).items():
        names = {m["model"] for m in summary["models"]}
        fresh = (asked & names) or (names if key in rerun else set())
        failed = results[key].get("status") != "SUCCESS"
        resume = {m["model"] for m in summary["models"] if failed and m["status"] == "FAILED"} - fresh
        found |= asked & names
        if not fresh and not resume:
            continue
        reuse[key] = {"status": "REPAIR", "outcome": "", "argument": {"repair": {
            "run_id": summary["run_id"], "started_ms": summary["started_ms"],
            "models": {m["model"]: {k: m[k] for k in ("from_date", "to_date", "run_id", "run_date", "backfill")
                                    if k in m}
                       for m in summary["models"]},
            "resume": sorted(resume), "fresh": sorted(fresh)}}}
    if asked - found:
        raise ValueError(f"no batch_serve_task of this run served {sorted(asked - found)}")
    pipelines = pipeline_runs(execution, child, children) if child else {}
    if stray := sorted(set(steps or {}) - set(pipelines)):
        raise ValueError(f"no vertex_pipeline_task of this run ran a pipeline for {stray}")
    for key, job in pipelines.items():
        again = sorted((steps or {}).get(key, []))
        failed = results[key].get("status") != "SUCCESS"
        if key in (fresh_pipelines or ()) or not (failed or again) or (key in rerun and not again):
            continue
        reuse[key] = {"status": "REPAIR", "outcome": "", "argument": {"repair": {"pipeline_job": job, "rerun": again}}}
    reuse |= {k: results[k] for k in kept}
    return {**({"params": params} if params else {}), **({"only": only} if only else {}), "repair": reuse}


def repaired_models(argument: dict) -> dict[str, dict]:
    """{task_key: {resume, fresh}} of a repair_argument's batch_serve_tasks."""
    return {k: {"resume": v["argument"]["repair"]["resume"], "fresh": v["argument"]["repair"]["fresh"]}
            for k, v in argument["repair"].items() if "argument" in v and "resume" in v["argument"]["repair"]}


def repaired_pipelines(argument: dict) -> dict[str, dict]:
    """{task_key: {pipeline_job, rerun}} of a repair_argument's vertex_pipeline_tasks rerun in Vertex."""
    return {k: v["argument"]["repair"] for k, v in argument["repair"].items()
            if "argument" in v and "pipeline_job" in v["argument"]["repair"]}


# --- a run and its repairs, as Databricks keeps them --------------------------------------
#
# A job run is its first execution; a repair is another execution of the job's Workflow whose
# argument names the run (repair_of) and its number (repair_id: 1, 2, ...), so the runner
# gives every attempt the run's {{job.run_id}}. The run's record, next to the job's
# definition, lists its repairs in order: the original execution can't point at them.

def run_of(execution: dict) -> tuple[str, int]:
    """(run_id, repair_id) of an execution: a repair's run and number, else its own id and 0."""
    arg = json.loads(execution.get("argument") or "{}")
    return arg.get("repair_of") or execution["name"].rsplit("/", 1)[-1], int(arg.get("repair_id") or 0)


def record_object(definition: str, run_id: str) -> str:
    """gs://<bucket>/<...>/jobs/<job>.runs/<run_id>.json, beside jobs/<job>.json."""
    return f"{definition.removesuffix('.json')}.runs/{run_id}.json"


def _record(gcp, job: dict, run_id: str) -> tuple[dict, str]:
    """The run's record and its generation ("0": none yet)."""
    got = gcp.read_object(job["bucket"], record_object(job["definition"], run_id))
    if got is None:
        return {"run_id": run_id, "repairs": []}, "0"
    return json.loads(got[0]), got[1]


def history(gcp, job: dict, run_id: str) -> list[dict]:
    """The run's attempts, original first: [{type, repair_id, execution, by, time,
    overrides, tasks, models}]; a repair still starting has no execution yet."""
    record, _ = _record(gcp, job, run_id)
    return [{"type": "ORIGINAL", "repair_id": 0, "execution": run_id},
            *({"type": "REPAIR", **r} for r in record["repairs"])]


def start_repair(gcp, job: dict, execution_id: str, *, user: str = "", latest_repair_id: int | None = None,
                 tasks: list[str] | None = None, models: list[str] | None = None,
                 params: dict[str, str | None] | None = None, child=None,
                 steps: dict[str, list[str]] | None = None, fresh: list[str] | None = None,
                 children: dict[str, str] | None = None, logged: dict[str, dict] | None = None,
                 keep: list[str] | None = None) -> dict:
    """Repairs a run (Databricks' runs/repair): its latest attempt's tasks that didn't succeed
    run again in a new attempt, with `tasks`, `models`, `steps`, `fresh`, `children` and
    `logged` and `keep` as repair_argument takes them.
    `params` changes the repair's parameter overrides, which later repairs keep: a value
    overrides the run's own, None drops the override. `latest_repair_id`, when given, must be
    the run's latest repair (0: none), so a repair never builds on an attempt it didn't see.
    `execution_id` is the run's or any of its attempts'."""
    wf = job["workflow"]
    run_id, _ = run_of(gcp.execution(wf, execution_id))
    record, generation = _record(gcp, job, run_id)
    latest = record["repairs"][-1] if record["repairs"] else None
    latest_id = latest["repair_id"] if latest else 0
    if latest_repair_id is not None and latest_repair_id != latest_id:
        raise ValueError(f"run {run_id}'s latest repair is {latest_id}, not {latest_repair_id}: "
                         "look at it again before repairing")
    if latest and not latest.get("execution"):
        raise ValueError(f"repair {latest_id} of run {run_id} is still starting")
    previous = gcp.execution(wf, latest["execution"] if latest else run_id)
    if previous.get("state") == "ACTIVE":
        raise ValueError(f"run {run_id} is still running (repair {latest_id}): repair it once it ends")
    known = set(gcp.read_json(job["bucket"], job["definition"]).get("parameters", {}))
    if unknown := sorted(set(params or {}) - known):
        raise ValueError(f"{job['job']} has parameters {sorted(known)}, not {unknown}")
    overrides = dict(latest.get("overrides") or {}) if latest else {}
    for k, v in (params or {}).items():
        if v is None:
            overrides.pop(k, None)
        else:
            overrides[k] = v
    argument = repair_argument(previous, models, child, tasks, steps, fresh, children, logged, keep)
    root = previous if not latest else gcp.execution(wf, run_id)
    merged = {**run_params(root), **overrides}
    argument = {**{k: v for k, v in argument.items() if k != "params"}, **({"params": merged} if merged else {}),
                "repair_of": run_id, "repair_id": latest_id + 1}
    entry = {"repair_id": latest_id + 1, "execution": "", "by": user,
             "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "overrides": overrides,
             "tasks": sorted(tasks or []), "models": sorted(models or []),
             **({"steps": {k: sorted(v) for k, v in steps.items()}} if steps else {}),
             **({"fresh": sorted(fresh)} if fresh else {})}
    record["repairs"].append(entry)
    generation = _save(gcp, job, record, generation)
    try:
        started = gcp.start(wf, argument)
    except Exception:
        record["repairs"].pop()
        _save(gcp, job, record, generation)
        raise
    entry["execution"] = started["name"].rsplit("/", 1)[-1]
    _save(gcp, job, record, generation)
    return {"run_id": run_id, "repair_id": entry["repair_id"], "execution": entry["execution"],
            "argument": argument}


def _save(gcp, job: dict, record: dict, generation: str) -> str:
    """Writes the record over the generation read; another repair's write in between refuses."""
    try:
        return gcp.write_object(job["bucket"], record_object(job["definition"], record["run_id"]),
                                json.dumps(record, indent=1).encode(), "application/json", generation)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 412:
            raise ValueError(f"run {record['run_id']} was repaired by someone else just now: look at it again") from None
        raise
