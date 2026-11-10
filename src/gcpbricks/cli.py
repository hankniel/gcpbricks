"""mlops bundle: validate, deploy and run what a project's mlops.yaml declares, the way
`databricks bundle` does.

  mlops bundle validate [-t T] [--var k=v]   mlops.yaml resolves and validates for T
  mlops bundle summary  [-t T]               jobs, tasks, schedules and resource names
  mlops bundle render   [-t T]               the terraform module's `plan` input, as JSON
  mlops bundle deploy   [-t T] [--image URI] [--version V] [--out-dir DIR]
                                             upload the bundle's files, build the jobs'
                                             environments a registry lacks, build +
                                             publish every vertex_pipeline_task,
                                             apply the jobs' infrastructure (Workflows,
                                             Cloud Run jobs, schedulers, triggers, alerts)
                                             with terraform, and apply experiments,
                                             registered_models and model_serving_endpoints
  mlops bundle destroy  [-t T]               remove the jobs' infrastructure for T
  mlops bundle run JOB  [-t T] [--params k=v,k2=v2] [--only task,task] [--no-wait]
                                             execute the job's Workflow now, with its
                                             scheduler's argument and these parameters
  mlops bundle runs JOB [-t T] [-n N]        the job's recent runs, with their ids
  mlops bundle why JOB --run ID [--task K]   what stopped the run: its failure down to the
                                             log line, or the pipeline conditions a run
                                             that succeeded skipped (a gate's rejection)
                                             and the verdicts behind them
  mlops bundle task JOB K --run ID           a task's state; a pipeline's steps, their
                                             outputs, metrics and Markdown verdicts
  mlops bundle logs JOB K --run ID [--step S] [--raw]
                                             what the task (or one pipeline step) printed,
                                             without the runtime's noise unless --raw
                                             (why/task/logs read GCP as you, no console/IAP)
  mlops bundle repair JOB --run ID [-t T] [--models m,m] [--tasks t,t] [--params k=v,k2=]
                    [--steps TASK=STEP] [--fresh-pipelines t,t] [--latest-repair-id N] [--no-wait]
                                             repair a failed run: a new attempt of it that
                                             reuses its successful tasks, with its parameters
                                             and these overrides; a batch_serve_task resumes
                                             its failed models and runs --models again in full;
                                             a vertex_pipeline_task reruns its pipeline in Vertex,
                                             reusing the steps that succeeded (--steps: also these)
  mlops bundle redeploy -t T [--bucket B] [--bundle NAME]
                                             redeploy every bundle on "latest" (or NAME)
                                             from the files its last deploy to T saved,
                                             with this installed release: a platform
                                             release's rollout, no project pipeline

T defaults to the target with `default: true`. Deploy prod from CI only: a deploy from a
branch rolls every resource back to that branch's view. Infrastructure needs terraform
(>= 1.6) on PATH; its state is the bundle's own (gcpbricks.infra).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pydantic import ValidationError

from gcpbricks import bundle as B
from gcpbricks import environments, files, infra
from gcpbricks import runs as runs_mod
from gcpbricks.publish import publish
from gcpbricks.targets import storage_prefix

EXECUTIONS_API = "https://workflowexecutions.googleapis.com/v1"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def _workflow_path(lb: B.Loaded, job: str) -> str:
    b = lb.bundle
    return (f"projects/{b.gcp.project}/locations/{b.gcp.region}/workflows/"
            f"{B.workflow_name(b, job, lb.target)}")


def _version(explicit: str) -> str:
    if explicit:
        return explicit
    if os.environ.get("CI_COMMIT_SHA"):
        return os.environ["CI_COMMIT_SHA"]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True, check=False).stdout.strip()
    return sha + ("-dirty" if dirty else "")


def _pairs(raw: list[str], flag: str) -> dict[str, str]:
    out = {}
    for item in raw:
        for pair in item.split(","):
            key, sep, value = pair.partition("=")
            if not sep:
                sys.exit(f"mlops: {flag} {pair!r} is not key=value")
            out[key.strip()] = value
    return out


def _load(a: argparse.Namespace) -> B.Loaded:
    try:
        return B.load(a.file, a.target, _pairs(a.var, "--var"))
    except ValidationError as e:
        lines = []
        for err in e.errors():
            where = ".".join(str(p) for p in err["loc"])
            msg = ("not supported by gcpbricks" if err["type"] == "extra_forbidden"
                   else err["msg"])
            lines.append(f"  {where}: {msg}")
        sys.exit("mlops: mlops.yaml is invalid:\n" + "\n".join(lines))
    except (ValueError, TypeError) as e:
        sys.exit(f"mlops: {e}")


def validate(lb: B.Loaded) -> int:
    for name, job in lb.bundle.resources.jobs.items():
        s, t = job.schedule, job.trigger
        when = (f"{s.quartz_cron_expression} {s.timezone_id}" if s
                else f"trigger {t.kind}" if t else "manual only")
        print(f"ok   {name}: {len(job.tasks)} task(s), {when}")
    for key, d in B.dashboards(lb).items():
        print(f"ok   dashboard {key}: {len(json.loads(d['content'])['widgets'])} widget(s)")
    print(f"{lb.bundle.bundle.name} @ {lb.target}: {len(lb.bundle.resources.jobs)} job(s), "
          f"mlops_version {lb.bundle.bundle.mlops_version}")
    return 0


def summary(lb: B.Loaded) -> int:
    plan = B.render(lb)
    print(f"bundle {plan['bundle']}  target {plan['target']}  run as {plan['service_account']}")
    for name, job in lb.bundle.resources.jobs.items():
        j = plan["jobs"][name]
        sched = "manual only" if not j["schedule"] else (
            f"{j['schedule']} {j['time_zone']}" + (" (paused)" if j["paused"] else ""))
        print(f"\njob {name}  workflow {j['workflow']}  {sched}")
        for i, level in enumerate(B.levels(job)):
            for key in level:
                t = next(x for x in job.tasks if x.task_key == key)
                deps = ",".join(d.task_key + (f"={d.outcome}" if d.outcome else "")
                                for d in t.depends_on)
                print(f"  [{i}] {key:<24} {t.kind:<22} {('after ' + deps) if deps else ''}"
                      f"{'' if t.run_if == 'ALL_SUCCESS' else '  run_if ' + t.run_if}")
    for name, rj in plan["run_jobs"].items():
        print(f"\ncloud run job {name}  image {rj['image'] or '(platform dbt image)'}")
    ml = B.ml_plan(lb)
    for name in ml["experiments"]:
        print(f"\nexperiment {name}")
    for m in ml["registered_models"]:
        print(f"\nregistered model {m['display_name']}  aliases {m['aliases'] or '-'}")
    for e in ml["model_serving_endpoints"]:
        print(f"\nserving endpoint {e['display_name']}  traffic {e['traffic']}")
        for d in e["deployments"]:
            gpu = f" + {d['accelerator_count']}x {d['accelerator_type']}" if d["accelerator_count"] else ""
            print(f"  {d['name']:<28} {d['model']} v{d['version']}  {d['machine_type']}{gpu}  "
                  f"replicas {d['min_replica_count']}-{d['max_replica_count']}")
    return 0


def deploy(lb: B.Loaded, image: str, version: str, out_dir: Path | None, snapshot: bool = False) -> int:
    """`snapshot`: lb is the files a deploy saved (redeploy), so they and its published
    pipelines stay as they are."""
    b, target = lb.bundle, lb.target
    version = _version(version)
    if out_dir is None:
        # Before anything is built or uploaded: a schema terraform would replace stops the deploy.
        try:
            infra.existing_schemas(lb, _session())
        except ValueError as e:
            print(f"FAIL {e}")
            return 1
    # Every deploy saves its files, which a dbt or python task reads and redeploy starts from.
    if not snapshot:
        paths = files.bundle_files(lb.root, b.sync.include, b.sync.exclude)
        uri = B.files_uri(b, target)
        files.upload(uri, version, files.archive(lb.root, paths), out_dir)
        print(f"ok   files: {len(paths)} file(s) @ {version} -> {uri}")
    try:
        environments.ensure(lb, out_dir, None if out_dir is not None else _session())
    except (RuntimeError, ValueError) as e:
        print(f"FAIL {e}")
        return 1
    for job_name, job in b.resources.jobs.items():
        for t in B._all_tasks(job):
            if t.kind != "vertex_pipeline_task":
                continue
            if snapshot:
                print(f"note {t.task_key}: keeps the pipeline published @ {version}; its next deploy "
                      f"from the project recompiles it")
                continue
            task_image = B.environment_image(lb, job_name, t.environment_key) if t.environment_key else image
            code = _publish_pipeline(lb, job_name, t, task_image, version, out_dir)
            if code:
                return code
    missing = [] if out_dir is not None else _missing_workflows(lb, B.external_jobs(lb))
    if missing:
        print(f"FAIL run_job_task names job(s) with no Workflow on {target}: {missing}; deploy "
              f"those bundles first, or check the lookup names")
        return 1
    problems = [] if out_dir is not None else _dashboard_problems(lb, _session())
    if problems:
        print("FAIL dashboards:\n" + "\n".join(f"  {p}" for p in problems))
        return 1
    from datetime import UTC, datetime

    record = {} if out_dir is not None else _previous_record(lb)
    lb = lb.model_copy(update={
        "files_version": version,
        "retired_jobs": B.retired_jobs(lb, record, datetime.now(UTC).strftime("%Y-%m-%d"))})
    for key, r in sorted(lb.retired_jobs.items()):
        print(f"note job {key}: no longer declared, kept retired since {r['since']} (no schedule or "
              f"trigger); an admin deletes it from the console")
    code = infra.apply(lb, out_dir=out_dir)
    if code:
        return code
    ml_plan = B.ml_plan(lb)
    if out_dir is not None:
        if any(ml_plan.values()):
            (out_dir / "ml_plan.json").write_text(json.dumps(ml_plan, indent=2, sort_keys=True))
            print(f"ok   ml resources: written to {out_dir / 'ml_plan.json'}, not applied")
    else:
        previous = record.get("resources", {})
        if any(ml_plan.values()) or any(previous.get(k) for k in ML_KINDS):
            from gcpbricks.ml import VertexSdk, apply

            apply(ml_plan, VertexSdk(b.gcp.project, b.gcp.region), B.bundle_labels(lb), previous,
                  datetime.now(UTC).strftime("%Y-%m-%d"))
    _record_deployment(lb, version, out_dir)
    return 0


def redeploy(bucket: str, target: str, only: str | None) -> int:
    """Redeploys each bundle whose last deploy to `target` recorded a deployment.json under
    gs://`bucket`, from the files that deploy saved, with this installed release. A bundle
    pinned to a release is left to its own pipeline; one that names bundle `only` too."""
    from google.cloud import storage

    prefix = f"{storage_prefix(target)}.bundle/"
    gcs = storage.Client().bucket(bucket)
    records = [json.loads(blob.download_as_bytes()) for blob in gcs.list_blobs(prefix=prefix)
               if blob.name.count("/") == prefix.count("/") + 1 and blob.name.endswith("/deployment.json")]
    failed = []
    for r in sorted(records, key=lambda r: r["bundle"]):
        name = r["bundle"]
        if only and name != only:
            continue
        if not only and r.get("declared_mlops_version") != B.LATEST:
            print(f"skip {name}: pinned to {r.get('declared_mlops_version')}; its own pipeline deploys a new release")
            continue
        print(f"\n=== {name} @ {target}: {r['version']}", flush=True)
        root = Path(tempfile.mkdtemp(prefix=f"mlops-{name}-"))
        try:
            files.fetch(B.files_uri_of(bucket, target, name), root, version=r["version"])
            code = deploy(B.load(root / "mlops.yaml", target), "", r["version"], None, snapshot=True)
        except Exception as e:  # noqa: BLE001 - one bundle's failure must not stop the rest
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            code = 1
        if code:
            failed.append(name)
    if failed:
        print(f"\nFAIL redeploy {target}: {failed}")
        return 1
    return 0


def _dashboard_problems(lb: B.Loaded, session) -> list[str]:
    """Each dashboard dataset's SQL dry-run in BigQuery (nothing billed), and the columns
    its widgets and filters name checked against what it returns."""
    from gcpbricks.dashboards import DashboardSpec

    out = []
    for key, d in B.dashboards(lb).items():
        spec = DashboardSpec.model_validate({k: v for k, v in json.loads(d["content"]).items()
                                             if k in ("window", "datasets", "filters", "widgets")})
        for name, ds in spec.datasets.items():
            params = [{"name": n, "parameterType": {"type": "TIMESTAMP"},
                       "parameterValue": {"value": "2026-01-01T00:00:00Z"}}
                      for n in ("start", "end") if f"@{n}" in ds.bigquery]
            params += [{"name": n, "parameterType": {"type": "STRING"}, "parameterValue": {"value": ""}}
                       for n in spec.filters if f"@{n}" in ds.bigquery]
            r = session.post(f"https://bigquery.googleapis.com/bigquery/v2/projects/{lb.bundle.gcp.project}/jobs",
                             json={"configuration": {"dryRun": True, "query": {
                                 "query": ds.bigquery, "useLegacySql": False, "parameterMode": "NAMED",
                                 "queryParameters": params}}})
            if not r.ok:
                out.append(f"{key}.{name}: {r.json().get('error', {}).get('message', r.text)}")
                continue
            columns = {f["name"] for f in r.json()["statistics"]["query"]["schema"]["fields"]}
            used = {ds.time} if ds.time else set()
            for w in spec.widgets:
                if w.dataset == name:
                    used |= w.fields()
            used |= {f.field for f in spec.filters.values() if f.dataset == name}
            if used - columns:
                out.append(f"{key}.{name}: no column {sorted(used - columns)}; it returns {sorted(columns)}")
    return out


ML_KINDS = ("experiments", "registered_models", "model_serving_endpoints")


def _previous_record(lb: B.Loaded) -> dict:
    """The target's last deploy record (deployment.json), {} before the first."""
    from google.api_core.exceptions import NotFound
    from google.cloud import storage

    blob = storage.Client().bucket(lb.bundle.gcp.bucket).blob(B.deployment_object(lb.bundle, lb.target))
    try:
        return json.loads(blob.download_as_bytes())
    except NotFound:
        return {}


