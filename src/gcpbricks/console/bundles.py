"""The console's Bundles: what each deploy of a bundle to a target put where, read from the
deployment.json that `mlops bundle deploy` writes beside the job definitions
(bundle.deployment), with each resource's live state where the console reads it anyway."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from gcpbricks.console import service as S


def _jobs(gcp) -> list[dict]:
    return [j for j in map(S._job_of, gcp.workflows()) if j]


def folders(gcp) -> dict[tuple[str, str], tuple[str, str]]:
    """(bundle, target) -> (bucket, "<target prefix>.bundle/<bundle>/"), read off its jobs'
    definition paths: where deploy writes its deployment record and dashboards."""
    out = {}
    for j in _jobs(gcp):
        if j["bucket"] and "/jobs/" in f"/{j['definition']}":
            out[(j["bundle"], j["target"])] = (j["bucket"], j["definition"].rsplit("jobs/", 1)[0])
    return out


def deployments(gcp) -> list[dict]:
    """Every deployed bundle's deployment record (none before 0.5.2), read directly."""
    where = list(folders(gcp).values())
    with ThreadPoolExecutor(8) as pool:
        found = pool.map(lambda w: gcp.read_json_or_none(w[0], f"{w[1]}deployment.json"), where)
    return [d for d in found if d]


def deployment(gcp, bundle: str, target: str) -> dict | None:
    where = folders(gcp).get((bundle, target))
    return gcp.read_json_or_none(where[0], f"{where[1]}deployment.json") if where else None


def list_bundles(gcp) -> list[dict]:
    """One row per bundle and target: its deployment record when it has one (deployed on
    0.5.2 or later), else what its jobs alone say."""
    rows = {(d["bundle"], d["target"]): {"bundle": d["bundle"], "target": d["target"], "mode": d.get("mode"),
                                         "version": d["version"], "mlops_version": d["mlops_version"],
                                         "deployed_at": d["deployed_at"], "deployed_by": d.get("deployed_by", ""),
                                         "jobs": len(d["resources"]["jobs"]), "recorded": True}
            for d in deployments(gcp)}
    for j in _jobs(gcp):
        key = (j["bundle"], j["target"])
        if key not in rows:
            rows[key] = {"bundle": j["bundle"], "target": j["target"], "mode": None, "version": None,
                         "mlops_version": None, "deployed_at": j["updated"], "deployed_by": "", "jobs": 0,
                         "recorded": False}
        if not rows[key]["recorded"]:
            rows[key]["jobs"] += 1
    return sorted(rows.values(), key=lambda r: (r["bundle"], r["target"]))


def bundle(gcp, name: str) -> dict:
    """A bundle's targets, each with its deployment record (None before 0.5.2) and its jobs
    as deployed, with their schedule and last run."""
    jobs = [j for j in S.list_jobs(gcp, None, 1) if j["bundle"] == name]
    records = {d["target"]: d for d in deployments(gcp) if d["bundle"] == name}
    targets = sorted({j["target"] for j in jobs} | set(records))
    if not targets:
        raise KeyError(f"no bundle {name!r} is deployed")
    return {"bundle": name, "targets": [
        {"target": t, "deployment": records.get(t),
         "jobs": [{k: j.get(k) for k in ("workflow", "job", "description", "schedule", "trigger", "runs")}
                  for j in jobs if j["target"] == t]}
        for t in targets]}
