"""Where the project's BigQuery cost comes from: every query job of the window, at list price,
put on what ran it and on the tables it wrote and read, and each model's data rolled up along
the lineage those jobs draw.

One query over INFORMATION_SCHEMA.JOBS_BY_PROJECT (JOBS_BY_USER when the console may not list
every user's jobs: then only its own show), grouped by what attributes a job:

    mlops_exec label         a platform execution: its Cloud Run job's bundle and job, or a
                             Vertex AI custom job's labels
    dbt_invocation_id label  a dbt command: the platform's when a dbt_task's run_results
                             (task_runner.save_run_results) name the invocation, which gives its
                             bundle, job and task; else dbt run elsewhere, by profile and target
    dbt's query comment      the model (node_id) the statement built
    neither                  whoever ran it (user_email)

A job's whole cost goes to the table it wrote (destination_table), so the tables' build costs
add up to the jobs'. A job's bytes are not split by the tables it read: a table's read cost is
the whole cost of the jobs that read it, and those overlap. Each job that read S and wrote T
is a lineage edge S -> T; a model's upstream is everything those edges reach back from its
feature and label tables (catalog.model_flows), and its data cost is the build cost of those
tables, each once. A table shared by models counts in each one's.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import requests

from gcpbricks.console import catalog as C
from gcpbricks.console import prices as PR
from gcpbricks.console import service as S
from gcpbricks.console.costs import TIB, _iso
from gcpbricks.job_labels import LABEL

#: Upstream walks stop this many hops back: dbt DAGs are far shallower.
MAX_DEPTH = 30
#: run_results objects read once per console instance: a finished command's never change.
_INVOCATIONS: dict[str, dict | None] = {}

_SQL = r"""
SELECT FORMAT_DATE('%F', DATE(creation_time)) AS day, user_email,
  (SELECT value FROM UNNEST(labels) WHERE key = '{label}') AS exec,
  (SELECT value FROM UNNEST(labels) WHERE key = 'dbt_invocation_id') AS inv,
  REGEXP_EXTRACT(query, r'"node_id"\s*:\s*"([^"]+)"') AS node,
  REGEXP_EXTRACT(query, r'"profile_name"\s*:\s*"([^"]+)"') AS profile,
  REGEXP_EXTRACT(query, r'"target_name"\s*:\s*"([^"]+)"') AS target,
  IF(destination_table.dataset_id IS NULL OR STARTS_WITH(destination_table.dataset_id, '_'), NULL,
     FORMAT('%s.%s.%s', destination_table.project_id, destination_table.dataset_id, destination_table.table_id)) AS dest,
  ARRAY_TO_STRING(ARRAY(SELECT DISTINCT FORMAT('%s.%s.%s', r.project_id, r.dataset_id, r.table_id)
                        FROM UNNEST(referenced_tables) r WHERE NOT STARTS_WITH(r.dataset_id, '_') ORDER BY 1), ',') AS refs,
  COUNT(*) AS jobs, SUM(IFNULL(total_bytes_billed, 0)) AS billed, SUM(IFNULL(total_slot_ms, 0)) AS slot_ms
FROM `region-{location}`.INFORMATION_SCHEMA.{view}
WHERE creation_time >= TIMESTAMP('{start}') AND creation_time < TIMESTAMP('{end}')
  AND job_type = 'QUERY' AND IFNULL(statement_type, '') != 'SCRIPT'
