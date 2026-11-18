"""The console's Quality tab, Databricks' Catalog Explorer Quality tab and monitor dashboard:
a table's quality monitors, found in each bundle's deployment.json (bundle.quality_monitors),
and their metric tables read for one granularity, model_id and slice.

The metric tables are small (a row per window, model, slice and column), so a read is three
queries of them under MAX_QUERY_BYTES, billed in the console's project."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import requests

from gcpbricks.console import bundles as BN
from gcpbricks.console import service as S
from gcpbricks.console.catalog import _bq_error, _field, _rows
from gcpbricks.monitors import QualityMonitor

MAX_QUERY_BYTES = 200 * 1024 ** 2
#: Windows a read goes back by default, per granularity.
WINDOWS = 30
_WIDTH = {"5 minutes": timedelta(minutes=5), "30 minutes": timedelta(minutes=30), "1 hour": timedelta(hours=1),
          "1 day": timedelta(days=1), "1 week": timedelta(weeks=1), "2 weeks": timedelta(weeks=2),
          "3 weeks": timedelta(weeks=3), "4 weeks": timedelta(weeks=4), "1 month": timedelta(days=31),
          "1 year": timedelta(days=366)}


#: A monitored view's sources, kept ten minutes: view -> (read at, tables).
_SOURCES: dict[str, tuple[float, list[str]]] = {}


def _sources(gcp, view: str) -> list[str]:
    hit = _SOURCES.get(view)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        tables = gcp.bq_referenced_tables(view)
    except requests.HTTPError:
        tables = []  # not built yet, or not readable: it monitors nothing it can be traced to
    _SOURCES[view] = (time.time(), tables)
    return tables


def monitors(gcp, dataset: str, table: str, project: str | None = None) -> list[dict]:
    """The quality monitors that profile the table, or write it as a metric table, on every
    deployed bundle and target; then those that profile a view over it (`via`: that view),
    as a monitor of predictions beside their labels profiles a view joining the two."""
    fqn = f"{project or gcp.project}.{dataset}.{table}"
    out, others = [], []
    for d in BN.deployments(gcp):
        for key, m in (d["resources"].get("quality_monitors") or {}).items():
            row = {"bundle": d["bundle"], "target": d["target"], "key": key, **m}
            if fqn in (m["table_name"], m["profile_metrics_table"], m["drift_metrics_table"], m.get("anomalies_table")):
                out.append(row)
            else:
                others.append(row)
    with ThreadPoolExecutor(8) as pool:
        reads = list(pool.map(lambda m: fqn in _sources(gcp, m["table_name"]), others))
    out += [m | {"via": m["table_name"]} for m, reads_it in zip(others, reads) if reads_it]
    return sorted(out, key=lambda m: ("via" in m, m["table_name"] != fqn, m["bundle"], m["target"], m["key"]))


def _exists(gcp, fqn: str) -> bool:
    project, ds, tb = fqn.split(".")
    try:
        gcp.bq_get(f"datasets/{ds}/tables/{tb}", project=project)
        return True
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return False
        raise


def leftovers(gcp, dataset: str, table: str, project: str | None = None) -> list[dict]:
    """Monitors of the table since deleted (their refresh job retired) whose metric tables are
    still there: deleting a monitor keeps them, as Databricks keeps a deleted monitor's assets."""
    fqn = f"{project or gcp.project}.{dataset}.{table}"
    out = []
    for wf in gcp.workflows():
        j = S._job_of(wf)
        if not j or not j["retired"] or j["name"] != f"Quality monitor: {fqn}":
            continue
        try:
            tasks = (gcp.read_json(j["bucket"], j["definition"]).get("tasks") or {}).values()
        except requests.HTTPError:
            continue
        for t in tasks:
            args = t.get("args") or []
            if "--config" not in args or "--key" not in args:
                continue
            try:
                m = QualityMonitor.model_validate(json.loads(args[args.index("--config") + 1]))
            except ValueError:
                continue
            tables = [x for x in (*m.output_tables(), m.anomalies_table()) if x]
            left = [x for x in tables if _exists(gcp, x)]
            if left:
                out.append({"key": args[args.index("--key") + 1], "bundle": j["bundle"], "target": j["target"],
                            "retired": j["retired"], "tables": left})
    return out


