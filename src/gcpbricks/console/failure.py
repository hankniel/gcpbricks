"""Why a run failed, in one place: the console's "Why it failed" card and the failure email.

A failure surfaces as "task(s) failed" at the top while its reason sits several systems
down: run -> task -> its child run (a bundle job, or a runner Workflow) -> a Vertex
pipeline -> its failed step -> that step's custom job, whose log carries the line that
matters, often at no severity at all. why() follows that chain to the deepest failure it
can reach and returns the path walked, the leaf's own error, the leaf's last log lines (every
severity) and the code the bundle last deployed.
"""
from __future__ import annotations

import json
import re

from gcpbricks.console import pipeline_view as PV
from gcpbricks.console import service as S

#: Log lines a leaf shows: its last ones before it ended.
TAIL = 40
_PIPELINE = re.compile(r"projects/[^/\s\"]+/locations/[^/\s\"]+/pipelineJobs/[^\s\"]+")
#: Finished runs never change: their answer is read once per console instance.
_DONE: dict[tuple[str, str, str], dict] = {}


def why(gcp, workflow: str, execution_id: str, task: str | None = None) -> dict:
    """{path: [{kind, label, state, link}], error, logs: [{time, severity, text}], code}, and
    for a run with no failed task, skipped: [{task, pipeline, skipped, verdicts}];
    task: the failed task to explain, else the run's first failed one."""
    key = (workflow, execution_id, task or "")
    if key in _DONE:
        return _DONE[key]
    out = {"path": [], "error": "", "logs": [], "code": _code(gcp, workflow)}
    _run(gcp, workflow, execution_id, task, out)
    if out["path"] and out["path"][0].get("state") in ("FAILED", "CANCELLED", "SUCCEEDED"):
        _DONE[key] = out
    return out


def _run(gcp, workflow: str, execution_id: str, task: str | None, out: dict) -> None:
    d = S.execution_detail(gcp, workflow, execution_id)
    out["path"].append({"kind": "run", "label": f"{workflow} run", "state": d.get("state"),
                        "link": f"/jobs/{workflow}/runs/{execution_id}"})
    failed = sorted((t for t in d["task_runs"].values() if t["status"] == "FAILED" and (not task or t["key"] == task)),
                    key=lambda t: t.get("end") or "~")
    if not failed:
        out["error"] = d.get("error_message") or S._error_text(d.get("error"))
        out["skipped"] = _skipped(gcp, workflow, execution_id, [k for k in d["task_runs"] if not task or k == task])
        return
    t = failed[0]
    out["path"].append({"kind": "task", "label": t["key"], "state": "FAILED",
                        "link": f"/jobs/{workflow}/runs/{execution_id}/tasks/{t['key']}"})
    out["error"] = t.get("error") or ""
    if not t["executions"]:
        return
    x = t["executions"][-1]
    if x["kind"] == "cloud_run":
        _container(gcp, x, out)
        return
    wf, child_id = S._workflow_execution(x["name"])
    if S._job_of(gcp.workflow(wf)):
        _run(gcp, wf, child_id, None, out)
    else:
        _runner(gcp, wf, child_id, out)


def _skipped(gcp, workflow: str, execution_id: str, tasks: list[str]) -> list[dict]:
    """A run that didn't fail can still have skipped what it was for (a gate's rejection skips
    promote, and the run succeeds): each vertex_pipeline_task's conditions that didn't hold,
    with the Markdown verdicts its steps wrote."""
    out = []
    for key in tasks:
        try:
            p = S.pipeline_run(gcp, workflow, execution_id, key)
        except (KeyError, ValueError):
            continue
        nodes = p.get("nodes", [])
        skipped = {n["key"] for n in nodes if n["state"] == "SKIPPED"}
        # Only the outermost: a condition inside a skipped one never got to be evaluated.
        conds = [{"step": n["label"], "condition": n["condition"]} for n in nodes
                 if n["kind"] == "condition" and n["key"] in skipped and n["condition"]
                 and n.get("parent") not in skipped]
        if not p["run"] or not conds:
            continue
        job = gcp.ai_get(p["run"]["name"])
        verdicts = []
        for d in (job.get("jobDetail") or {}).get("taskDetails", []):
            for a in PV._artifacts(d.get("outputs")):
                if a["type"] != "system.Markdown" or not (a["uri"] or "").startswith("gs://"):
                    continue
                got = gcp.read_object(*PV._gs(a["uri"]))
                if got:
                    verdicts.append({"step": d.get("taskName", ""), "name": a["name"],
                                     "text": got[0][:PV.MAX_TEXT].decode("utf-8", "replace")})
        out.append({"task": key, "pipeline": p["run"]["link"], "skipped": conds, "verdicts": verdicts})
    return out


