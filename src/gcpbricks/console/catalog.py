"""The console's Catalog over BigQuery, as Databricks' Catalog Explorer shows Unity Catalog:
datasets (schemas), their tables, a table's columns, details, sample data and lineage.

Sample data is BigQuery's free preview (tabledata.list), which runs no query and bills nothing.
Views and external tables have no preview, so query_preview runs `SELECT * ... LIMIT` under a
maximumBytesBilled cap, only when the user asks for it.

Lineage joins two sources. BigQuery's Data Lineage API records what its jobs read and wrote,
table to table. Training and scoring read through the Storage API from Python, which it never
sees, so a model's edges come from the model itself: each version's train_window.json (written
by its training) names its feature and label tables, its predict job and its predictions table.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import requests

from gcpbricks.console import ml as M

#: query_preview's cap: a query that would bill more fails before it runs.
MAX_QUERY_BYTES = 100 * 1024 ** 2
PREVIEW_ROWS = 50
#: Tables only, as tabledata.list serves them; a view or an external table needs a query.
PREVIEWABLE = {"TABLE", "SNAPSHOT", "CLONE"}
NAME = re.compile(r"^[\w-]+$")
MODEL_FLOWS_TTL = 600


def _check(*names: str) -> None:
    for n in names:
        if not NAME.match(n):
            raise ValueError(f"not a BigQuery name: {n!r}")


def _missing(e: requests.HTTPError) -> bool:
    return e.response is not None and e.response.status_code == 404


def _denied(e: requests.HTTPError) -> bool:
    return e.response is not None and e.response.status_code in (403, 404)


def _bq_error(e: requests.HTTPError) -> str:
    try:
        return e.response.json()["error"]["message"]
    except Exception:  # noqa: BLE001 - any unreadable body falls back to the status line
        return str(e)


def _ms(v) -> str | None:
    return datetime.fromtimestamp(int(v) / 1000, UTC).isoformat() if v else None


# --- datasets and tables -------------------------------------------------------------------

def datasets(gcp) -> list[dict]:
    """Every dataset of the project, without BigQuery's hidden ones (a leading _)."""
    out = [{"id": d["datasetReference"]["datasetId"], "location": d.get("location", ""),
            "labels": d.get("labels") or {}}
           for d in gcp.bq_list("datasets", "datasets", maxResults=1000)]
    return sorted((d for d in out if not d["id"].startswith("_")), key=lambda d: d["id"])


def dataset(gcp, dataset_id: str) -> dict:
    _check(dataset_id)
    try:
        d = gcp.bq_get(f"datasets/{dataset_id}")
    except requests.HTTPError as e:
        if _missing(e):
            raise KeyError(f"no dataset {dataset_id}") from None
        raise
    tables = [{"id": t["tableReference"]["tableId"], "type": t.get("type", ""),
               "created": _ms(t.get("creationTime")), "labels": t.get("labels") or {},
               "partitioning": (t.get("timePartitioning") or {}).get("field") or (t.get("timePartitioning") or {}).get("type"),
               "clustering": (t.get("clustering") or {}).get("fields", [])}
              for t in gcp.bq_list(f"datasets/{dataset_id}/tables", "tables", maxResults=1000)]
    return {"id": dataset_id, "location": d.get("location", ""), "description": d.get("description", ""),
            "labels": d.get("labels") or {}, "created": _ms(d.get("creationTime")),
            "updated": _ms(d.get("lastModifiedTime")),
            "default_table_expiration_ms": d.get("defaultTableExpirationMs"),
            "tables": sorted(tables, key=lambda t: t["id"])}


def _field(f: dict) -> dict:
    return {"name": f["name"], "type": f["type"], "mode": f.get("mode", "NULLABLE"),
            "description": f.get("description", ""), "fields": [_field(x) for x in f.get("fields", [])]}


def _table_raw(gcp, dataset_id: str, table_id: str, project: str | None = None) -> dict:
    """A table's metadata; `project` reads another project's table, as the console's account
    is allowed to."""
    _check(dataset_id, table_id, *([project] if project else []))
    try:
        return gcp.bq_get(f"datasets/{dataset_id}/tables/{table_id}", project=project)
    except requests.HTTPError as e:
        if _missing(e):
            raise KeyError(f"no table {project or gcp.project}.{dataset_id}.{table_id}") from None
        if _denied(e):
            raise ValueError(f"the console can't read {project or gcp.project}.{dataset_id}.{table_id}") from None
        raise