def monitor(gcp, bundle: str, target: str, key: str) -> dict:
    d = BN.deployment(gcp, bundle, target)
    m = ((d or {}).get("resources", {}).get("quality_monitors") or {}).get(key)
    if m is None:
        raise KeyError(f"no quality monitor {key!r} in {bundle} on {target}")
    return {"bundle": bundle, "target": target, "key": key, **m}


def _query(gcp, sql: str, params: dict[str, object]) -> tuple[list[dict], int]:
    qp = []
    for n, v in params.items():
        kind = "TIMESTAMP" if isinstance(v, datetime) else "INT64" if isinstance(v, int) else "STRING"
        qp.append({"name": n, "parameterType": {"type": kind},
                   "parameterValue": {"value": v.isoformat() if isinstance(v, datetime) else str(v)}})
    try:
        r = gcp.bq_query({"query": sql, "useLegacySql": False, "parameterMode": "NAMED", "queryParameters": qp,
                          "maximumBytesBilled": str(MAX_QUERY_BYTES), "timeoutMs": 20000,
                          "formatOptions": {"useInt64Timestamp": True}})
    except requests.HTTPError as e:
        raise ValueError(f"reading the metric tables failed: {_bq_error(e)}") from None
    schema = [_field(f) for f in r["schema"]["fields"]]
    names = [f["name"] for f in schema]
    return [dict(zip(names, row)) for row in _rows(schema, r.get("rows", []))], int(r.get("totalBytesBilled") or 0)


#: '' stands for NULL in a filter: the whole table, all models, a snapshot's granularity.
_GROUP = ("IFNULL(model_id, '') = @model AND IFNULL(slice_key, '') = @slice_key "
          "AND IFNULL(slice_value, '') = @slice_value")
_PROFILE = ("`window`.start AS start, log_type, column_name, data_type, `count`, num_nulls, percent_null, "
            "distinct_count, percent_distinct, percent_zeros, `avg`, `min`, median, `max`, stddev, "
            "ARRAY(SELECT AS STRUCT item, `count` FROM UNNEST(frequent_items) LIMIT 5) AS top_items, "
            "mean_squared_error, root_mean_squared_error, mean_average_error, "
            "mean_absolute_percentage_error, r2_score, accuracy_score, precision.macro AS precision_macro, "
            "recall.macro AS recall_macro, f1_score.macro AS f1_macro, confusion_matrix, log_loss, "
            "roc_auc_score.one_vs_one.weighted AS roc_auc_weighted, roc_auc_score.one_vs_one.macro AS roc_auc_macro, "
            "predictive_parity, predictive_equality, equal_opportunity, statistical_parity")
_DRIFT = ("`window`.start AS start, drift_type, column_name, data_type, count_delta, avg_delta, "
          "percent_null_delta, ks_test.statistic AS ks_statistic, ks_test.pvalue AS ks_pvalue, "
          "chi_squared_test.statistic AS chi_squared_statistic, chi_squared_test.pvalue AS chi_squared_pvalue, "
          "population_stability_index, js_distance, wasserstein_distance, tv_distance, l_infinity_distance, "
          "non_null_columns_delta.added AS columns_added, non_null_columns_delta.missing AS columns_missing")