GROUP BY day, user_email, exec, inv, node, profile, target, dest, refs"""

_COLS = ("day", "user", "exec", "inv", "node", "profile", "target", "dest", "refs", "jobs", "billed", "slot_ms")


def _query_all(gcp, sql: str) -> list[dict]:
    """Every row of a query, page by page."""
    r = gcp.bq_query({"query": sql, "useLegacySql": False, "timeoutMs": 60000, "maxResults": 20000})
    rows, ref = list(r.get("rows", [])), r["jobReference"]
    token = r.get("pageToken")
    while token:
        r = gcp._get(f"https://bigquery.googleapis.com/bigquery/v2/projects/{gcp.project}/queries/{ref['jobId']}",
                     location=ref.get("location"), pageToken=token, maxResults=20000)
        rows += r.get("rows", [])
        token = r.get("pageToken")
    return [dict(zip(_COLS, (c["v"] for c in row["f"]))) for row in rows]


def job_rows(gcp, start, end, location: str) -> tuple[list[dict], str]:
    """The window's query jobs, grouped; and "project", or "user" when only the console's own
    could be listed."""
    for view, scope in (("JOBS_BY_PROJECT", "project"), ("JOBS_BY_USER", "user")):
        sql = _SQL.format(label=LABEL, location=location, view=view, start=_iso(start), end=_iso(end))
        try:
            rows = _query_all(gcp, sql)
        except requests.HTTPError as e:
            if scope == "project" and e.response is not None and e.response.status_code == 403:
                continue
            raise
        for r in rows:
            r["jobs"], r["billed"], r["slot_ms"] = int(r["jobs"]), int(r["billed"] or 0), int(r["slot_ms"] or 0)
            r["refs"] = [t for t in (r["refs"] or "").split(",") if t]
        return rows, scope
    raise AssertionError("unreachable")


# --- who ran a job -------------------------------------------------------------------------

def _platform(gcp) -> tuple[dict[str, list[dict]], list[dict]]:
    """Cloud Run job -> the jobs that run it ({bundle, job, workflow}: a bundle's dbt_tasks
    without a job cluster share one), and every dbt_task: {bundle, job, workflow, task, run_job,
    files, commands}, from the deployed jobs' definitions."""
    jobs = [j for wf in gcp.workflows() if (j := S._job_of(wf))]

    def tasks(j: dict) -> list[tuple[str, dict]]:
        try:
            d = gcp.read_json(j["bucket"], j["definition"])
        except requests.HTTPError:
            return []
        out = []
        for key, t in (d.get("tasks") or {}).items():
            t = t.get("task", t) if t.get("type") == "for_each" else t
            if t.get("type") == "cloud_run":
                out.append((key, t))
        return out
    with ThreadPoolExecutor(8) as pool:
        per_job = list(pool.map(tasks, jobs))
    run_jobs: dict[str, list[dict]] = defaultdict(list)
    dbt = []
    for j, ts in zip(jobs, per_job):
        who = {"bundle": j["bundle"], "job": j["job"], "workflow": j["workflow"]}
        for key, t in ts:
            if who not in run_jobs[t["run_job"]]:
                run_jobs[t["run_job"]].append(who)
            args = t.get("args") or []
            if args[:2] == ["mlops-task", "dbt"] and "--files" in args:
                dbt.append(who | {"task": key, "run_job": t["run_job"], "files": args[args.index("--files") + 1],
                                  "commands": args[args.index("--") + 1:] if "--" in args else []})
    return run_jobs, dbt


def _invocations(gcp, dbt_tasks: list[dict], since: str) -> dict[str, dict]:
    """dbt invocation id -> {bundle, job, workflow, task, command, execution}: the run_results
    the platform's dbt_tasks kept since `since` (an execution is "<Cloud Run job>-<5 chars>")."""
    by_files: dict[str, list[dict]] = defaultdict(list)
    for t in dbt_tasks:
        by_files[t["files"]].append(t)

    def objects(files_uri: str) -> list[tuple[str, str, dict]]:
        bucket, _, prefix = f"{files_uri.removesuffix('/files')}/runs/".removeprefix("gs://").partition("/")
        try:
            objs = gcp.list_objects(bucket, prefix, "**/dbt-*.json")
        except requests.HTTPError:
            return []
        return [(bucket, o["name"], {"files": files_uri}) for o in objs if (o.get("timeCreated") or "") >= since]
    with ThreadPoolExecutor(8) as pool:
        found = [o for objs in pool.map(objects, by_files) for o in objs]

    def read(o: tuple[str, str, dict]) -> tuple[str, dict | None]:
        bucket, name, _ = o
        key = f"{bucket}/{name}"
        if key not in _INVOCATIONS:
            try:
                rr = gcp.read_json(bucket, name)
                _INVOCATIONS[key] = {"invocation_id": rr.get("invocation_id"), "command": rr.get("command")}
            except requests.HTTPError:
                _INVOCATIONS[key] = None
        return key, _INVOCATIONS[key]
    with ThreadPoolExecutor(16) as pool:
        got = list(pool.map(read, found))
    out = {}
    for (bucket, name, meta), (_, rr) in zip(found, got):
        if not rr or not rr.get("invocation_id"):
            continue
        m = re.search(r"/runs/([^/]+)/", name)
        execution = m[1] if m else ""
        run_job = re.sub(r"-[a-z0-9]{5}$", "", execution)
        cands = [t for t in by_files[meta["files"]] if t["run_job"] == run_job] or by_files[meta["files"]]
        hits = [t for t in cands if any(_runs(c, rr.get("command") or "") for c in t["commands"])]
        # No declared command matches: the bundle (whose files it ran) is still known.
        out[rr["invocation_id"]] = _one(hits or [{"bundle": t["bundle"]} for t in cands]) | {
            "command": rr.get("command"), "execution": execution}
    return out


