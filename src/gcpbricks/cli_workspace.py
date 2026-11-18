"""The `mlops` CLI's workspace groups, named and shaped as the Databricks CLI's: the jobs and
runs, compute, catalog, models and quality monitors of every deployed bundle, from anywhere
(no mlops.yaml), as you. Each command is a console backend call (gcpbricks.workspace).

  mlops inspect LINK                     what a console / Workflows / Vertex link names, and
                                         for a failed run or task its failure and log tail
  mlops jobs list [--target T]           every bundle job
  mlops jobs get JOB                     its settings, tasks, schedule and recent runs
  mlops jobs list-runs JOB [-n N]
  mlops jobs get-run RUN                 a run's tasks and their state (RUN: link, or JOB ID)
  mlops jobs get-run-output RUN [--task K] [--step S] [-n N]
                                         a task's (or pipeline step's) log; no --task: why
  mlops jobs why RUN [--task K]          what stopped the run, down to the log line
  mlops jobs run-now JOB [--params k=v] [--only t,t]
  mlops jobs cancel-run RUN              cancel a run and the containers it started
  mlops jobs cancel-all-runs JOB         cancel every active run of the job
  mlops jobs repair-run RUN [--tasks t,t] [--models m,m] [--params k=v] [--keep t,t]
  mlops jobs pause JOB | resume JOB      its schedule or trigger
  mlops jobs delete JOB                  a retired job (its bundle no longer declares it)
  mlops clusters list|get ID|create --template T [--name N]|start ID|stop ID|delete ID
                                         notebook compute: Colab Enterprise runtimes
  mlops cluster-policies list            runtime templates (machine, disk, idle shutdown)
  mlops schemas list | get DATASET       BigQuery datasets
  mlops tables list DATASET | get TABLE | sample TABLE [-n N]
  mlops lineage get NODE                 one hop up and down (table:p.d.t, path:gs://..)
  mlops experiments list | get ID
  mlops registered-models list | get ID | set-alias ID ALIAS VERSION | delete-alias ID ALIAS
  mlops model-versions get MODEL VERSION
  mlops serving-endpoints list | get ID
  mlops feature-groups list | get NAME
  mlops quality-monitors list TABLE | get MONITOR | list-refreshes MONITOR | run-refresh MONITOR
                                         (MONITOR: BUNDLE/TARGET/KEY or its dashboard link)
  mlops deployments list | get BUNDLE    each bundle's deploys per target

Every command takes -o json for the whole answer; text shows the useful part."""
from __future__ import annotations

import argparse
import json
import sys

from gcpbricks import workspace as W


def _csv(v: str) -> list[str]:
    return [x for x in (v or "").split(",") if x]


def _pairs(raw: list[str]) -> dict[str, str]:
    out = {}
    for item in raw:
        for pair in item.split(","):
            if pair:
                k, sep, v = pair.partition("=")
                if not sep:
                    sys.exit(f"mlops: {pair!r} is not k=v")
                out[k] = v
    return out


def _table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    cells = [[("" if r.get(c) is None else str(r.get(c))).replace("\n", " ")[:80] for c in cols] for r in rows]
    widths = [max(len(c), *(len(x[i]) for x in cells)) for i, c in enumerate(cols)]
    print("  ".join(c.upper().ljust(w) for c, w in zip(cols, widths)).rstrip())
    for x in cells:
        print("  ".join(v.ljust(w) for v, w in zip(x, widths)).rstrip())


def _json(v) -> None:
    print(json.dumps(v, indent=2, default=str))


def _monitor(arg: str) -> tuple[str, str, str]:
    if "/quality/" in arg:
        r = W.resolve(arg)
        return r["bundle"], r["target"], r["key"]
    parts = arg.split("/")
    if len(parts) != 3:
        sys.exit("mlops: a monitor is BUNDLE/TARGET/KEY or its dashboard link")
    return parts[0], parts[1], parts[2]