def metrics(gcp, bundle: str, target: str, key: str, granularity: str = "", model: str = "",
            slice_key: str = "", slice_value: str = "", windows: int = WINDOWS) -> dict:
    """A monitor's metrics for one granularity ('' for a snapshot), model_id and slice ('' for
    all models, the whole table): the profile of each column per window and the baseline's,
    model quality on the `:table` rows, drift per column and window, and the groups there
    are to pick from."""
    m = monitor(gcp, bundle, target, key)
    granularity = granularity or (m["granularities"][0] if m["granularities"] else "")
    if granularity and granularity not in m["granularities"]:
        raise ValueError(f"granularity {granularity!r} is not one of {m['granularities']}")
    if not _exists(gcp, m["profile_metrics_table"]):
        # Set up, never refreshed: a refresh creates the metric tables.
        return {"monitor": m, "granularity": granularity, "model": model, "slice_key": slice_key,
                "slice_value": slice_value, "profile": [], "drift": [], "anomalies": [], "refreshed": False,
                "options": {"granularities": m["granularities"], "models": [], "slices": []}, "billed_bytes": 0}
    windows = max(1, min(int(windows), 366))
    since = datetime.now(UTC) - (_WIDTH[granularity] * (windows + 1) if granularity else timedelta(days=windows))
    params = {"granularity": granularity, "model": model, "slice_key": slice_key, "slice_value": slice_value,
              "since": since}
    custom = "".join(f", {c}" for c in m["custom_metrics"])
    drift_custom = "".join(f", {c}" for c in m.get("drift_custom_metrics") or [])
    profile_sql = (f"SELECT {_PROFILE}{custom} FROM `{m['profile_metrics_table']}`\n"
                   f"WHERE {_GROUP} AND (log_type = 'BASELINE' OR (IFNULL(granularity, '') = @granularity "
                   "AND `window`.start >= @since))\nORDER BY start, column_name")
    drift_sql = (f"SELECT {_DRIFT}{drift_custom} FROM `{m['drift_metrics_table']}`\n"
                 f"WHERE {_GROUP} AND IFNULL(granularity, '') = @granularity AND `window`.start >= @since\n"
                 "ORDER BY start, column_name")
    groups_sql = (f"SELECT DISTINCT IFNULL(granularity, '') AS granularity, IFNULL(model_id, '') AS model, "
                  f"IFNULL(slice_key, '') AS slice_key, IFNULL(slice_value, '') AS slice_value\n"
                  f"FROM `{m['profile_metrics_table']}`\nWHERE log_type = 'INPUT' AND column_name = ':table'")
    queries = [(profile_sql, params), (drift_sql, params), (groups_sql, {})]
    if m.get("anomalies_table") and _exists(gcp, m["anomalies_table"]):
        anomalies_sql = (f"SELECT `window`.start AS start, column_name, metric, value, lower_bound, upper_bound, "
                         f"anomaly_probability, is_anomaly, status FROM `{m['anomalies_table']}`\n"
                         f"WHERE {_GROUP} AND IFNULL(granularity, '') = @granularity AND `window`.start >= @since\n"
                         "ORDER BY start, column_name, metric")
        queries.append((anomalies_sql, params))
    with ThreadPoolExecutor(len(queries)) as pool:
        results = list(pool.map(lambda a: _query(gcp, *a), queries))
    (profile, b1), (drift, b2), (groups, b3) = results[:3]
    anomalies, b4 = results[3] if len(results) > 3 else ([], 0)
    models = sorted({g["model"] for g in groups if g["model"]})
    slices = sorted({(g["slice_key"], g["slice_value"]) for g in groups if g["slice_key"]})
    return {"monitor": m, "granularity": granularity, "model": model, "slice_key": slice_key,
            "slice_value": slice_value, "profile": profile, "drift": drift, "anomalies": anomalies, "refreshed": True,
            "options": {"granularities": m["granularities"], "models": models,
                        "slices": [{"key": k, "value": v} for k, v in slices]},
            "billed_bytes": b1 + b2 + b3 + b4}


def _picked(gcp, bundle: str, target: str, key: str, granularity: str) -> tuple[dict, str]:
    m = monitor(gcp, bundle, target, key)
    granularity = granularity or (m["granularities"][0] if m["granularities"] else "")
    if granularity and granularity not in m["granularities"]:
        raise ValueError(f"granularity {granularity!r} is not one of {m['granularities']}")
    return m, granularity