def _runs(declared: str, ran: str) -> bool:
    """Whether a command as the task declares it ran as `ran`: its {{job.parameters...}} are
    filled in at run time, so each stands for any text."""
    parts = [p.strip() for p in re.split(r"\{\{.*?\}\}", " ".join(declared.split()))]
    pattern = r"\s*.*?\s*".join(re.escape(p) for p in parts)
    return re.fullmatch(pattern, " ".join(ran.split()), re.DOTALL) is not None


def _one(whos: list[dict]) -> dict:
    """The one task (or job) these candidates agree on; what they don't agree on is left None,
    never guessed: a bundle's dbt_tasks share one Cloud Run job, and a command can be any job's."""
    agree = lambda k: whos[0].get(k) if whos and all(w.get(k) == whos[0].get(k) for w in whos) else None
    job = agree("job")
    return {"bundle": agree("bundle"), "job": job, "workflow": agree("workflow") if job else None,
            "task": agree("task") if job else None}


def _vertex_owner(gcp, job_id: str, cache: dict) -> dict | None:
    if job_id not in cache:
        try:
            labels = gcp.ai_get(f"customJobs/{job_id}").get("labels") or {}
        except requests.HTTPError:
            labels = {}
        cache[job_id] = ({"bundle": labels["mlops_bundle"], "job": labels.get("mlops_job") or labels.get("mlops_task")}
                         if labels.get("mlops_bundle") else None)
    return cache[job_id]


def owner(r: dict, run_jobs: dict, invocations: dict, vertex: dict) -> dict:
    """What ran a grouped row: {kind, bundle, job, workflow, task, model, profile, target, user},
    the keys its kind has. kind: platform (an execution or a dbt_task), dbt (dbt elsewhere), user."""
    model = r["node"]
    if r["inv"] and r["inv"] in invocations:
        i = invocations[r["inv"]]
        return {"kind": "platform", **{k: i[k] for k in ("bundle", "job", "workflow", "task")}, "model": model}
    if r["exec"]:
        if r["exec"].isdigit():
            v = vertex.get(r["exec"])
            if v:
                return {"kind": "platform", "bundle": v["bundle"], "job": v["job"], "workflow": None, "task": None,
                        "model": model}
        else:
            whos = run_jobs.get(re.sub(r"-[a-z0-9]{5}$", "", r["exec"]))
            if whos:
                return {"kind": "platform", **_one(whos), "task": None, "model": model}
    if r["inv"] or model:
        return {"kind": "dbt", "profile": r["profile"], "target": r["target"], "user": r["user"], "model": model}
    return {"kind": "user", "user": r["user"]}


def _path(o: dict) -> list[str]:
    """An owner as the tree the page draws: platform > bundle > job > task > model,
    dbt > profile.target > user > model, other > user."""
    if o["kind"] == "platform":
        return ["Platform", o["bundle"] or "(bundle unknown)", o["job"] or "(job unknown)", o["task"] or "(other queries)",
                *([o["model"]] if o.get("model") else [])]
    if o["kind"] == "dbt":
        return ["dbt outside the platform", f"{o['profile'] or '?'}.{o['target'] or '?'}", o["user"] or "?",
                o["model"] or "(no model)"]
    return ["Other queries", o["user"] or "?"]


# --- the attribution -----------------------------------------------------------------------

_TEMP_SUFFIX = re.compile(r"(__dbt_tmp\d*|__staging_\d+)$")


def canonical(fqn: str | None) -> str | None:
    """The table a job really wrote or read: dbt's `<t>__dbt_tmp<n>` and a monitor refresh's
    `<t>__staging_<time>` are `<t>`'s, and a table named `_...` is a temporary (a loader's staging
    copy), no table of its own."""
    if not fqn:
        return None
    project, ds, tb = fqn.split(".", 2)
    if tb.startswith("_"):
        return None
    return f"{project}.{ds}.{_TEMP_SUFFIX.sub('', tb)}"