def _table_ref(arg: str) -> tuple[str, str, str | None]:
    """dataset.table, project.dataset.table, or a catalog link."""
    if "/" in arg:
        r = W.resolve(arg)
        return r["dataset"], r["table"], r.get("project")
    parts = arg.split(".")
    if len(parts) == 3:
        return parts[1], parts[2], parts[0]
    if len(parts) == 2:
        return parts[0], parts[1], None
    sys.exit("mlops: a table is DATASET.TABLE or PROJECT.DATASET.TABLE")


def _when(j: dict) -> str:
    """A job's start, as the jobs list shows it: retired, its cron or trigger, paused."""
    if j.get("retired"):
        return "retired"
    s = j.get("schedule") or j.get("trigger")
    if not s:
        return "manual"
    return f"{s.get('cron') or 'trigger'} {s.get('time_zone') or ''}".strip() + (" (paused)" if s.get("paused") else "")


def _runs_rows(runs: list[dict]) -> list[dict]:
    return [{"run": r["id"], "state": "SUCCEEDED_WITH_FAILURES" if r.get("with_failures") else r.get("state"),
             "start": (r.get("start") or "")[:19], "end": (r.get("end") or "")[:19],
             "repair": r.get("repair_id") or "", "error": r.get("error_message") or ""} for r in runs]


def _print_why(w: dict) -> None:
    for p in w["path"]:
        print(f"{p['kind']:<10} {p['label']}  {p.get('state') or ''}  {p.get('link') or ''}")
    if w.get("code"):
        c = w["code"]
        print(f"code       {c.get('commit') or c.get('version', '')}  mlops {c.get('mlops_version', '?')}  "
              f"deployed {c.get('deployed_at', '')} by {c.get('deployed_by', '')}")
    if w["error"]:
        print(f"\nerror: {w['error']}")
    for s in w.get("skipped") or []:
        print(f"\n{s['task']}: skipped  {s['pipeline']}")
        for c in s["skipped"]:
            print(f"  {c['step']}: {c['condition']} did not hold")
    if w["logs"]:
        print("\nlast log lines:")
        for line in w["logs"]:
            print(f"  {line}")


# --- handlers: (args, gcp) -> printed answer ---------------------------------------------------