def table(gcp, dataset_id: str, table_id: str, project: str | None = None) -> dict:
    t = _table_raw(gcp, dataset_id, table_id, project)
    project = project or gcp.project
    tp, rp = t.get("timePartitioning") or {}, t.get("rangePartitioning") or {}
    ext = t.get("externalDataConfiguration") or {}
    return {"id": table_id, "dataset": dataset_id, "project": project, "full_name": f"{project}.{dataset_id}.{table_id}",
            "type": t.get("type", ""), "location": t.get("location", ""), "description": t.get("description", ""),
            "labels": t.get("labels") or {}, "created": _ms(t.get("creationTime")),
            "updated": _ms(t.get("lastModifiedTime")), "expires": _ms(t.get("expirationTime")),
            "rows": int(t["numRows"]) if t.get("numRows") else None,
            "bytes": int(t["numBytes"]) if t.get("numBytes") else None,
            "partitioning": ({"type": tp.get("type", "DAY"), "field": tp.get("field") or "_PARTITIONTIME"} if tp
                             else {"type": "RANGE", "field": rp.get("field")} if rp else None),
            "require_partition_filter": bool(t.get("requirePartitionFilter") or tp.get("requirePartitionFilter")),
            "clustering": (t.get("clustering") or {}).get("fields", []),
            "view_query": (t.get("view") or t.get("materializedView") or {}).get("query"),
            "external": {"format": ext.get("sourceFormat"), "uris": ext.get("sourceUris", [])} if ext else None,
            "previewable": t.get("type") in PREVIEWABLE,
            "schema": [_field(f) for f in (t.get("schema") or {}).get("fields", [])]}


# --- sample data ---------------------------------------------------------------------------

def _value(field: dict, v):
    if v is None:
        return None
    if field.get("mode") == "REPEATED":
        return [_value({**field, "mode": "NULLABLE"}, x["v"]) for x in v]
    if field["type"] in ("RECORD", "STRUCT"):
        return {f["name"]: _value(f, c["v"]) for f, c in zip(field["fields"], v["f"])}
    if field["type"] == "TIMESTAMP":
        return datetime.fromtimestamp(int(v) / 1e6, UTC).isoformat()
    return v


def _rows(schema: list[dict], rows: list[dict]) -> list[list]:
    return [[_value(f, c["v"]) for f, c in zip(schema, r["f"])] for r in rows]


def preview(gcp, dataset_id: str, table_id: str, project: str | None = None, n: int = PREVIEW_ROWS) -> dict:
    """The table's first rows through tabledata.list: no query, nothing billed."""
    t = table(gcp, dataset_id, table_id, project)
    if not t["previewable"]:
        raise ValueError(f"a {t['type'].lower().replace('_', ' ')} has no free preview: run a query")
    page = gcp.bq_get(f"datasets/{dataset_id}/tables/{table_id}/data", project=project, maxResults=n,
                      **{"formatOptions.useInt64Timestamp": "true"})
    return {"columns": [f["name"] for f in t["schema"]], "rows": _rows(t["schema"], page.get("rows", [])),
            "total_rows": int(page.get("totalRows", 0)), "billed_bytes": 0, "source": "preview"}


def query_preview(gcp, user: str, dataset_id: str, table_id: str, project: str | None = None,
                  n: int = PREVIEW_ROWS) -> dict:
    """`SELECT * LIMIT n` for what has no free preview, refused by BigQuery if it would bill
    over MAX_QUERY_BYTES. The query runs, and bills, in the console's project."""
    t = table(gcp, dataset_id, table_id, project)
    try:
        r = gcp.bq_query({"query": f"SELECT * FROM `{t['full_name']}` LIMIT {int(n)}", "useLegacySql": False,
                          "maximumBytesBilled": str(MAX_QUERY_BYTES), "location": t["location"],
                          "timeoutMs": 20000, "formatOptions": {"useInt64Timestamp": True}})
    except requests.HTTPError as e:
        raise ValueError(_bq_error(e)) from None
    except TimeoutError as e:
        raise ValueError(str(e)) from None
    schema = [_field(f) for f in r["schema"]["fields"]]
    billed = int(r.get("totalBytesBilled") or 0)
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "query_preview",
               "table": t["full_name"], "bytes_billed": billed})
    return {"columns": [f["name"] for f in schema], "rows": _rows(schema, r.get("rows", [])),
            "total_rows": int(r.get("totalRows", 0)), "billed_bytes": billed, "source": "query"}