def _source(lb: B.Loaded) -> dict:
    """The git commit a deploy ships: CI's, else the bundle checkout's (dirty when it has
    uncommitted changes, which the commit alone doesn't show). {} outside git."""
    sha = os.environ.get("CI_COMMIT_SHA")
    if sha:
        project = os.environ.get("CI_PROJECT_URL", "")
        return {"commit": sha, "branch": os.environ.get("CI_COMMIT_REF_NAME", ""),
                **({"commit_url": f"{project}/-/commit/{sha}"} if project else {})}

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(lb.root), *args], capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()
    try:
        out = {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD")}
        if git("status", "--porcelain"):
            out["dirty"] = True
        return out
    except (OSError, subprocess.SubprocessError):
        return {}


def _record_deployment(lb: B.Loaded, version: str, out_dir: Path | None) -> None:
    """deployment.json beside the job definitions: what this deploy put where (B.deployment)."""
    from datetime import UTC, datetime
    from importlib.metadata import version as installed

    record = B.deployment(
        lb, version, installed("gcpbricks"),
        os.environ.get("GITLAB_USER_EMAIL") or os.environ.get("USER", ""),
        os.environ.get("CI_PIPELINE_URL", ""), datetime.now(UTC).isoformat(timespec="seconds"), _source(lb))
    obj = B.deployment_object(lb.bundle, lb.target)
    body = json.dumps(record, indent=2, sort_keys=True)
    if out_dir is not None:
        path = out_dir / lb.bundle.gcp.bucket / obj
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    else:
        from google.cloud import storage

        storage.Client().bucket(lb.bundle.gcp.bucket).blob(obj).upload_from_string(
            body, content_type="application/json")
    print(f"ok   deployment: {version} -> gs://{lb.bundle.gcp.bucket}/{obj}")


def _publish_pipeline(lb: B.Loaded, job: str, t, image: str, version: str, out_dir: Path | None) -> int:
    """Compiles a vertex_pipeline_task's python_file (or runs its build) and publishes it."""
    b, target, v = lb.bundle, lb.target, t.vertex_pipeline_task
    build_dir = Path(tempfile.mkdtemp(prefix=f"mlops-{t.task_key}-"))
    env = {**os.environ, "TARGET": target}
    if v.python_file:
        env |= {"MLOPS_PIPELINE_IMAGE": image, "MLOPS_BUNDLE_FILES": B.files_uri(b, target),
                "MLOPS_BUNDLE_VERSION": version, **B.mlflow_env(lb)}
        cmd = [sys.executable, "-m", "gcpbricks.pipelines", "compile", "--root", str(lb.root),
               "--python-file", v.python_file, "--function", v.function,
               "--out", str(build_dir / f"{v.pipeline}_pipeline.json")]
        print(f"+ compile {v.python_file}:{v.function}", flush=True)
        done = subprocess.run(cmd, cwd=lb.root, env=env, check=False)
        (build_dir / f"{v.pipeline}_params.json").write_text(json.dumps(
            B.pipeline_params(lb, job, t), indent=2, sort_keys=True))
    else:
        cmd = v.build.format(out_dir=build_dir, image=image, target=target, version=version)
        print(f"+ {cmd}", flush=True)
        done = subprocess.run(cmd, shell=True, cwd=lb.root, env=env, check=False)
    if done.returncode:
        print(f"FAIL {t.task_key}: build exited {done.returncode}")
        return done.returncode
    base = storage_prefix(target) + B.pipeline_prefix(b, job, t)
    body = publish(build_dir, bucket=b.gcp.bucket, base=base, version=version, image=image,
                   out_dir=out_dir)
    where = out_dir or f"gs://{b.gcp.bucket}/{base}"
    print(f"ok   {t.task_key}: published {[k for k in body if k not in ('version', 'image')]} "
          f"@ {version} -> {where}")
    return 0


def _missing_workflows(lb: B.Loaded, names: list[str]) -> list[str]:
    """Which of these Workflows (other bundles' jobs) don't exist in the bundle's project."""
    if not names:
        return []
    s = _session()
    base = (f"https://workflows.googleapis.com/v1/projects/{lb.bundle.gcp.project}/locations/"
            f"{lb.bundle.gcp.region}/workflows")
    return [n for n in names if s.get(f"{base}/{n}").status_code == 404]


def run(lb: B.Loaded, name: str, params: dict[str, str], wait: bool, only: list[str] = ()) -> int:
    jobs = lb.bundle.resources.jobs
    if name not in jobs:
        sys.exit(f"mlops: no job {name!r} in the bundle")
    declared = {p.name for p in jobs[name].parameters}
    unknown = sorted(set(params) - declared)
    if unknown:
        sys.exit(f"mlops: job {name} has parameters {sorted(declared)}, not {unknown}")
    tasks = {t.task_key for t in jobs[name].tasks}
    if set(only) - tasks:
        sys.exit(f"mlops: job {name} has tasks {sorted(tasks)}, not {sorted(set(only) - tasks)}")
    argument = B.job_argument(lb, name)
    if params:
        argument["params"] = params
    if only:
        argument["only"] = {k: True for k in only}
    return _start(lb, name, argument, wait)


def _start(lb: B.Loaded, name: str, argument: dict, wait: bool) -> int:
    s = _session()
    resp = s.post(f"{EXECUTIONS_API}/{_workflow_path(lb, name)}/executions",
                  json={"argument": json.dumps(argument)})
    resp.raise_for_status()
    execution = resp.json()
    print(f"started {execution['name']}")
    if not wait:
        return 0
    while execution.get("state") not in TERMINAL:
        time.sleep(30)
        execution = s.get(f"{EXECUTIONS_API}/{execution['name']}").json()
    print(f"{execution['state']}: {execution.get('result') or execution.get('error', {}).get('payload', '')}")
    return 0 if execution["state"] == "SUCCEEDED" else 1


def repair_argument(execution: dict, models: list[str] | None = None, child=None) -> dict:
    try:
        return runs_mod.repair_argument(execution, models, child)
    except ValueError as e:
        sys.exit(f"mlops: {e}")


def repair(lb: B.Loaded, name: str, run_id: str, wait: bool, models: list[str] | None = None,
           tasks: list[str] | None = None, params: dict[str, str] | None = None,
           latest_repair_id: int | None = None, steps: dict[str, list[str]] | None = None,
           fresh: list[str] | None = None) -> int:
    """A new attempt of the run, as Databricks' repair (runs.start_repair): `params` override
    the run's and stay for later repairs, an empty value drops one."""
    from gcpbricks.console.gcp import Gcp

    if name not in lb.bundle.resources.jobs:
        sys.exit(f"mlops: no job {name!r} in the bundle")
    b = lb.bundle
    gcp = Gcp(b.gcp.project, b.gcp.region)
    job = {"workflow": B.workflow_name(b, name, lb.target), "job": name, "bucket": b.gcp.bucket,
           "definition": B.definition_object(b, name, lb.target)}
    try:
        started = runs_mod.start_repair(
            gcp, job, run_id, user=_whoami(), latest_repair_id=latest_repair_id, tasks=tasks, models=models,
            params={k: (v or None) for k, v in (params or {}).items()}, steps=steps, fresh=fresh,
            child=lambda n: gcp.execution(n.split("/")[5], n.split("/")[7]))
    except ValueError as e:
        sys.exit(f"mlops: {e}")
    argument = started["argument"]
    reused = sorted(k for k, v in argument["repair"].items() if v.get("status") == "SUCCESS")
    print(f"repair {started['repair_id']} of run {started['run_id']}: reusing {reused or 'nothing'}"
          + (f", parameters {argument['params']}" if argument.get("params") else ""))
    for key, m in runs_mod.repaired_models(argument).items():
        print(f"  {key}: resuming {m['resume'] or 'none'}, again in full {m['fresh'] or 'none'}")
    for key, p in runs_mod.repaired_pipelines(argument).items():
        print(f"  {key}: Vertex rerun of {p['pipeline_job'].rsplit('/', 1)[-1]}, steps again: {p['rerun'] or 'only the failed'}")
    print(f"started {_workflow_path(lb, name)}/executions/{started['execution']}")
    if not wait:
        return 0
    execution = gcp.execution(job["workflow"], started["execution"])
    while execution.get("state") not in TERMINAL:
        time.sleep(30)
        execution = gcp.execution(job["workflow"], started["execution"])
    print(f"{execution['state']}: {execution.get('result') or execution.get('error', {}).get('payload', '')}")
    return 0 if execution["state"] == "SUCCEEDED" else 1


def _steps(raw: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in raw:
        task, sep, step = item.partition("=")
        if not sep or not step:
            sys.exit(f"mlops: --steps {item!r} is not TASK=STEP")
        out.setdefault(task, []).append(step)
    return out


def _whoami() -> str:
    """Who repairs, for the run's record: CI's user, else the local one."""
    return os.environ.get("GITLAB_USER_EMAIL") or os.environ.get("USER", "")


def runs(lb: B.Loaded, name: str, n: int) -> int:
    if name not in lb.bundle.resources.jobs:
        sys.exit(f"mlops: no job {name!r} in the bundle")
    resp = _session().get(f"{EXECUTIONS_API}/{_workflow_path(lb, name)}/executions",
                          params={"view": "FULL", "pageSize": n})
    resp.raise_for_status()
    executions = resp.json().get("executions", [])
    for ex in executions:
        err = (ex.get("error") or {}).get("payload", "")
        run_id, repair_id = runs_mod.run_of(ex)
        print(f"{ex.get('startTime', '')[:19]}  {ex['name'].rsplit('/', 1)[-1]}  "
              f"{ex.get('state', ''):<10} " + (f"repair {repair_id} of {run_id}  " if repair_id else "") + err[:80])
    if not executions:
        print(f"no runs of {name} on {lb.target}")
    return 0


# --- debugging a run: the console's own reads, on the caller's credentials (no IAP) ---

#: Log lines `logs` drops unless --raw: runtime noise around the lines a step prints.
_NOISE = ("FutureWarning", "warnings.warn(", "Running pip as the 'root' user", "Job is running.",
          "Waiting for job to be provisioned.", "Vertex AI is provisioning job running framework.")


def _console(lb: B.Loaded, name: str):
    if name not in lb.bundle.resources.jobs:
        sys.exit(f"mlops: no job {name!r} in the bundle")
    from gcpbricks.console import gcp as G
    return G.Gcp(lb.bundle.gcp.project, lb.bundle.gcp.region), B.workflow_name(lb.bundle, name, lb.target)


def why(lb: B.Loaded, name: str, run_id: str, task: str | None) -> int:
    """What stopped a run: its failure followed down to the line that matters, or, for a run
    that succeeded, the pipeline conditions that skipped steps and the verdicts behind them."""
    from gcpbricks.console import failure as F
    g, wf = _console(lb, name)
    w = F.why(g, wf, run_id, task)
    for p in w["path"]:
        print(f"{p['kind']:<10} {p['label']}  {p.get('state') or ''}  {p.get('link') or ''}")
    code = w.get("code") or {}
    if code:
        print(f"code       {code.get('commit') or code.get('version', '')}  mlops {code.get('mlops_version', '?')}  "
              f"deployed {code.get('deployed_at', '')} by {code.get('deployed_by', '')}")
    if w["error"]:
        print(f"\nerror: {w['error']}")
    for s in w.get("skipped") or []:
        print(f"\n{s['task']}: skipped  {s['pipeline']}")
        for c in s["skipped"]:
            print(f"  {c['step']}: {c['condition']} did not hold")
        for v in s["verdicts"]:
            print(f"\n--- {v['step']} / {v['name']}\n{v['text'].rstrip()}")
    if w["logs"]:
        print("\nlast log lines:")
        for line in w["logs"]:
            print(f"  {(line['time'] or '')[:19]} {line['severity']:<8} {line['text']}")
    if not w["error"] and not w.get("skipped") and len(w["path"]) == 1:
        print("\nnothing failed and no pipeline step was skipped")
    return 0


def task(lb: B.Loaded, name: str, run_id: str, key: str) -> int:
    """One task of a run: its state, and for a vertex_pipeline_task every step with its state,
    output values and artifacts' metadata (a gate's metrics), and the Markdown it wrote."""
    from gcpbricks.console import pipeline_view as PV
    from gcpbricks.console import service as S
    g, wf = _console(lb, name)
    t = S.task_run(g, wf, run_id, key)
    print(f"task {key}  {t.get('status') or ''}  {t.get('start') or ''} -> {t.get('end') or ''}"
          f"  (run {t['run'].get('state')})")
    if t.get("error"):
        print(f"error: {t['error']}")
    if S.task_kind(t["spec"]) != "vertex_pipeline_task":
        return 0
    p = S.pipeline_run(g, wf, run_id, key)
    if not p["run"]:
        print("no pipeline run (not started, or the runner skipped it)")
        return 0
    print(f"pipeline {p['run']['id']}  {p['run']['state']}  {p['run']['link']}")
    if p["run"]["error"]:
        print(f"error: {p['run']['error']}")
    job = g.ai_get(p["run"]["name"])
    details = {d.get("taskName"): d for d in (job.get("jobDetail") or {}).get("taskDetails", [])}
    texts = []
    for n in sorted(p["nodes"], key=lambda n: n["level"]):
        d = details.get(n["key"]) or {}
        cached = "  (cached)" if (d.get("execution") or {}).get("state") == "CACHED" else ""
        cond = f"  if {n['condition']}" if n["condition"] else ""
        print(f"\n  {n['label']:<28} {n['state']:<9}{cond}{cached}")
        if n["error"]:
            print(f"    error: {n['error']}")
        meta = (d.get("execution") or {}).get("metadata") or {}
        for k, v in meta.items():
            if k.startswith("output:") and k != "output:gcp_resources":
                shown = json.dumps(v)
                print(f"    {k.removeprefix('output:')} = " + (shown if len(shown) <= 200 else shown[:200] + " ..."))
        for a in PV._artifacts(d.get("outputs")):
            if a["metadata"]:
                print(f"    {a['name']}: {json.dumps(a['metadata'], separators=(',', ':'))}")
            if a["type"] == "system.Markdown" and (a["uri"] or "").startswith("gs://"):
                texts.append((n["label"], a))
    for label, a in texts:
        got = g.read_object(*PV._gs(a["uri"]))
        if got:
            print(f"\n--- {label} / {a['name']}\n{got[0][:PV.MAX_TEXT].decode('utf-8', 'replace').rstrip()}")
    return 0


def _quiet(entries: list[tuple]) -> list[tuple]:
    """(time, severity, text) log entries without the runtime's noise: KFP's executor_input
    dump (a JSON object, one entry per line, from 'Got executor_input:' to its closing brace)
    and _NOISE."""
    out, dump = [], False
    for e in entries:
        text = e[2]
        if dump:
            # Its closing brace is the only one at column 0; nested ones are indented.
            dump = text.rstrip() != "}"
            continue
        if text.rstrip().endswith("Got executor_input:"):
            dump = True
            continue
        if not text.strip() or any(s in text for s in _NOISE):
            continue
        out.append(e)
    return out


def logs(lb: B.Loaded, name: str, run_id: str, key: str, step: str | None, raw: bool) -> int:
    """What a task (or, with --step, one of its pipeline's steps) printed, oldest first."""
    from gcpbricks.console import pipeline_view as PV
    from gcpbricks.console import service as S
    g, wf = _console(lb, name)
    if step:
        entries = [(e["time"], e["severity"], e["text"]) for e in PV.step(g, wf, run_id, key, step)["logs"]]
    else:
        entries = [(e["time"], e["severity"], (f"[{e['task']}] " if e.get("task") else "") + e["text"])
                   for e in S.task_logs(g, wf, run_id, key)]
    if not raw:
        entries = _quiet(entries)
    for t, sev, text in entries:
        print(f"{(t or '')[:19]} {sev:<8} {text.rstrip()}")
    if not entries:
        print("no log lines" + ("" if raw else " (--raw shows the runtime's own)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mlops", description=__doc__.split("\n\n")[0])
    top = ap.add_subparsers(dest="group", required=True)
    bundle = top.add_parser("bundle")
    sub = bundle.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("-f", "--file", default="mlops.yaml", help="the project's mlops.yaml")
        p.add_argument("-t", "--target")
        p.add_argument("--var", action="append", default=[], help="name=value, repeatable")
        return p
    common(sub.add_parser("validate"))
    common(sub.add_parser("summary"))
    common(sub.add_parser("render"))
    p = common(sub.add_parser("deploy"))
    p.add_argument("--image", default="")
    p.add_argument("--version", default="", help="default: $CI_COMMIT_SHA, else git HEAD")
    p.add_argument("--out-dir", type=Path, help="write what GCS would get here instead")
    common(sub.add_parser("destroy"))
    p = common(sub.add_parser("run"))
    p.add_argument("job")
    p.add_argument("--params", action="append", default=[], help="k=v[,k=v]: job parameters")
    p.add_argument("--only", default="", help="task,task: run these tasks only")
    p.add_argument("--no-wait", dest="wait", action="store_false")
    p = common(sub.add_parser("runs"))
    p.add_argument("job")
    p.add_argument("-n", type=int, default=10)
    p = common(sub.add_parser("why"))
    p.add_argument("job")
    p.add_argument("--run", required=True, help="the run's id, from `runs`")
    p.add_argument("--task", help="the task to explain (default: the run's first failed one, or all)")
    p = common(sub.add_parser("task"))
    p.add_argument("job")
    p.add_argument("task")
    p.add_argument("--run", required=True, help="the run's id, from `runs`")
    p = common(sub.add_parser("logs"))
    p.add_argument("job")
    p.add_argument("task")
    p.add_argument("--run", required=True, help="the run's id, from `runs`")
    p.add_argument("--step", help="a vertex_pipeline_task's pipeline step (as `task` lists it), e.g. gate-op")
    p.add_argument("--raw", action="store_true", help="keep the executor_input dump and runtime warnings")
    p = common(sub.add_parser("repair"))
    p.add_argument("job")
    p.add_argument("--run", required=True, help="the failed run's id (or its latest repair's), from `runs`")
    p.add_argument("--models", default="", help="m,m: a batch_serve_task's models to run again in full")
    p.add_argument("--tasks", default="", help="task,task: succeeded tasks to run again too")
    p.add_argument("--params", action="append", default=[],
                   help="k=v,k2=: override the run's parameters (kept by later repairs); k= drops one")
    p.add_argument("--latest-repair-id", type=int, help="the run's latest repair you looked at (0: none)")
    p.add_argument("--steps", action="append", default=[],
                   help="TASK=STEP: a vertex_pipeline_task's pipeline step to run again although it succeeded")
    p.add_argument("--fresh-pipelines", default="", help="task,task: start a new pipeline instead of a Vertex rerun")
    p.add_argument("--no-wait", dest="wait", action="store_false")
    p = sub.add_parser("redeploy")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("--bucket", default="my-ml-bucket")
    p.add_argument("--bundle", help="only this bundle, pinned or not")
    a = ap.parse_args(argv)

    if a.cmd == "redeploy":
        return redeploy(a.bucket, a.target, a.bundle)

    lb = _load(a)
    if a.cmd == "validate":
        return validate(lb)
    if a.cmd == "summary":
        return summary(lb)
    if a.cmd == "render":
        print(json.dumps(B.render(lb), indent=2, sort_keys=True))
        return 0
    if a.cmd == "deploy":
        return deploy(lb, a.image, a.version, a.out_dir)
    if a.cmd == "destroy":
        return infra.apply(lb, destroy=True)
    if a.cmd == "run":
        return run(lb, a.job, _pairs(a.params, "--params"), a.wait, [t for t in a.only.split(",") if t])
    if a.cmd == "repair":
        return repair(lb, a.job, a.run, a.wait, [m for m in a.models.split(",") if m],
                      [t for t in a.tasks.split(",") if t], _pairs(a.params, "--params"), a.latest_repair_id,
                      _steps(a.steps), [t for t in a.fresh_pipelines.split(",") if t])
    if a.cmd == "why":
        return why(lb, a.job, a.run, a.task)
    if a.cmd == "task":
        return task(lb, a.job, a.run, a.task)
    if a.cmd == "logs":
        return logs(lb, a.job, a.run, a.task, a.step, a.raw)
    return runs(lb, a.job, a.n)


if __name__ == "__main__":
    sys.exit(main())