def _jobs(a, g) -> None:
    from gcpbricks.console import service as S

    c = a.cmd
    if c == "list":
        jobs = S.list_jobs(g, a.target, 1)
        if a.output == "json":
            return _json(jobs)
        return _table([{"workflow": j["workflow"], "bundle": j["bundle"], "target": j["target"],
                        "last_run": (j["runs"] or [{}])[0].get("state", ""), "schedule": _when(j)} for j in jobs],
                      ["workflow", "bundle", "target", "last_run", "schedule"])
    if c == "get":
        j = S.job_detail(g, W.workflow_of(a.job), 10)
        if a.output == "json":
            return _json(j)
        _json({**W._job(j), "parameters": j.get("parameters"), "tasks": sorted(j["tasks"]),
               "settings": j.get("settings")})
        print()
        return _table(_runs_rows(j["runs"]), ["run", "state", "start", "end", "repair", "error"])
    if c == "list-runs":
        runs = S.job_detail(g, W.workflow_of(a.job), a.n)["runs"]
        return _json(runs) if a.output == "json" else _table(_runs_rows(runs), ["run", "state", "start", "end", "repair", "error"])
    wf, run = W.run_of(a.job, getattr(a, "run", None)) if c in ("get-run", "get-run-output", "why", "cancel-run",
                                                                  "repair-run") else (None, None)
    if c == "get-run":
        d = S.run_detail(g, wf, run)
        return _json(d if a.output == "json" else W._run(d))
    if c == "why":
        w = W.why(g, wf, run, a.task)
        return _json(w) if a.output == "json" else _print_why(w)
    if c == "get-run-output":
        if not a.task:
            w = W.why(g, wf, run)
            return _json(w) if a.output == "json" else _print_why(w)
        if a.step:
            from gcpbricks.console import pipeline_view as PV
            lines = PV.step(g, wf, run, a.task, a.step)["logs"]
        else:
            lines = S.task_logs(g, wf, run, a.task)
        if a.output == "json":
            return _json(lines[-a.n:])
        for line in W._logs(lines, a.n):
            print(line)
        return None
    if c == "run-now":
        wf = W.workflow_of(a.job)
        r = S.run_now(g, W.whoami(), wf, _pairs(a.params), _csv(a.only))
        return _json({**r, "workflow": wf})
    if c == "cancel-run":
        return _json(S.cancel(g, W.whoami(), wf, run))
    if c == "cancel-all-runs":
        wf = W.workflow_of(a.job)
        active = [e["name"].rsplit("/", 1)[-1] for e in g.executions(wf, 50) if e.get("state") == "ACTIVE"]
        return _json({"cancelled": [S.cancel(g, W.whoami(), wf, x)["cancelled"] for x in active]})
    if c == "repair-run":
        params = {k: (v or None) for k, v in _pairs(a.params).items()}
        return _json(S.repair(g, W.whoami(), wf, run, _csv(a.models), _csv(a.tasks), params or None,
                              a.latest_repair_id, None, None, _csv(a.keep)))
    if c in ("pause", "resume"):
        return _json(S.set_paused(g, W.whoami(), W.workflow_of(a.job), c == "pause"))
    if c == "delete":
        return _json(S.delete_job(g, W.whoami(), W.workflow_of(a.job)))
    return None


def _clusters(a, g) -> None:
    from gcpbricks.console import notebooks as NB

    c = a.cmd
    if c == "list":
        rs = NB.runtimes(g)
        return _json(rs) if a.output == "json" else _table(rs, ["id", "name", "user", "state", "machine", "template"])
    if c == "get":
        return _json(NB.runtime(g, a.id))
    if c == "create":
        return _json(NB.create_runtime(g, W.whoami(), a.template, a.name or ""))
    return _json(NB.set_runtime(g, W.whoami(), a.id, c))


def _cluster_policies(a, g) -> None:
    from gcpbricks.console import notebooks as NB

    ts = NB.templates(g)
    return _json(ts) if a.output == "json" else _table(ts, ["id", "name", "machine", "accelerator", "disk_gb", "idle_shutdown"])


def _schemas(a, g) -> None:
    from gcpbricks.console import catalog as C

    if a.cmd == "list":
        ds = C.datasets(g)
        return _json(ds) if a.output == "json" else _table(ds, ["id", "location"])
    d = C.dataset(g, a.name)
    if a.output == "json":
        return _json(d)
    _json({k: v for k, v in d.items() if k != "tables"})
    return _table(d.get("tables", []), ["id", "type", "partitioning", "created"])


def _tables(a, g) -> None:
    from gcpbricks.console import catalog as C

    if a.cmd == "list":
        tables = C.dataset(g, a.schema).get("tables", [])
        return _json(tables) if a.output == "json" else _table(tables, ["id", "type", "partitioning", "created"])
    ds, t, project = _table_ref(a.name)
    if a.cmd == "get":
        return _json(C.table(g, ds, t, project) if a.output == "json" else W.inspect(g, f"{project or W.PROJECT}.{ds}.{t}"))
    s = C.preview(g, ds, t, project, a.n)
    if a.output == "json":
        return _json(s)
    return _table([dict(zip(s["columns"], r)) for r in s["rows"]], s["columns"][:12])


def _lineage(a, g) -> None:
    from gcpbricks.console import catalog as C
    return _json(C.lineage(g, a.node))


def _experiments(a, g) -> None:
    from gcpbricks.console import ml as M

    if a.cmd == "list":
        xs = M.experiments(g)
        return _json(xs) if a.output == "json" else _table(xs, ["id", "name", "updated", "retired"])
    return _json(M.experiment(g, a.id))