def attribute(gcp, start, end, location: str, usd_per_tib: float) -> dict:
    rows, scope = job_rows(gcp, start, end, location)
    for r in rows:
        r["dest"] = canonical(r["dest"])
        r["refs"] = sorted({c for t in r["refs"] if (c := canonical(t))})
    run_jobs, dbt_tasks = _platform(gcp)
    invocations = _invocations(gcp, dbt_tasks, _iso(start)) if any(r["inv"] for r in rows) else {}
    vertex: dict = {}
    for job_id in {r["exec"] for r in rows if r["exec"] and r["exec"].isdigit()}:
        vertex[job_id] = _vertex_owner(gcp, job_id, vertex)
    usd = lambda billed: billed / TIB * usd_per_tib

    owners: dict[tuple, dict] = {}
    built: dict[str, dict] = {}
    read: dict[str, dict] = {}
    edges: dict[str, set[str]] = defaultdict(set)  # table -> what it was built from
    for r in rows:
        o = owner(r, run_jobs, invocations, vertex)
        cost = usd(r["billed"])
        path = tuple(_path(o))
        it = owners.setdefault(path, {"path": list(path), "kind": o["kind"], "workflow": o.get("workflow"),
                                      "usd": 0.0, "tib": 0.0, "jobs": 0, "slot_ms": 0, "tables": set()})
        it["usd"] += cost
        it["tib"] += r["billed"] / TIB
        it["jobs"] += r["jobs"]
        it["slot_ms"] += r["slot_ms"]
        who = " / ".join(path[1:])
        if r["dest"]:
            it["tables"].add(r["dest"])
            b = built.setdefault(r["dest"], {"table": r["dest"], "usd": 0.0, "tib": 0.0, "jobs": 0,
                                             "by_day": defaultdict(float), "by": defaultdict(float)})
            b["usd"] += cost
            b["tib"] += r["billed"] / TIB
            b["jobs"] += r["jobs"]
            b["by_day"][r["day"]] += cost
            b["by"][who] += cost
            edges[r["dest"]].update(t for t in r["refs"] if t != r["dest"])
        for t in r["refs"]:
            x = read.setdefault(t, {"table": t, "jobs_usd": 0.0, "jobs": 0, "by": defaultdict(float)})
            x["jobs_usd"] += cost
            x["jobs"] += r["jobs"]
            x["by"][who] += cost
    unattributed = sum(r["billed"] for r in rows if not r["dest"])
    return {"scope": scope, "usd_per_tib": usd_per_tib, "owners": owners, "built": built, "read": read, "edges": edges,
            "total_usd": sum(usd(r["billed"]) for r in rows), "no_table_usd": usd(unattributed),
            "invocations_found": len(invocations)}


def upstream(edges: dict[str, set[str]], roots: list[str]) -> dict[str, int]:
    """Every table the edges reach back from `roots`, with its distance (roots are 0)."""
    seen = {t: 0 for t in roots}
    frontier = list(roots)
    for depth in range(1, MAX_DEPTH + 1):
        nxt = []
        for t in frontier:
            for s in edges.get(t, ()):
                if s not in seen:
                    seen[s] = depth
                    nxt.append(s)
        if not nxt:
            break
        frontier = nxt
    return seen


def _tables(a: dict, reach: dict[str, int]) -> list[dict]:
    """Reached tables with their build cost in the window, costliest first."""
    rows = []
    for t, d in reach.items():
        b = a["built"].get(t)
        rows.append({"table": t, "depth": d, "usd": round(b["usd"], 4) if b else 0.0, "built": b is not None})
    return sorted(rows, key=lambda x: (-x["usd"], x["depth"]))


def _top(by: dict, n: int = 5) -> list[dict]:
    return [{"name": k, "usd": round(v, 4)} for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:n]]


def models(gcp, a: dict) -> list[dict]:
    """Each model's serving version with the build cost of its data: its feature and label
    tables and everything upstream of them."""
    out = []
    for f in C.model_flows(gcp):
        roots = [t for t in (f.get("features"), f.get("labels")) if t]
        if not roots:
            continue
        tables = _tables(a, upstream(a["edges"], roots))
        out.append({"model": f["model"], "name": f["name"], "version": f["version"], "roots": roots,
                    "usd": round(sum(x["usd"] for x in tables), 4), "tables": tables,
                    # A root nothing in the window built, with no edges back: a view, or a table
                    # loaded outside BigQuery's query jobs; its upstream can't be followed from jobs.
                    "unresolved": [t for t in roots if t not in a["built"] and not a["edges"].get(t)]})
    return sorted(out, key=lambda m: -m["usd"])


