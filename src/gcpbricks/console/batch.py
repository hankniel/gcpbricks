"""The console's Batch serving: every bundle job's batch_serve_task, read from the job's
deployed definition, and its job's runs (each run's error names the models that failed)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from gcpbricks.console import service as S

#: How many of the job's runs a task's page lists.
RUNS_LIMIT = 25


def _serve_tasks(definition: dict) -> dict[str, dict]:
    """{task key: its batch_serve argument}."""
    return {k: t["argument"] for k, t in definition["tasks"].items() if S.task_kind(t) == "batch_serve_task"}


def _summary(job: dict, task: str, arg: dict) -> dict:
    return {"workflow": job["workflow"], "job": job["job"], "bundle": job["bundle"], "target": job["target"],
            "task": task, "models": arg["models"], "window_days": arg["window_days"]}


def tasks(gcp) -> list[dict]:
    """Every batch_serve_task of every deployed bundle job, with its job's schedule and last run."""
    jobs = S.list_jobs(gcp, None, 1)
    with ThreadPoolExecutor(8) as pool:
        defs = list(pool.map(lambda j: gcp.read_json(j["bucket"], j["definition"]), jobs))
    return [{**_summary(j, k, arg), "schedule": j["schedule"], "trigger": j["trigger"], "last_run": (j["runs"] or [None])[0]}
            for j, d in zip(jobs, defs) for k, arg in _serve_tasks(d).items()]


def task(gcp, workflow: str, task_key: str) -> dict:
    """One batch_serve_task: its models, settings and its job's latest runs."""
    job = S._find(gcp, workflow)
    arg = _serve_tasks(gcp.read_json(job["bucket"], job["definition"])).get(task_key)
    if arg is None:
        raise KeyError(f"{job['job']} has no batch_serve_task {task_key}")
    settings = {k: arg.get(k) for k in ("window_days", "max_hours", "utc_offset_hours", "region")}
    runs = [S._run_summary(e) for e in gcp.executions(workflow, RUNS_LIMIT)]
    return {**_summary(job, task_key, arg), "settings": settings, "runs": runs}
