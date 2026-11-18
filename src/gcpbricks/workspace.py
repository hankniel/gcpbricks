"""The workspace as the console sees it, from anywhere, as you: what the `mlops` CLI's
Databricks-style groups (jobs, clusters, tables, registered-models, ...) and the MCP server
call. Every read and write is the console's own backend function, on your Application Default
Credentials (no console, no IAP): GCP's IAM decides what you may change, as in the console.

`resolve` turns a console link (or a Workflows / Vertex link, or a resource name) into what it
names; `inspect` answers "what is this, and what went wrong" for it in one call: a failed run's
failure chain down to the log line, a task's logs, a table's schema and monitors, a model
version's training."""
from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, unquote, urlparse

PROJECT = os.environ.get("MLOPS_PROJECT", "my-gcp-project")
REGION = os.environ.get("MLOPS_REGION", "us-central1")
#: Log lines `inspect` returns for a task or a failure.
TAIL = 40


def client():
    """The console's GCP client on your Application Default Credentials."""
    from gcpbricks.console.gcp import Gcp

    return Gcp(PROJECT, REGION)


def whoami() -> str:
    """Your Google account (for audit records and owned runtimes), else the local user."""
    if os.environ.get("MLOPS_USER"):
        return os.environ["MLOPS_USER"]
    try:
        import google.auth
        import requests
        from google.auth.transport.requests import Request

        creds, _ = google.auth.default(scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"])
        creds.refresh(Request())
        email = requests.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": creds.token},
                             timeout=30).json().get("email", "")
    except Exception:  # noqa: BLE001 - who you are is a label here, not a permission
        email = ""
    return email or os.environ.get("USER", "")


# --- links ----------------------------------------------------------------------------------

_CONSOLE = [
    (r"^/jobs/(?P<workflow>[^/]+)/runs/(?P<run>[^/]+)/tasks/(?P<task>[^/]+)$", "task"),
    (r"^/jobs/(?P<workflow>[^/]+)/runs/(?P<run>[^/]+)$", "run"),
    (r"^/jobs/(?P<workflow>[^/]+)$", "job"),
    (r"^/catalog/(?P<dataset>[^/]+)/(?P<table>[^/]+)$", "table"),
    (r"^/catalog/(?P<dataset>[^/]+)$", "dataset"),
    (r"^/models/(?P<model>[^/]+)/versions/(?P<version>[^/]+)$", "model_version"),
    (r"^/models/(?P<model>[^/]+)$", "model"),
    (r"^/experiments/(?P<experiment>[^/]+)(?:/runs/(?P<run>[^/]+))?$", "experiment"),
    (r"^/serving/(?P<endpoint>[^/]+)$", "endpoint"),
    (r"^/batch-serving/(?P<workflow>[^/]+)/(?P<task>[^/]+)$", "batch_task"),
    (r"^/features/(?P<name>[^/]+)$", "feature_group"),
    (r"^/bundles/(?P<bundle>[^/]+)$", "bundle"),
    (r"^/quality/(?P<bundle>[^/]+)/(?P<target>[^/]+)/(?P<key>[^/]+)$", "monitor"),
    (r"^/dashboards/(?P<bundle>[^/]+)/(?P<target>[^/]+)/(?P<key>[^/]+)$", "dashboard"),
]
_WORKFLOW_EXEC = re.compile(r"workflows/(?:workflow/[^/]+/)?(?P<workflow>[a-z0-9_-]+)/(?:execution|executions)/(?P<run>[a-f0-9-]{36})")
_WORKFLOW = re.compile(r"workflows/workflow/[^/]+/(?P<workflow>[a-z0-9_-]+)(?:/|$|\?)")
_PIPELINE = re.compile(r"(?:pipelineJobs/|pipelines/runs/|locations/[^/]+/runs/)(?P<pipeline>[a-z0-9-]+)")
_BQ_JOB = re.compile(r"(?P<project>[a-z][a-z0-9-]*):(?P<location>[A-Za-z0-9-]+)\.(?P<job>[A-Za-z0-9_-]+)$")