def report(gcp, hit: dict) -> dict:
    """The Costs page's BigQuery breakdown, from cached()'s attribution."""
    a = hit["a"]
    owners = [{**{k: v for k, v in o.items() if k != "tables"}, "usd": round(o["usd"], 4), "tib": round(o["tib"], 4),
               "tables": sorted(o["tables"])} for o in a["owners"].values()]
    built = [{**{k: v for k, v in b.items() if k not in ("by_day", "by")}, "usd": round(b["usd"], 4),
              "tib": round(b["tib"], 4), "by": _top(b["by"])} for b in a["built"].values()]
    read = [{"table": x["table"], "jobs": x["jobs"], "jobs_usd": round(x["jobs_usd"], 4), "by": _top(x["by"])}
            for x in a["read"].values()]
    try:
        model_costs, model_error = models(gcp, a), None
    except Exception as e:  # noqa: BLE001 - the rest of the breakdown stands without it
        model_costs, model_error = [], f"{type(e).__name__}: {e}"
    return {"scope": scope_note(a["scope"]), "total_usd": round(a["total_usd"], 2),
            "no_table_usd": round(a["no_table_usd"], 2), "invocations_found": a["invocations_found"],
            "owners": sorted(owners, key=lambda o: -o["usd"]),
            "built": sorted(built, key=lambda b: -b["usd"]),
            "read": sorted(read, key=lambda x: -x["jobs_usd"]),
            "models": model_costs, "models_error": model_error, "usd_per_tib": a["usd_per_tib"],
            "window": {"start": _iso(hit["start"]), "end": _iso(hit["end"]), "days": len(hit["days"])}}


def scope_note(scope: str) -> dict:
    return {"kind": scope, "text": "Every user's query jobs in the project." if scope == "project" else
            "Only the console's own query jobs: it may not list other users' (bigquery.jobs.listAll)."}


def table(hit: dict, fqn: str) -> dict:
    """One table's cost: what building it cost (by day, by whom), who read it, and the build
    cost of everything upstream of it."""
    a, days = hit["a"], hit["days"]
    b = a["built"].get(fqn)
    x = a["read"].get(fqn)
    ups = [u for u in _tables(a, upstream(a["edges"], [fqn])) if u["table"] != fqn]
    downs = sorted(t for t, srcs in a["edges"].items() if fqn in srcs)
    return {"table": fqn, "scope": scope_note(a["scope"]),
            "build": {"usd": round(b["usd"], 4), "tib": round(b["tib"], 4), "jobs": b["jobs"],
                      "by_day": [{"day": d, "usd": round(b["by_day"].get(d, 0.0), 4)} for d in days],
                      "by": _top(b["by"], 20)} if b else None,
            "read": {"jobs": x["jobs"], "jobs_usd": round(x["jobs_usd"], 4), "by": _top(x["by"], 20)} if x else None,
            "upstream": ups, "upstream_usd": round(sum(u["usd"] for u in ups), 4),
            "built_from": sorted(a["edges"].get(fqn, ())), "feeds": downs}


_CACHE: dict = {}


def usd_per_tib(gcp, location: str) -> float:
    """BigQuery's on-demand list price per TiB where its jobs run, as the Costs page prices it."""
    skus_of, _ = PR.catalog(gcp)
    prices = PR.Prices(skus_of, gcp.region)
    p = prices.bigquery_tib(location) or (prices.bigquery_tib("us") if location.startswith("us-") else None)
    if p is None:
        raise LookupError(f"the catalog has no BigQuery analysis price for {location}")
    return p["usd"]


def cached(gcp, days: int, location: str, usd_per_tib: float, now: datetime | None = None) -> dict:
    """The attribution for the last `days` UTC days, kept ten minutes."""
    hit = _CACHE.get(days)
    if hit and time.time() - hit["at"] < 600:
        return hit
    now = now or datetime.now(UTC)
    start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=days - 1)
    a = attribute(gcp, start, now, location, usd_per_tib)
    hit = {"at": time.time(), "a": a, "days": [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)],
           "start": start, "end": now}
    _CACHE[days] = hit
    return hit