def _since(granularity: str, windows: int) -> datetime:
    windows = max(1, min(int(windows), 366))
    return datetime.now(UTC) - (_WIDTH[granularity] * (windows + 1) if granularity else timedelta(days=windows))


_QUALITY = ("`count`, mean_squared_error, root_mean_squared_error, mean_average_error, "
            "mean_absolute_percentage_error, r2_score, accuracy_score, precision.macro AS precision_macro, "
            "recall.macro AS recall_macro, f1_score.macro AS f1_macro, log_loss, "
            "roc_auc_score.one_vs_one.weighted AS roc_auc_weighted, roc_auc_score.one_vs_one.macro AS roc_auc_macro")


def slices(gcp, bundle: str, target: str, key: str, granularity: str = "", model: str = "",
           windows: int = WINDOWS) -> dict:
    """Every slice's `:table` row per window (rows and model quality), the whole table's
    among them (slice_key ''): the dashboard's breakdown, worst slices first."""
    m, granularity = _picked(gcp, bundle, target, key, granularity)
    sql = (f"SELECT `window`.start AS start, IFNULL(slice_key, '') AS slice_key, "
           f"IFNULL(slice_value, '') AS slice_value, {_QUALITY} FROM `{m['profile_metrics_table']}`\n"
           "WHERE log_type = 'INPUT' AND column_name = ':table' AND IFNULL(model_id, '') = @model "
           "AND IFNULL(granularity, '') = @granularity AND `window`.start >= @since\n"
           "ORDER BY start, slice_key, slice_value")
    rows, billed = _query(gcp, sql, {"model": model, "granularity": granularity,
                                     "since": _since(granularity, windows)})
    return {"granularity": granularity, "model": model, "rows": rows, "billed_bytes": billed}


def column(gcp, bundle: str, target: str, key: str, column_name: str, granularity: str = "", model: str = "",
           slice_key: str = "", slice_value: str = "", windows: int = WINDOWS) -> dict:
    """One column's distribution per window and the baseline's: its quantiles (a number's)
    and frequent items (a category's), which the dashboard draws against each other."""
    m, granularity = _picked(gcp, bundle, target, key, granularity)
    sql = (f"SELECT `window`.start AS start, log_type, data_type, `count`, quantiles, "
           f"ARRAY(SELECT AS STRUCT item, `count` FROM UNNEST(frequent_items) LIMIT 20) AS frequent_items\n"
           f"FROM `{m['profile_metrics_table']}`\nWHERE column_name = @column AND {_GROUP} AND "
           "(log_type = 'BASELINE' OR (IFNULL(granularity, '') = @granularity AND `window`.start >= @since))\n"
           "ORDER BY start")
    rows, billed = _query(gcp, sql, {"column": column_name, "model": model, "slice_key": slice_key,
                                     "slice_value": slice_value, "granularity": granularity,
                                     "since": _since(granularity, windows)})
    return {"column": column_name, "rows": rows, "billed_bytes": billed}


def monitor_yaml(key: str, spec: dict) -> dict:
    """The Create monitor dialog's monitor as a bundle's `resources.quality_monitors` entry,
    validated as deploy would validate it."""
    import re

    import yaml
    from pydantic import ValidationError

    from gcpbricks.bundle import MONITOR_KEY
    from gcpbricks.monitors import QualityMonitor

    if not re.match(MONITOR_KEY, key):
        raise ValueError(f"monitor key {key!r}: lowercase letters, digits, - and _, starting with a letter")
    try:
        m = QualityMonitor.model_validate(spec)
    except ValidationError as e:
        raise ValueError(str(e)) from None
    body = m.model_dump(exclude_none=True, exclude_defaults=True)
    for kind in ("inference_log", "time_series", "snapshot"):
        if getattr(m, kind) is not None:
            body.setdefault(kind, {})
    text = yaml.safe_dump({"resources": {"quality_monitors": {key: body}}}, sort_keys=False, width=110)
    return {"key": key, "yaml": text}