def resolve(link: str) -> dict:
    """{kind, ...ids} for a console link (any host: /jobs/..., /catalog/..., /models/...), a GCP
    console Workflows or Vertex pipeline link, a `projects/.../workflows/W/executions/E` name, or
    a BigQuery job id `project:location.job_id`."""
    link = link.strip()
    u = urlparse(link)
    path = unquote(u.path).rstrip("/")
    query = {k: v[0] for k, v in parse_qs(u.query).items()}
    for pattern, kind in _CONSOLE:
        m = re.match(pattern, path)
        if m and "console.cloud.google.com" not in (u.netloc or ""):
            out = {"kind": kind, **{k: v for k, v in m.groupdict().items() if v is not None}}
            if kind == "table" and query.get("project"):
                out["project"] = query["project"]
            return out
    if m := _WORKFLOW_EXEC.search(link):
        return {"kind": "run", **m.groupdict()}
    if m := _WORKFLOW.search(link):
        return {"kind": "job", **m.groupdict()}
    if m := _PIPELINE.search(link):
        return {"kind": "pipeline", **m.groupdict()}
    if m := _BQ_JOB.match(link):
        return {"kind": "bigquery_job", **m.groupdict()}
    if re.match(r"^[\w-]+\.\w+\.\w+$", link):
        p, d, t = link.split(".")
        return {"kind": "table", "project": p, "dataset": d, "table": t}
    raise ValueError(f"not a link this knows: {link!r} (a console page, a Workflows execution, a Vertex "
                     "pipeline run, a BigQuery job id or project.dataset.table)")


# --- what inspect returns -------------------------------------------------------------------

def _job(j: dict) -> dict:
    return {k: j.get(k) for k in ("workflow", "job", "name", "bundle", "target", "retired", "schedule", "trigger")}


def _run(r: dict) -> dict:
    keep = ("id", "run_id", "repair_id", "latest_repair_id", "state", "start", "end", "params", "error_message")
    out = {k: r.get(k) for k in keep if k in r}
    if "task_runs" in r:
        out["tasks"] = {k: {x: t.get(x) for x in ("status", "start", "end", "error") if t.get(x)}
                        for k, t in r["task_runs"].items()}
    return out


def _logs(lines: list[dict], n: int = TAIL) -> list[str]:
    return [f"{(e.get('time') or '')[:19]} {e.get('severity') or '':<8} "
            f"{('[' + e['task'] + '] ') if e.get('task') else ''}{(e.get('text') or '').rstrip()}" for e in lines[-n:]]


def why(gcp, workflow: str, run: str, task: str | None = None) -> dict:
    from gcpbricks.console import failure as F

    w = F.why(gcp, workflow, run, task)
    return {"path": w["path"], "error": w["error"], "code": w.get("code"),
            "skipped": w.get("skipped") or [], "logs": _logs(w["logs"])}


def _failed(state: str | None) -> bool:
    return state in ("FAILED", "UPSTREAM_FAILED", "CANCELLED", "TIMEDOUT", "INTERNAL_ERROR")