def _models(a, g) -> None:
    from gcpbricks.console import ml as M

    c = a.cmd
    if c == "list":
        ms = M.models(g)
        return _json(ms) if a.output == "json" else _table(ms, ["id", "name", "latest", "versions", "aliases", "updated"])
    if c == "get":
        return _json(M.model(g, a.id))
    return _json(M.set_alias(g, W.whoami(), a.id, a.alias, a.version if c == "set-alias" else None))


def _versions(a, g) -> None:
    from gcpbricks.console import ml as M
    return _json({"training": M.version_training(g, a.model, a.version),
                  "features": M.version_features(g, a.model, a.version)})


def _serving(a, g) -> None:
    from gcpbricks.console import ml as M

    if a.cmd == "list":
        es = M.endpoints(g)
        return _json(es) if a.output == "json" or not es else _table(es, [k for k in ("id", "name", "updated") if k in es[0]])
    return _json(M.endpoint(g, a.id))


def _features(a, g) -> None:
    from gcpbricks.console import ml as M

    if a.cmd == "list":
        fs = M.feature_groups(g)
        return _json(fs) if a.output == "json" else _table(fs, ["name", "n_features", "families"])
    return _json(M.feature_group(g, a.name))


def _monitors(a, g) -> None:
    from gcpbricks.console import quality as Q
    from gcpbricks.console import service as S

    if a.cmd == "list":
        ds, t, project = _table_ref(a.table)
        ms = Q.monitors(g, ds, t, project)
        return _json(ms) if a.output == "json" else _table(ms, ["bundle", "target", "key", "profile_type", "workflow"])
    m = Q.monitor(g, *_monitor(a.monitor))
    if a.cmd == "get":
        return _json(m)
    if a.cmd == "list-refreshes":
        runs = S.job_detail(g, m["workflow"], a.n)["runs"]
        return _json(runs) if a.output == "json" else _table(_runs_rows(runs), ["run", "state", "start", "end", "error"])
    return _json({**S.run_now(g, W.whoami(), m["workflow"], {}), "workflow": m["workflow"]})


def _deployments(a, g) -> None:
    from gcpbricks.console import bundles as BN

    if a.cmd == "list":
        bs = BN.list_bundles(g)
        return _json(bs) if a.output == "json" else _table(bs, ["bundle", "target", "mlops_version", "deployed_at", "deployed_by", "jobs"])
    return _json(BN.bundle(g, a.name))


def _inspect(a, g) -> None:
    out = W.inspect(g, a.link)
    if a.output == "json" or "why" not in out:
        return _json(out)
    _json({k: v for k, v in out.items() if k not in ("why", "logs")})
    print()
    _print_why(out["why"])
    return None


# --- parsers ----------------------------------------------------------------------------------