# --- lineage -------------------------------------------------------------------------------
#
# A node id is "<kind>:<name>": table:<project.dataset.table>, path:<gs://...>, or a model
# version model:<model id>@<version>.

def _fqn(gcp, name: str) -> str:
    """"dataset.table" or "project.dataset.table" -> "project.dataset.table"."""
    return name if name.count(".") == 2 else f"{gcp.project}.{name}"


def _node(gcp, node_id: str, **extra) -> dict:
    kind, _, name = node_id.partition(":")
    node = {"id": node_id, "kind": kind, "name": name, **extra}
    if kind == "table":
        project, ds, tb = name.split(".")
        node |= {"project": project, "dataset": ds, "table": tb, "label": f"{ds}.{tb}" if project == gcp.project else name}
    elif kind == "model":
        node.setdefault("label", name)
    else:
        node["label"] = name
    return node


def _flow(gcp, manifest: dict) -> dict:
    """What a model version reads and where its scoring lands, from its train manifest."""
    fs, st, sv = manifest.get("feature_store") or {}, manifest.get("storage") or {}, manifest.get("serving") or {}
    return {"features": _fqn(gcp, fs["feature_table"]) if fs.get("feature_table") else None,
            "labels": _fqn(gcp, fs["label_table"]) if fs.get("label_table") else None,
            "predict_job": sv.get("predict_job"),
            "predictions": st.get("predictions_bq_table")}


def _train_window(gcp, artifact_uri: str) -> dict | None:
    if not artifact_uri.startswith("gs://"):
        return None
    bucket, _, prefix = artifact_uri.removeprefix("gs://").partition("/")
    try:
        return gcp.read_json(bucket, f"{prefix.rstrip('/')}/train_window.json")
    except requests.HTTPError as e:
        if _missing(e):
            return None
        raise


_flows_cache: dict = {"at": 0.0, "flows": []}


def model_flows(gcp) -> list[dict]:
    """Every model's serving version (@champion, else its latest) with its flow, cached:
    reading them is a listVersions and a GCS read per model."""
    if time.time() - _flows_cache["at"] < MODEL_FLOWS_TTL:
        return _flows_cache["flows"]

    def one(m: dict) -> dict | None:
        vs = M._versions(gcp, m["name"])
        v = next((x for x in vs if "champion" in x["aliases"]), vs[0] if vs else None)
        tw = _train_window(gcp, v["artifact_uri"]) if v else None
        if not tw or "manifest" not in tw:
            return None
        return {"model": M._id(m["name"]), "name": m.get("displayName"), "version": v["version"],
                "aliases": v["aliases"], **_flow(gcp, tw["manifest"])}
    with ThreadPoolExecutor(8) as pool:
        flows = [f for f in pool.map(one, gcp.ai_list("models", "models", pageSize=100)) if f]
    _flows_cache.update(at=time.time(), flows=flows)
    return flows


def _model_node(gcp, f: dict) -> dict:
    return _node(gcp, f"model:{f['model']}@{f['version']}", label=f"{f['name']} v{f['version']}",
                 model_id=f["model"], model_name=f["name"], version=f["version"], aliases=f["aliases"])


def _via(f: dict) -> dict | None:
    """The process on a model -> predictions edge, drawn on the edge as Databricks draws a job."""
    return {"kind": "job", "name": f["predict_job"]} if f.get("predict_job") else None


def _describe(gcp, node: dict) -> dict | None:
    """A table node with its type and columns for its card; None once one of ours is dropped.
    Another project's table the console can't read stays, marked unreadable."""
    if node["kind"] != "table":
        return node
    ours = node["project"] == gcp.project
    try:
        t = gcp.bq_get(f"datasets/{node['dataset']}/tables/{node['table']}", project=None if ours else node["project"])
    except requests.HTTPError as e:
        if ours and _missing(e):
            return None
        if not ours and _denied(e):
            return node | {"readable": False}
        raise
    return node | {"type": t.get("type", ""), "columns": [
        {"name": f["name"], "type": f["type"]} for f in (t.get("schema") or {}).get("fields", [])]}