def inspect(gcp, link: str) -> dict:
    """What `link` names and its state, with what an agent asks next answered: a failed run or
    task's failure chain and log tail, a table's schema and monitors, a model version's
    training. One call, the console's own reads."""
    from gcpbricks.console import catalog as C
    from gcpbricks.console import ml as M
    from gcpbricks.console import quality as Q
    from gcpbricks.console import service as S

    r = resolve(link)
    kind = r["kind"]
    out: dict = {"kind": kind, "ref": r}
    if kind == "job":
        j = S.job_detail(gcp, r["workflow"], 10)
        out |= {"job": _job(j), "parameters": j.get("parameters"), "tasks": sorted(j["tasks"]),
                "runs": [_run(x) for x in j["runs"]]}
    elif kind == "run":
        d = S.run_detail(gcp, r["workflow"], r["run"])
        out["run"] = _run(d)
        if _failed(d.get("state")) or any(_failed(t.get("status")) for t in d["task_runs"].values()) \
                or d.get("state") == "SUCCEEDED":
            out["why"] = why(gcp, r["workflow"], r["run"])
    elif kind == "task":
        t = S.task_run(gcp, r["workflow"], r["run"], r["task"])
        out["task"] = {k: t.get(k) for k in ("key", "status", "start", "end", "error", "job", "workflow")}
        out["task"]["kind"] = S.task_kind(t["spec"])
        out["run"] = t["run"]
        if _failed(t.get("status")):
            out["why"] = why(gcp, r["workflow"], r["run"], r["task"])
        out["logs"] = _logs(S.task_logs(gcp, r["workflow"], r["run"], r["task"]))
    elif kind == "table":
        t = C.table(gcp, r["dataset"], r["table"], r.get("project"))
        out["table"] = {k: t.get(k) for k in ("full_name", "type", "location", "description", "rows", "bytes",
                                               "created", "updated", "partitioning", "clustering", "view_query")}
        out["schema"] = [{"name": c["name"], "type": c["type"], "mode": c.get("mode"),
                          **({"description": c["description"]} if c.get("description") else {})} for c in t["schema"]]
        out["monitors"] = [{k: m.get(k) for k in ("bundle", "target", "key", "profile_type", "workflow")}
                           for m in Q.monitors(gcp, r["dataset"], r["table"], r.get("project"))]
    elif kind == "dataset":
        out["dataset"] = C.dataset(gcp, r["dataset"])
    elif kind == "model":
        out["model"] = M.model(gcp, r["model"])
    elif kind == "model_version":
        out["model"] = {k: v for k, v in M.model(gcp, r["model"]).items() if k != "versions"}
        out["training"] = M.version_training(gcp, r["model"], r["version"])
    elif kind == "experiment":
        out["experiment"] = M.experiment(gcp, r["experiment"])
    elif kind == "endpoint":
        out["endpoint"] = M.endpoint(gcp, r["endpoint"])
    elif kind == "batch_task":
        from gcpbricks.console import batch as B
        out["batch_task"] = B.task(gcp, r["workflow"], r["task"])
    elif kind == "feature_group":
        out["feature_group"] = M.feature_group(gcp, r["name"])
    elif kind == "bundle":
        from gcpbricks.console import bundles as BN
        out["bundle"] = BN.bundle(gcp, r["bundle"])
    elif kind == "monitor":
        out["monitor"] = Q.monitor(gcp, r["bundle"], r["target"], r["key"])
        out["refreshes"] = [_run(x) for x in S.job_detail(gcp, out["monitor"]["workflow"], 5)["runs"]]
    elif kind == "dashboard":
        from gcpbricks.console import dashboards as D
        out["dashboard"] = D.dashboard(gcp, r["bundle"], r["target"], r["key"])
    elif kind == "pipeline":
        job = gcp.ai_get(f"pipelineJobs/{r['pipeline']}")
        failed = [{"step": S.step_ref(d), "state": d.get("state"), "error": (d.get("error") or {}).get("message")}
                  for d in (job.get("jobDetail") or {}).get("taskDetails", []) if d.get("state") == "FAILED"]
        out["pipeline"] = {"name": job.get("name"), "state": job.get("state"), "error": (job.get("error") or {}).get("message"),
                           "start": job.get("startTime"), "end": job.get("endTime"), "failed_steps": failed,
                           "labels": job.get("labels")}
    elif kind == "bigquery_job":
        from gcpbricks.console import stats as ST
        out["bigquery_job"] = ST.bigquery_job(gcp, r["project"], r["location"], r["job"])
    return out


def workflow_of(arg: str) -> str:
    """A job argument: its Workflow name, or a console / Workflows link to it."""
    if "/" in arg:
        r = resolve(arg)
        if "workflow" not in r:
            raise ValueError(f"{arg!r} is a {r['kind']}, not a job")
        return r["workflow"]
    return arg


def run_of(job: str, run: str | None) -> tuple[str, str]:
    """(workflow, run id) from a run link alone, or a job and a run id."""
    if run is None and "/" in job:
        r = resolve(job)
        if "run" not in r:
            raise ValueError(f"{job!r} is a {r['kind']}, not a run: give the run's id too")
        return r["workflow"], r["run"]
    if run is None:
        raise ValueError("give the run's id, or the run's link alone")
    return workflow_of(job), run
