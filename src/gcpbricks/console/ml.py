"""The console's AI/ML section over Vertex AI: experiments and their runs, registered
models and their versions and aliases, serving endpoints, and the feature groups dbt-features
publishes (gcpbricks.features)."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from gcpbricks.ml import RETIRED

#: Where dbt-features's CI publishes the feature groups (features.DEFAULT_CONFIG).
FEATURE_CONFIG = os.environ.get("MLOPS_FEATURE_CONFIG", "gs://my-ml-bucket/feature_store/features.json")
EXPERIMENT = 'schema_title="system.Experiment"'
#: A nested run's parent, as gcpbricks.mlflow logs MLflow's mlflow.parentRunId tag.
PARENT_PARAM = "tag_mlflow.parentRunId"


def _id(resource: str) -> str:
    return resource.rsplit("/", 1)[-1]


# --- experiments ---------------------------------------------------------------------------

def experiments(gcp) -> list[dict]:
    out = [{"id": _id(c["name"]), "name": c.get("displayName", _id(c["name"])),
            "created": c.get("createTime"), "updated": c.get("updateTime"),
            "description": c.get("description", ""), "retired": (c.get("labels") or {}).get(RETIRED)}
           for c in gcp.ai_list("metadataStores/default/contexts", "contexts", filter=EXPERIMENT, pageSize=100)
           if not (c.get("metadata") or {}).get("experiment_deleted")]
    return sorted(out, key=lambda e: e["updated"] or "", reverse=True)


def experiment(gcp, experiment_id: str) -> dict:
    ctx = gcp.ai_get(f"metadataStores/default/contexts/{experiment_id}")
    runs = []
    for r in gcp.ai_list("metadataStores/default/contexts", "contexts",
                         filter=f'parent_contexts:"{ctx["name"]}"', pageSize=100):
        meta = r.get("metadata") or {}
        runs.append({"id": _id(r["name"]), "name": r.get("displayName", _id(r["name"])),
                     "type": "Pipeline run" if r.get("schemaTitle") == "system.PipelineRun" else "Run",
                     "created": r.get("createTime"), "updated": r.get("updateTime"),
                     "state": meta.get("_state") or meta.get("state") or "",
                     "params": meta.get("_params") or {}, "metrics": meta.get("_metrics") or {},
                     # The parent's run name: the run that ran it (a sweep's trial, under the sweep).
                     "parent": (meta.get("_params") or {}).get(PARENT_PARAM)})
    runs.sort(key=lambda r: r["created"] or "", reverse=True)
    return {"id": experiment_id, "name": ctx.get("displayName", experiment_id),
            "created": ctx.get("createTime"), "updated": ctx.get("updateTime"),
            "retired": (ctx.get("labels") or {}).get(RETIRED), "runs": runs}


# --- registered models ---------------------------------------------------------------------

def _version(v: dict) -> dict:
    return {"version": v.get("versionId"), "aliases": [a for a in v.get("versionAliases") or [] if a != "default"],
            "default": "default" in (v.get("versionAliases") or []), "labels": v.get("labels") or {},
            "created": v.get("versionCreateTime") or v.get("createTime"),
            "updated": v.get("versionUpdateTime") or v.get("updateTime"),
            "description": v.get("versionDescription") or v.get("description", ""),
            "artifact_uri": v.get("artifactUri", "")}


def _versions(gcp, model_name: str) -> list[dict]:
    vs = [_version(v) for v in gcp.ai_list(f"{model_name}:listVersions", "models", pageSize=100)]
    return sorted(vs, key=lambda v: int(v["version"] or 0), reverse=True)


#: The registered models with their versions, kept MODELS_TTL seconds per client (listing
#: them and every model's versions takes seconds); an alias moved here drops the copy.
MODELS_TTL = 60
_MODELS: dict[int, tuple[float, list[dict]]] = {}


def models(gcp) -> list[dict]:
    hit = _MODELS.get(id(gcp))
    if hit and time.monotonic() - hit[0] < MODELS_TTL:
        return hit[1]
    out = _list_models(gcp)
    _MODELS[id(gcp)] = (time.monotonic(), out)
    return out


def _list_models(gcp) -> list[dict]:
    listed = gcp.ai_list("models", "models", pageSize=100)

    def one(m: dict) -> dict:
        vs = _versions(gcp, m["name"])
        return {"id": _id(m["name"]), "name": m.get("displayName"), "description": m.get("description", ""),
                "latest": vs[0]["version"] if vs else None, "versions": len(vs),
                "aliases": {a: v["version"] for v in vs for a in v["aliases"]},
                "updated": max((v["updated"] or "" for v in vs), default=m.get("updateTime")),
                "retired": (m.get("labels") or {}).get(RETIRED)}
    with ThreadPoolExecutor(8) as pool:
        out = list(pool.map(one, listed))
    return sorted(out, key=lambda m: m["name"] or "")


def model_experiment(model_name: str) -> str:
    """The experiment a model's training logs to: its name with - for _ (forecast_90d -> forecast-90d);
    each version's training run is named by the version's model_id label."""
    return model_name.replace("_", "-")


def _source_runs(gcp, experiment_id: str) -> dict[str, dict]:
    """{run name: run} of an experiment, {} when the model has no experiment."""
    try:
        return {r["name"]: r for r in experiment(gcp, experiment_id)["runs"]}
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return {}
        raise


def _serving(gcp, model_id: str) -> dict[str, list[dict]]:
    """{version: [the endpoints serving it, with its traffic share]}."""
    out: dict[str, list[dict]] = {}
    for e in endpoints(gcp):
        for d in e["deployed"]:
            if d["model"] == model_id:
                out.setdefault(d["version"], []).append({"id": e["id"], "name": e["name"], "traffic": d["traffic"]})
    return out


def _run_ref(model_name: str, labels: dict) -> tuple[str, str]:
    """(experiment, run name) of a version's training run: gcpbricks.mlflow's labels,
    else demo_model's naming (the model's experiment, the run named by model_id)."""
    if labels.get("mlflow_run_id") and labels.get("mlflow_experiment"):
        return labels["mlflow_experiment"], labels["mlflow_run_id"]
    return model_experiment(model_name), labels.get("model_id", "")


def model(gcp, model_id: str) -> dict:
    """A registered model and its versions, each with what Databricks' version page shows:
    its source run (and that run's params and metrics), the endpoints serving it, its
    container and schemas."""
    m = gcp.ai_get(f"models/{model_id}")
    raw = gcp.ai_list(f"{m['name']}:listVersions", "models", pageSize=100)
    refs = {v.get("versionId"): _run_ref(m.get("displayName") or "", v.get("labels") or {}) for v in raw}
    exps = sorted({e for e, _ in refs.values()})
    with ThreadPoolExecutor(4) as pool:
        serving_f = pool.submit(_serving, gcp, model_id)
        runs = dict(zip(exps, pool.map(lambda e: _source_runs(gcp, e), exps), strict=True))
        serving = serving_f.result()
    vs = []
    for v in raw:
        exp, name = refs[v.get("versionId")]
        run = runs.get(exp, {}).get(name) if name else None
        vs.append({**_version(v), "image": (v.get("containerSpec") or {}).get("imageUri", ""),
                   "schemata": v.get("predictSchemata") or {}, "source": (v.get("modelSourceInfo") or {}).get("sourceType", ""),
                   "source_run": {"experiment": exp, "id": run["id"], "name": run["name"], "state": run["state"],
                                  "params": run["params"], "metrics": run["metrics"]} if run else None,
                   "serving": serving.get(v.get("versionId"), [])})
    vs.sort(key=lambda v: int(v["version"] or 0), reverse=True)
    return {"id": model_id, "name": m.get("displayName"), "description": m.get("description", ""),
            "created": m.get("createTime"), "updated": m.get("updateTime"),
            "labels": m.get("labels") or {}, "retired": (m.get("labels") or {}).get(RETIRED),
            "experiment": next((e for e in exps if runs.get(e)), None), "versions": vs}


def set_alias(gcp, user: str, model_id: str, alias: str, version: str | None) -> dict:
    """Moves `alias` onto `version` (None: removes it). An alias names one version, so it is
    taken off its current holder first, as Databricks moves @champion."""
    _MODELS.pop(id(gcp), None)
    if alias == "default":
        raise ValueError("default is Vertex's own alias; set another")
    m = gcp.ai_get(f"models/{model_id}")
    held = [v["version"] for v in _versions(gcp, m["name"]) if alias in v["aliases"]]
    for v in held:
        if v != version:
            gcp.ai_post(f"{m['name']}@{v}:mergeVersionAliases", {"versionAliases": [f"-{alias}"]})
    if version is not None and version not in held:
        gcp.ai_post(f"{m['name']}@{version}:mergeVersionAliases", {"versionAliases": [alias]})
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "set_alias",
               "model": m.get("displayName"), "alias": alias, "version": version, "from": held})
    return {"model": m.get("displayName"), "alias": alias, "version": version, "from": held}


# --- serving -------------------------------------------------------------------------------

def _endpoint(e: dict) -> dict:
    split = e.get("trafficSplit") or {}
    deployed = [{"id": d["id"], "name": d.get("displayName", ""), "model": _id((d.get("model") or "").split("@")[0]),
                 "version": d.get("modelVersionId"), "traffic": split.get(d["id"], 0),
                 "machine": ((d.get("dedicatedResources") or {}).get("machineSpec") or {}).get("machineType", ""),
                 "replicas": [(d.get("dedicatedResources") or {}).get("minReplicaCount"),
                              (d.get("dedicatedResources") or {}).get("maxReplicaCount")],
                 "created": d.get("createTime")} for d in e.get("deployedModels") or []]
    return {"id": _id(e["name"]), "name": e.get("displayName"), "description": e.get("description", ""),
            "labels": e.get("labels") or {}, "created": e.get("createTime"), "updated": e.get("updateTime"),
            "ready": bool(deployed), "deployed": deployed, "retired": (e.get("labels") or {}).get(RETIRED)}


def endpoints(gcp) -> list[dict]:
    return sorted((_endpoint(e) for e in gcp.ai_list("endpoints", "endpoints", pageSize=100)),
                  key=lambda e: e["updated"] or "", reverse=True)


def endpoint(gcp, endpoint_id: str) -> dict:
    return _endpoint(gcp.ai_get(f"endpoints/{endpoint_id}"))


# --- deleting what deploy retired ----------------------------------------------------------

def _deletable(kind: str, name: str, labels: dict) -> None:
    if RETIRED not in (labels or {}):
        raise ValueError(f"{kind} {name} is not retired: only what its bundle no longer declares can be deleted")


def delete_experiment(gcp, user: str, experiment_id: str) -> dict:
    """Deletes a retired experiment and its runs."""
    ctx = gcp.ai_get(f"metadataStores/default/contexts/{experiment_id}")
    _deletable("experiment", ctx.get("displayName", experiment_id), ctx.get("labels"))
    runs = gcp.ai_list("metadataStores/default/contexts", "contexts", filter=f'parent_contexts:"{ctx["name"]}"', pageSize=100)
    for r in runs:
        gcp.ai_delete(f"metadataStores/default/contexts/{_id(r['name'])}")
    gcp.ai_delete(f"metadataStores/default/contexts/{experiment_id}")
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "delete_experiment",
               "experiment": ctx.get("displayName", experiment_id), "runs": len(runs)})
    return {"deleted": ctx.get("displayName", experiment_id), "runs": len(runs)}


def delete_model(gcp, user: str, model_id: str) -> dict:
    """Deletes a retired registered model with every version; one an endpoint still serves stays."""
    m = gcp.ai_get(f"models/{model_id}")
    _deletable("model", m.get("displayName"), m.get("labels"))
    if _serving(gcp, model_id):
        raise ValueError(f"model {m.get('displayName')} is still deployed to an endpoint: undeploy it first")
    _MODELS.pop(id(gcp), None)
    gcp.ai_delete(f"models/{model_id}")
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "delete_model", "model": m.get("displayName")})
    return {"deleted": m.get("displayName")}


def delete_endpoint(gcp, user: str, endpoint_id: str) -> dict:
    """Deletes a retired endpoint; deploy undeployed its models when it retired it."""
    e = endpoint(gcp, endpoint_id)
    _deletable("endpoint", e["name"], e["labels"])
    if e["deployed"]:
        raise ValueError(f"endpoint {e['name']} still has deployed models: undeploy them first")
    gcp.ai_delete(f"endpoints/{endpoint_id}")
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "delete_endpoint", "endpoint": e["name"]})
    return {"deleted": e["name"]}


# --- features ------------------------------------------------------------------------------

def feature_groups(gcp) -> list[dict]:
    bucket, _, obj = FEATURE_CONFIG.removeprefix("gs://").partition("/")
    groups = gcp.read_json(bucket, obj).get("groups", [])
    out = []
    for g in groups:
        fams: dict[str, int] = {}
        for f in g.get("features", []):
            fams[f.get("family", "default")] = fams.get(f.get("family", "default"), 0) + 1
        out.append({**g, "families": fams, "n_features": len(g.get("features", []))})
    return out


def feature_group(gcp, name: str) -> dict:
    """A published group with its offline table as BigQuery has it now: size, freshness, and
    which declared columns the table lacks or which of its columns aren't declared (served
    only once declared)."""
    from gcpbricks.console import catalog as C

    g = next((x for x in feature_groups(gcp) if x["name"] == name), None)
    if g is None:
        raise KeyError(f"no feature group {name!r} in the published config")
    parts = g["offline_table"].split(".")
    project, dataset, table = parts if len(parts) == 3 else (gcp.project, *parts)
    declared = [*g["keys"], g["timestamp"], *(f["name"] for f in g["features"])]
    try:
        t = C.table(gcp, dataset, table, project if project != gcp.project else None)
    except (KeyError, ValueError) as e:
        return {**g, "live": None, "live_error": str(e).strip("'\""), "declared_not_live": [], "live_not_declared": []}
    live = {f["name"]: f["type"] for f in t["schema"]}
    return {**g, "live": {k: t[k] for k in ("project", "dataset", "id", "full_name", "rows", "bytes", "updated",
                                            "partitioning", "clustering", "previewable")} | {"types": live},
            "live_error": None,
            "declared_not_live": [c for c in declared if c not in live],
            "live_not_declared": [c for c in live if c not in declared]}


# --- a version's features ------------------------------------------------------------------

def _read_json(gcp, uri: str, name: str):
    bucket, _, prefix = uri.removeprefix("gs://").partition("/")
    got = gcp.read_object(bucket, f"{prefix.rstrip('/')}/{name}")
    return got[0] if got else None


def version_features(gcp, model_id: str, version: str) -> dict:
    """What a version reads and is fit on, from its artifacts: features.json (the platform's
    log_model / log_features writes it), else what its MLmodel signature and feature_spec.json
    say. Names and counts, and the columns read but not fit on (and the reverse)."""
    import json

    import yaml

    from gcpbricks.mlflow import features as FT
    from gcpbricks.mlflow.models import ModelSignature

    v = gcp.ai_get(f"models/{model_id}@{version}")
    uri = v.get("artifactUri", "")
    if not uri.startswith("gs://"):
        return {"artifact_uri": uri, "logged": False, "read": None, "model_inputs": {}, "computed": None,
                "signature": [], "read_not_fit": [], "fit_not_read": []}
    rec = _read_json(gcp, uri, FT.FILE)
    mlmodel = _read_json(gcp, uri, "MLmodel")
    sig = ((yaml.safe_load(mlmodel) or {}).get("signature") if mlmodel else None)
    signature = ModelSignature.from_dict(sig) if sig else None
    logged = rec is not None
    if rec is not None:
        rec = json.loads(rec)
    else:
        spec = _read_json(gcp, uri, "feature_spec.json")
        service = None
        if spec:
            from types import SimpleNamespace
            s = json.loads(spec)
            service = SimpleNamespace(name=s.get("name", ""), spine_inputs=s.get("spine_inputs") or [],
                                      feature_names=[r.split(":", 1)[1] for r in s.get("features", [])])
        rec = FT.describe(signature=signature, feature_spec=service) or {"read": None, "model_inputs": None,
                                                                          "computed": None, "sources": {}}
    read = rec.get("read")
    inputs = rec.get("model_inputs") or {}
    fit = list(dict.fromkeys(c for cols in inputs.values() for c in cols))
    return {"artifact_uri": uri, "logged": logged, "sources": rec.get("sources", {}),
            "read": read, "model_inputs": inputs, "computed": rec.get("computed"),
            "signature": [{"name": c.name, "type": c.type} for c in signature.inputs.inputs] if signature else [],
            "read_not_fit": [c for c in read or [] if c not in fit] if read is not None and inputs else [],
            "fit_not_read": [c for c in fit if c not in read] if read is not None else []}


# --- a version's training: what it trained on, what training cost ---------------------------

def _training_cost(gcp, execution: str) -> dict:
    """What the execution the run trained in cost at list price, and how long it ran: a Vertex AI
    custom job (and, for a pipeline step, its whole pipeline run), or a Cloud Run execution."""
    from datetime import datetime

    from gcpbricks.console import prices as PR
    from gcpbricks.console import stats as ST

    def span(a, b):
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() if a and b else None

    if execution.isdigit():
        skus_of, source = PR.catalog(gcp)
        prices = PR.Prices(skus_of, gcp.region)
        zone = f"{gcp.region}-a"

        def job_cost(j):
            secs = span(j.get("startTime"), j.get("endTime"))
            per_hour, machines = 0.0, []
            for pool in j.get("jobSpec", {}).get("workerPoolSpecs", []):
                m = pool.get("machineSpec") or {}
                shape = PR.machine_shape(gcp.machine_type(zone, m["machineType"]), m["machineType"])
                gpu = PR.gpu_name(m.get("acceleratorType"))
                p = prices.vertex_hour("training", shape, gpu, int(m.get("acceleratorCount") or 0))
                n = int(pool.get("replicaCount") or 1)
                per_hour = None if p["usd_per_hour"] is None or per_hour is None else per_hour + p["usd_per_hour"] * n
                machines.append(f"{n} x {m['machineType']}" + (f" + {m.get('acceleratorCount')} x {gpu}" if gpu else ""))
            return {"seconds": secs, "usd": per_hour * secs / 3600 if per_hour is not None and secs else None,
                    "usd_per_hour": per_hour, "machine": ", ".join(machines)}
        j = gcp.ai_get(f"customJobs/{execution}")
        out = {"kind": "vertex", "name": j.get("displayName"), "start": j.get("startTime"), "end": j.get("endTime"),
               "price_source": source, **job_cost(j)}
        run = (j.get("labels") or {}).get("vertex-ai-pipelines-run-billing-id")
        if run:
            steps = [job_cost(s) for s in gcp.ai_list("customJobs", "customJobs",
                                                      filter=f'labels.vertex-ai-pipelines-run-billing-id="{run}"')]
            out["pipeline"] = {"steps": len(steps), "usd": sum(s["usd"] or 0 for s in steps),
                               "seconds": sum(s["seconds"] or 0 for s in steps)}
        return out
    job = execution.rsplit("-", 1)[0]
    e = gcp.run_execution(f"{gcp.parent}/jobs/{job}/executions/{execution}")
    unit = ST.cloud_run(gcp, e["name"], e, "train")
    return {"kind": "cloud_run", "name": job, "start": unit["start"], "end": unit["end"],
            "seconds": span(unit["start"], unit["end"]), "usd": unit["cost_usd"], "price_source": "list price (stats.py)",
            "machine": f"{unit['machine']['cpu']} vCPU, {(unit['machine']['memory_bytes'] or 0) / 2 ** 30:g} GiB"
                       f" x {unit['machine']['tasks']} task(s)"}


def version_training(gcp, model_id: str, version: str) -> dict:
    """The version's training_summary.json (mlflow.training_summary(): its data, feature
    statistics, label, importances) and what its training cost; None parts when not logged."""
    import json

    from gcpbricks.mlflow import training as TR

    v = gcp.ai_get(f"models/{model_id}@{version}")
    uri = v.get("artifactUri", "")
    raw = _read_json(gcp, uri, TR.FILE) if uri.startswith("gs://") else None
    summary = json.loads(raw) if raw else None
    cost, cost_error = None, None
    if summary and summary.get("execution"):
        try:
            cost = _training_cost(gcp, summary["execution"])
        except Exception as e:  # noqa: BLE001 - the job may be gone: the summary still shows
            cost_error = f"{type(e).__name__}: {e}"
    return {"artifact_uri": uri, "summary": summary, "cost": cost, "cost_error": cost_error}