def _bq_links(gcp, fqn: str, location: str) -> tuple[list[dict], list[dict]]:
    """Upstream and downstream nodes BigQuery recorded, without hidden datasets' tables
    (script temporaries)."""
    key = f"bigquery:{fqn}"
    with ThreadPoolExecutor(2) as pool:
        ups, downs = pool.map(lambda end: gcp.lineage_links(location, end, key), ("target", "source"))

    def nodes(links: list[dict], other: str) -> list[dict]:
        seen: dict[str, str] = {}
        for link in links:
            name = link[other]["fullyQualifiedName"]
            seen[name] = max(seen.get(name, ""), link.get("endTime", ""))
        out = []
        for name, last in seen.items():
            system, _, rest = name.partition(":")
            if system == "bigquery":
                if not rest.split(".")[1].startswith("_"):
                    out.append(_node(gcp, f"table:{rest}", last_seen=last))
            else:
                out.append(_node(gcp, f"path:{name.replace('gcs:', 'gs://', 1)}", last_seen=last))
        return out
    return nodes(ups, "source"), nodes(downs, "target")


def lineage(gcp, node_id: str) -> dict:
    """One hop up and down from a node, as Databricks' lineage graph expands one level a click.
    A node is a table, a storage path or a model version; a predict job is no node but the
    process on its model -> predictions edge (`via`). Tables since dropped are left out."""
    kind, _, name = node_id.partition(":")
    flows = model_flows(gcp)
    center = _node(gcp, node_id)
    up: list[dict] = []
    down: list[dict] = []
    if kind == "table":
        project, ds, tb = name.split(".")
        try:
            location = _table_raw(gcp, ds, tb, None if project == gcp.project else project).get("location", "US")
        except ValueError:
            location = "US"  # another project's table the console can't read
        up, down = _bq_links(gcp, name, location.lower())
        for f in flows:
            if name in (f["features"], f["labels"]):
                down.append(_model_node(gcp, f) | {"role": "features" if name == f["features"] else "labels"})
            if name == f["predictions"]:
                up.append(_model_node(gcp, f) | {"via": _via(f)})
    elif kind == "model":
        model_id, _, version = name.partition("@")
        f = next((x for x in flows if x["model"] == model_id and x["version"] == version), None)
        if f is None:
            f = version_flow(gcp, model_id, version)
        if f:
            center = _model_node(gcp, f)
            up = [_node(gcp, f"table:{f[r]}", role=r) for r in ("features", "labels") if f.get(r)]
            if f.get("predictions"):
                down = [_node(gcp, f"table:{f['predictions']}", via=_via(f))]
    elif kind != "path":
        raise ValueError(f"no lineage for a {kind!r} node")
    with ThreadPoolExecutor(8) as pool:
        center, *rest = pool.map(lambda n: _describe(gcp, n), [center, *up, *down])
    return {"node": center, "upstream": [n for n in rest[:len(up)] if n],
            "downstream": [n for n in rest[len(up):] if n]}


# --- a model version's flow ----------------------------------------------------------------

def version_flow(gcp, model_id: str, version: str) -> dict | None:
    """A version's training inputs, train window, predict job and predictions table, from its
    train_window.json; None when its training recorded no manifest."""
    m = gcp.ai_get(f"models/{model_id}")
    v = next((x for x in M._versions(gcp, m["name"]) if x["version"] == version), None)
    if v is None:
        raise KeyError(f"{m.get('displayName')} has no version {version}")
    tw = _train_window(gcp, v["artifact_uri"])
    if not tw or "manifest" not in tw:
        return None
    return {"model": model_id, "name": m.get("displayName"), "version": version, "aliases": v["aliases"],
            "train_start": tw.get("train_date_start"), "train_cutoff": tw.get("train_date_cutoff"),
            **_flow(gcp, tw["manifest"])}


def version_lineage(gcp, model_id: str, version: str) -> dict:
    """The training -> serving flow of one version, with its predict job's latest runs."""
    f = version_flow(gcp, model_id, version)
    if f is None:
        return {"flow": None, "runs": []}
    runs = []
    if f["predict_job"]:
        try:
            runs = [{"id": e["name"].rsplit("/", 1)[-1], "start": e.get("startTime") or e.get("createTime"),
                     "end": e.get("completionTime"),
                     "state": ("FAILED" if e.get("failedCount") else "CANCELLED" if e.get("cancelledCount")
                               else "SUCCEEDED" if e.get("completionTime") else "RUNNING")}
                    for e in gcp.run_executions(f["predict_job"], 10)]
        except requests.HTTPError as e:
            if not _missing(e):
                raise
    return {"flow": f, "runs": runs}