def _container(gcp, x: dict, out: dict) -> None:
    ex = gcp.run_execution(x["name"])
    short = x["name"].rsplit("/", 1)[-1]
    out["path"].append({"kind": "container", "label": short, "state": "FAILED" if ex.get("failedCount") else ex.get("state", ""),
                        "link": f"https://console.cloud.google.com/run/jobs/executions/details/{x['name'].split('/')[3]}/{short}"
                                f"/logs?project={gcp.project}"})
    flt = f'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="{short}"'
    out["logs"] = _tail(gcp, flt, x.get("start"), ex.get("completionTime"))


def _runner(gcp, workflow: str, execution_id: str, out: dict) -> None:
    """A runner Workflow (vertex_pipeline, batch_serve, notebook): its error, and the Vertex
    pipeline it ran when there is one."""
    ex = gcp.execution(workflow, execution_id)
    error = (ex.get("error") or {}).get("payload") or ""
    out["path"].append({"kind": "runner", "label": workflow, "state": ex.get("state"),
                        "link": f"https://console.cloud.google.com/workflows/workflow/{gcp.region}/{workflow}"
                                f"/execution/{execution_id}?project={gcp.project}"})
    if error:
        out["error"] = _unquote(error)
    m = _PIPELINE.search(error + (ex.get("result") or ""))
    if m:
        _pipeline(gcp, m.group(0), out)
        return
    flt = (f'resource.type="workflows.googleapis.com/Workflow" AND resource.labels.workflow_id="{workflow}" '
           f'AND labels.execution_id="{execution_id}"')
    out["logs"] = _tail(gcp, flt, ex.get("startTime"), ex.get("endTime"))


def _pipeline(gcp, name: str, out: dict) -> None:
    job = gcp.ai_get(name)
    pid = name.rsplit("/", 1)[-1]
    out["path"].append({"kind": "pipeline", "label": job.get("displayName") or pid,
                        "state": job.get("state", "").removeprefix("PIPELINE_STATE_"),
                        "link": f"https://console.cloud.google.com/vertex-ai/locations/{gcp.region}/pipelines/runs/{pid}"
                                f"?project={gcp.project}"})
    if (job.get("error") or {}).get("message"):
        out["error"] = job["error"]["message"]
    steps = [t for t in (job.get("jobDetail") or {}).get("taskDetails", [])
             if t.get("state") == "FAILED" and t.get("executorDetail")]
    if not steps:
        return
    step = min(steps, key=lambda t: t.get("endTime") or "~")
    out["path"].append({"kind": "step", "label": step.get("taskName", ""), "state": "FAILED", "link": out["path"][-1]["link"]})
    out["error"] = (step.get("error") or {}).get("message") or out["error"]
    ed = step["executorDetail"]
    custom = (ed.get("customJobDetail") or {}).get("job") or (ed.get("containerDetail") or {}).get("mainJob")
    if not custom:
        return
    cj = gcp.ai_get(custom)
    job_id = custom.rsplit("/", 1)[-1]
    out["path"].append({"kind": "custom_job", "label": job_id, "state": cj.get("state", "").removeprefix("JOB_STATE_"),
                        "link": f"https://console.cloud.google.com/vertex-ai/locations/{gcp.region}/training/{job_id}"
                                f"/cpu?project={gcp.project}"})
    out["logs"] = _tail(gcp, f'resource.type="ml_job" AND resource.labels.job_id="{job_id}"',
                        cj.get("createTime"), cj.get("endTime") or cj.get("updateTime"))


def _tail(gcp, flt: str, start: str | None, end: str | None) -> list[dict]:
    """The last TAIL lines up to `end`, oldest first, every severity; a run of identical
    lines (Vertex's provisioning notices) kept once."""
    bound = S._since(start) + (f' AND timestamp<="{end}"' if end else "")
    lines = []
    for e in reversed(gcp.log_entries(flt + bound, TAIL * 3, newest_first=True)):
        payload = e.get("textPayload") if "textPayload" in e else (e.get("jsonPayload") or {}).get("message", e.get("jsonPayload"))
        text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        if not text or not text.strip() or (lines and lines[-1]["text"] == text):
            continue
        lines.append({"time": e.get("timestamp"), "severity": e.get("severity", "DEFAULT"), "text": text.rstrip()})
    return lines[-TAIL:]


def _code(gcp, workflow: str) -> dict:
    """The code the job's bundle last deployed: its deployment.json beside the definition."""
    job = S._job_of(gcp.workflow(workflow))
    if not job or not job["definition"]:
        return {}
    obj = job["definition"].split("/jobs/")[0] + "/deployment.json"
    rec = gcp.read_json_or_none(job["bucket"], obj) or {}
    keep = ("version", "commit", "branch", "commit_url", "dirty", "deployed_at", "deployed_by", "ci_url", "mlops_version")
    return {k: rec[k] for k in keep if rec.get(k)}


def _unquote(payload: str) -> str:
    try:
        v = json.loads(payload)
    except ValueError:
        return payload
    return v.get("message", payload) if isinstance(v, dict) else str(v)