def add(top) -> None:
    """The workspace groups, on the `mlops` parser's subcommands."""
    def group(name: str, help_: str, handler):
        p = top.add_parser(name, help=help_)
        p.set_defaults(handler=handler)
        return p.add_subparsers(dest="cmd", required=True)

    def cmd(sub, name: str, *args: str, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("-o", "--output", choices=["text", "json"], default="text")
        for x in args:
            p.add_argument(x)
        return p

    p = top.add_parser("inspect", help="what a link names, and why it failed")
    p.add_argument("link", help="a console page, Workflows execution, Vertex pipeline run, BigQuery job or table")
    p.add_argument("-o", "--output", choices=["text", "json"], default="text")
    p.set_defaults(handler=_inspect, cmd="inspect")

    s = group("jobs", "bundle jobs and their runs", _jobs)
    cmd(s, "list").add_argument("--target")
    cmd(s, "get", "job")
    cmd(s, "list-runs", "job").add_argument("-n", type=int, default=20)
    for name in ("get-run", "why", "get-run-output", "cancel-run", "repair-run"):
        p = cmd(s, name, "job", help="RUN: the run's link, or the job and the run id")
        p.add_argument("run", nargs="?")
        if name in ("why", "get-run-output"):
            p.add_argument("--task")
        if name == "get-run-output":
            p.add_argument("--step", help="a vertex_pipeline_task's pipeline step")
            p.add_argument("-n", type=int, default=200, help="last N lines")
        if name == "repair-run":
            p.add_argument("--tasks", default="", help="t,t: succeeded tasks to run again too")
            p.add_argument("--models", default="", help="m,m: a batch_serve_task's models to run again in full")
            p.add_argument("--keep", default="", help="t,t: failed tasks not to run again")
            p.add_argument("--params", action="append", default=[], help="k=v,k2=: override parameters; k= drops one")
            p.add_argument("--latest-repair-id", type=int, help="the latest repair you looked at (0: none)")
    p = cmd(s, "run-now", "job")
    p.add_argument("--params", action="append", default=[], help="k=v[,k=v]")
    p.add_argument("--only", default="", help="t,t: these tasks only")
    cmd(s, "cancel-all-runs", "job")
    cmd(s, "pause", "job")
    cmd(s, "resume", "job")
    cmd(s, "delete", "job")

    s = group("clusters", "notebook compute (Colab Enterprise runtimes)", _clusters)
    cmd(s, "list")
    for name in ("get", "start", "stop", "delete"):
        cmd(s, name, "id")
    p = cmd(s, "create")
    p.add_argument("--template", required=True, help="a cluster policy's id (`mlops cluster-policies list`)")
    p.add_argument("--name")
    s = group("cluster-policies", "runtime templates", _cluster_policies)
    cmd(s, "list")

    s = group("schemas", "BigQuery datasets", _schemas)
    cmd(s, "list")
    cmd(s, "get", "name")
    s = group("tables", "BigQuery tables", _tables)
    cmd(s, "list", "schema")
    cmd(s, "get", "name")
    cmd(s, "sample", "name").add_argument("-n", type=int, default=20)
    s = group("lineage", "table, path and model lineage", _lineage)
    cmd(s, "get", "node", help="table:project.dataset.table, path:gs://..., model:ID@VERSION")

    s = group("experiments", "MLflow experiments", _experiments)
    cmd(s, "list")
    cmd(s, "get", "id")
    s = group("registered-models", "registered models and their aliases", _models)
    cmd(s, "list")
    cmd(s, "get", "id")
    cmd(s, "set-alias", "id", "alias", "version")
    cmd(s, "delete-alias", "id", "alias")
    s = group("model-versions", "a model version's training and features", _versions)
    cmd(s, "get", "model", "version")
    s = group("serving-endpoints", "model serving endpoints", _serving)
    cmd(s, "list")
    cmd(s, "get", "id")
    s = group("feature-groups", "feature store groups", _features)
    cmd(s, "list")
    cmd(s, "get", "name")

    s = group("quality-monitors", "data profiling monitors", _monitors)
    cmd(s, "list", "table")
    cmd(s, "get", "monitor")
    cmd(s, "list-refreshes", "monitor").add_argument("-n", type=int, default=20)
    cmd(s, "run-refresh", "monitor")
    s = group("deployments", "bundle deploys per target", _deployments)
    cmd(s, "list")
    cmd(s, "get", "name")


def run(a: argparse.Namespace) -> int:
    import requests

    try:
        a.handler(a, W.client())
    except (KeyError, ValueError, PermissionError) as e:
        sys.exit(f"mlops: {str(e).strip(chr(39))}")
    except requests.HTTPError as e:
        detail = e.response.text[:300] if e.response is not None else str(e)
        sys.exit(f"mlops: GCP refused it ({e.response.status_code if e.response is not None else '?'}): {detail}")
    return 0
