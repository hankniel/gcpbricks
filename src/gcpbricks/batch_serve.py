"""What a batch_serve_task's model job uses: building blocks, each optional.

The task (terraform/modules/bundle/workflows/batch_serve.yaml) only starts each model's job
over its window and waits; it reads nothing back. Scoring returns predictions, and what
happens to them is the job's own code, as a Databricks batch inference notebook calls
score_batch and then writes where it likes:

    from gcpbricks import batch_serve

    w = batch_serve.window()                                  # the run's model, dates and start
    model = batch_serve.load_model("models:/forecast_30d@champion")
    for f, t in batch_serve.task_chunks(w.from_date, w.to_date, chunk_days=7):
        rows = batch_serve.read_window("ml_feature_store.fs_demo_features", f, t,
                                       date_col="snapshot_date", as_of_ms=w.started_ms)
        preds = batch_serve.score_batch(model, rows, prediction_col="predicted_forecast_30d",
                                        keep_columns=["snapshot_date", "campaign_id"])
        # ... any post-processing ...
        batch_serve.write_window(preds, "p.ml_predictions.forecast_30d_predictions", f, t,
                                 date_col="snapshot_date", entity_id_columns=["snapshot_date", "campaign_id"],
                                 prediction_col="predicted_forecast_30d")
        batch_serve.chunk_done(f, t)                          # a repair skips it

A repair of a failed run (`mlops bundle repair`) runs a failed model again with the run's
run_id, start and window, so task_chunks leaves out the chunks chunk_done noted; a model it
is told to run again in full gets a new run_id, and so every chunk.

pandas, pyarrow and google-cloud-bigquery (the runtime image has them) are needed to read and
write BigQuery.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

from gcpbricks import job_labels

#: Env the Workflow sets on each model's job.
ENV = ("SERVE_MODEL", "FROM_DATE", "TO_DATE", "SERVE_RUN_ID", "SERVE_STARTED_MS")
#: Set too, but not needed: gs://.../<run_id>/<model>/, where chunk_done notes a chunk.
PROGRESS_ENV = "SERVE_PROGRESS"
#: Set too, read when present: the run's date, the served model's `output` (JSON), and
#: "true" on a run given its from_date/to_date.
RUN_DATE_ENV, OUTPUT_ENV, BACKFILL_ENV = "SERVE_RUN_DATE", "SERVE_OUTPUT", "SERVE_BACKFILL"
#: How write() puts a batch into its table (a served model's `output.mode`).
MODES = ("replace_window", "append", "insert_new", "merge")


class Window(NamedTuple):
    model: str
    from_date: str
    to_date: str
    run_id: str
    #: When the run started (epoch millis), the same for every model and Cloud Run task:
    #: read_window(..., as_of_ms=started_ms) pins every read of the run to one moment.
    started_ms: int
    #: Where chunk_done notes this model's finished chunks ("": nowhere, nothing skipped).
    progress: str = ""
    #: The day the run started where the task's utc_offset_hours puts it; a repair keeps it.
    run_date: str = ""
    #: The served model's `output` ({mode, keys, stamps, ...}); empty or None: replace_window.
    output: dict | None = None
    #: The run was given its from_date/to_date rather than its days from today.
    backfill: bool = False


def window() -> Window:
    """The run's model, its date window [from_date, to_date] and start, from the env
    the Workflow sets."""
    missing = [k for k in ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"not started by a batch_serve_task: {missing} unset")
    model, from_date, to_date, run_id, started = (os.environ[k] for k in ENV)
    return Window(model, from_date, to_date, run_id, int(started), os.environ.get(PROGRESS_ENV, ""),
                  os.environ.get(RUN_DATE_ENV) or to_date, json.loads(os.environ.get(OUTPUT_ENV) or "{}"),
                  os.environ.get(BACKFILL_ENV, "").lower() == "true")


def chunks(from_date: str, to_date: str, days: int | None) -> list[tuple[str, str]]:
    """[from_date, to_date] in consecutive windows of `days` (None: one window)."""
    lo, hi = date.fromisoformat(from_date), date.fromisoformat(to_date)
    if not days:
        return [(from_date, to_date)]
    out = []
    while lo <= hi:
        end = min(lo + timedelta(days=days - 1), hi)
        out.append((lo.isoformat(), end.isoformat()))
        lo = end + timedelta(days=1)
    return out


def task_chunks(from_date: str, to_date: str, chunk_days: int | None = None) -> list[tuple[str, str]]:
    """This Cloud Run task's chunks of the window: chunks go to tasks by date
    (CLOUD_RUN_TASK_INDEX / _COUNT, a cluster's num_workers), so a task count without
    chunk_days splits the window evenly. One task: every chunk. Leaves out the chunks this
    run's chunk_done already noted (a repair, or a Cloud Run task's retry)."""
    task = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    if not chunk_days and task_count > 1:
        span = (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1
        chunk_days = -(-span // task_count)
    mine = chunks(from_date, to_date, chunk_days)[task::task_count]
    done = _done_chunks()
    if done & set(mine):
        print(f"skipping {len(done & set(mine))} chunk(s) this run already finished")
    return [c for c in mine if c not in done]


def chunk_done(from_date: str, to_date: str) -> None:
    """Notes the chunk [from_date, to_date] finished, so this run's repair skips it. Call it
    after the chunk's writes. Nothing outside a batch_serve_task."""
    where = _progress()
    if where:
        where[0].blob(f"{where[1]}{from_date}_{to_date}").upload_from_string(b"")


def _progress():
    """(bucket, prefix) of SERVE_PROGRESS, or None."""
    url = os.environ.get(PROGRESS_ENV, "")
    if not url:
        return None
    from google.cloud import storage

    bucket, _, prefix = url.removeprefix("gs://").partition("/")
    return storage.Client().bucket(bucket), prefix


def _done_chunks() -> set[tuple[str, str]]:
    where = _progress()
    if not where:
        return set()
    names = (b.name.removeprefix(where[1]) for b in where[0].list_blobs(prefix=where[1]))
    return {tuple(n.split("_", 1)) for n in names if "_" in n}


# --- scoring ------------------------------------------------------------------------------------

class LoadedModel(NamedTuple):
    model: Any  # an mlflow pyfunc model
    name: str
    version: str
    model_id: str  # the version's model_id tag, else <name>-v<version>


def load_model(model_uri: str) -> LoadedModel:
    """models:/<name>@<alias> or models:/<name>/<version>, resolved once: a promotion while a
    run scores can't mix versions across its chunks."""
    from gcpbricks import mlflow

    m = re.fullmatch(r"models:/([^/@]+)(?:/(\w+)|@([\w-]+))", model_uri)
    if not m:
        raise ValueError(f"model_uri {model_uri!r}: give models:/<name>@<alias> or models:/<name>/<version>")
    client = mlflow.MlflowClient()
    mv = (client.get_model_version_by_alias(m.group(1), m.group(3)) if m.group(3)
          else client.get_model_version(m.group(1), m.group(2)))
    loaded = mlflow.pyfunc.load_model(f"models:/{mv.name}/{mv.version}")
    return LoadedModel(loaded, mv.name, str(mv.version), mv.tags.get("model_id") or f"{mv.name}-v{mv.version}")


def score_batch(model: LoadedModel | str, df, *, prediction_col: str = "prediction",
                keep_columns: list[str] | None = None):
    """df's rows scored by the model's predict(): keep_columns (default: all of df's) plus
    prediction_col, model_id and predicted_at. Returns a new DataFrame; writes nothing."""
    import pandas as pd

    if isinstance(model, str):
        model = load_model(model)
    out = (df[list(keep_columns)] if keep_columns is not None else df).copy()
    if not len(df):
        return out.assign(**{prediction_col: pd.Series(dtype=float), "model_id": pd.Series(dtype=str)})
    out[prediction_col] = list(model.model.predict(df))
    out["model_id"] = model.model_id
    out["predicted_at"] = pd.Timestamp(datetime.now(UTC))
    return out


def read_window(table: str, from_date: str, to_date: str, *, date_col: str,
                columns: list[str] | None = None, project: str | None = None, as_of_ms: int | None = None):
    """A BigQuery table's rows with date_col in [from_date, to_date] as a DataFrame; as_of_ms
    pins the read to that time (time travel). Needs google-cloud-bigquery and db-dtypes."""
    from google.cloud import bigquery

    client = job_labels.bigquery_client(project=project)
    as_of = f" FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS({int(as_of_ms)})" if as_of_ms else ""
    cols = ", ".join(f"`{c}`" for c in columns) if columns else "*"
    sql = (f"SELECT {cols} FROM `{table}`{as_of} "
           f"WHERE `{date_col}` BETWEEN @from_date AND @to_date")
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("from_date", "DATE", from_date),
        bigquery.ScalarQueryParameter("to_date", "DATE", to_date)]))
    return job.to_dataframe()


def scored_keys(table: str, keys: list[str], from_date: str, to_date: str, *,
                date_col: str, project: str | None = None):
    """The `keys` of table's rows with date_col in the window, as a DataFrame; none while the
    table doesn't exist. An insert_new job leaves these out before it scores."""
    import pandas as pd
    from google.api_core.exceptions import NotFound

    try:
        return read_window(table, from_date, to_date, date_col=date_col, columns=keys, project=project)
    except NotFound:
        return pd.DataFrame(columns=keys)


def watermarks(table: str, by: list[str], *, date_col: str,
               since: tuple[str, str] | None = None, as_of_ms: int | None = None,
               project: str | None = None):
    """Each `by` group's latest date_col in `table`, as a DataFrame [*by, date_col]; none while
    the table doesn't exist, or didn't at as_of_ms. since=(column, date) reads only the rows
    with column >= date, e.g. a table's recent partitions."""
    import pandas as pd
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    client = job_labels.bigquery_client(project=project)
    try:
        created = client.get_table(table).created
    except NotFound:
        return pd.DataFrame(columns=[*by, date_col])
    if as_of_ms and created > datetime.fromtimestamp(as_of_ms / 1000, UTC):
        return pd.DataFrame(columns=[*by, date_col])
    as_of = f" FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS({int(as_of_ms)})" if as_of_ms else ""
    group = ", ".join(f"`{c}`" for c in by)
    where = f" WHERE `{since[0]}` >= @since" if since else ""
    sql = (f"SELECT {group + ', ' if by else ''}MAX(`{date_col}`) AS `{date_col}` FROM `{table}`{as_of}"
           f"{where}{' GROUP BY ' + group if by else ''}")
    params = [bigquery.ScalarQueryParameter("since", "DATE", since[1])] if since else []
    df = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).to_dataframe()
    return df.dropna(subset=[date_col])


# --- writing ------------------------------------------------------------------------------------

def check_predictions(df, *, prediction_col: str, entity_id_columns: list[str], from_date: str,
                      to_date: str, date_col: str, keys: list[str] | None = None) -> None:
    """Raises on a batch that would corrupt a window replace: columns missing, rows without a
    model_id or outside the window, infinite predictions, or a repeated (entity, model_id) --
    or a repeated `keys`, the key an insert_new or merge writes by."""
    import numpy as np
    import pandas as pd

    missing = [c for c in (date_col, *entity_id_columns, prediction_col, "model_id") if c not in df.columns]
    if missing:
        raise ValueError(f"predictions lack columns {sorted(set(missing))}")
    if df["model_id"].isna().any() or (df["model_id"].astype(str) == "").any():
        raise ValueError("predictions have rows without a model_id")
    days = pd.to_datetime(df[date_col]).dt.date
    outside = int((~days.between(date.fromisoformat(from_date), date.fromisoformat(to_date))).sum())
    if outside:
        raise ValueError(f"{outside} predictions fall outside the window {from_date}..{to_date}")
    pred = pd.to_numeric(df[prediction_col], errors="coerce").to_numpy(dtype=float)
    infinite = int(np.isinf(pred).sum())
    if infinite:
        raise ValueError(f"{infinite} predictions in {prediction_col} are infinite")
    keys = list(dict.fromkeys(keys or [*entity_id_columns, "model_id"]))
    dupes = int(df.duplicated(keys).sum())
    if dupes:
        raise ValueError(f"{dupes} predictions repeat an ({', '.join(keys)}) key")


def _create_sql(target: str, staging: str, date_col: str, partition_by: str | None,
                cluster_by: list[str] | None) -> str:
    """The target created like the staged batch: partitioned by partition_by (default
    date_col), clustered by cluster_by (default model_id; [] for none)."""
    cluster = ", ".join(f"`{c}`" for c in (["model_id"] if cluster_by is None else cluster_by))
    lines = [f"CREATE TABLE IF NOT EXISTS `{target}`", f"PARTITION BY `{partition_by or date_col}`",
             *([f"CLUSTER BY {cluster}"] if cluster else []), f"AS SELECT * FROM `{staging}` WHERE FALSE;"]
    return "\n".join(lines)


def replace_window_sql(*, target: str, staging: str, date_col: str, entity_id_columns: list[str],
                       prediction_col: str, partition_by: str | None = None,
                       cluster_by: list[str] | None = None) -> str:
    """The BigQuery script write_window runs, given @from_date @to_date @min_keep_ratio. Key:
    (date_col in the window, model_id): a rerun replaces its own versions' rows, other
    versions' rows stay as history. Every check runs before anything is deleted, and the
    delete + insert commit together. New columns are added to the target, never dropped."""
    dataset, target_table = target.rsplit(".", 1)
    staging_table = staging.rsplit(".", 1)[1]
    entity = ", ".join(f"`{c}`" for c in entity_id_columns)
    return f"""
DECLARE staged_versions ARRAY<STRING>;
DECLARE staged_rows INT64;
DECLARE replaced_rows INT64;
DECLARE deleted_rows INT64;
DECLARE inserted_rows INT64;
DECLARE cols STRING;

{_create_sql(target, staging, date_col, partition_by, cluster_by)}

SET staged_rows = (SELECT COUNT(*) FROM `{staging}`);
SET staged_versions = (SELECT ARRAY_AGG(DISTINCT model_id) FROM `{staging}`);
SET replaced_rows = (
  SELECT COUNT(*) FROM `{target}`
  WHERE `{date_col}` BETWEEN @from_date AND @to_date AND model_id IN UNNEST(staged_versions)
);

ASSERT (SELECT COUNTIF(`{date_col}` NOT BETWEEN @from_date AND @to_date) FROM `{staging}`) = 0
  AS 'predictions fall outside the window';
ASSERT (SELECT COUNTIF(IS_INF(`{prediction_col}`)) FROM `{staging}`) = 0
  AS 'infinite predictions';
ASSERT (
  SELECT COUNT(*) FROM (SELECT 1 FROM `{staging}` GROUP BY {entity}, model_id HAVING COUNT(*) > 1)
) = 0 AS 'predictions repeat an (entity, model_id) key';
ASSERT replaced_rows = 0 OR staged_rows >= @min_keep_ratio * replaced_rows
  AS 'predictions are much fewer than the rows they would replace';

FOR c IN (
  SELECT s.column_name, s.data_type
  FROM `{dataset}`.INFORMATION_SCHEMA.COLUMNS AS s
  LEFT JOIN `{dataset}`.INFORMATION_SCHEMA.COLUMNS AS t
    ON t.table_name = '{target_table}' AND t.column_name = s.column_name
  WHERE s.table_name = '{staging_table}' AND t.column_name IS NULL
)
DO
  EXECUTE IMMEDIATE FORMAT('ALTER TABLE `{target}` ADD COLUMN `%s` %s', c.column_name, c.data_type);
END FOR;

SET cols = (
  SELECT STRING_AGG(FORMAT('`%s`', column_name), ', ' ORDER BY ordinal_position)
  FROM `{dataset}`.INFORMATION_SCHEMA.COLUMNS
  WHERE table_name = '{staging_table}'
);

BEGIN TRANSACTION;
  DELETE FROM `{target}`
  WHERE `{date_col}` BETWEEN @from_date AND @to_date AND model_id IN UNNEST(staged_versions);
  SET deleted_rows = @@row_count;
  EXECUTE IMMEDIATE FORMAT('INSERT INTO `{target}` (%s) SELECT %s FROM `{staging}`', cols, cols);
  SET inserted_rows = @@row_count;
COMMIT TRANSACTION;

DROP TABLE `{staging}`;
SELECT staged_rows AS rows_staged, replaced_rows AS rows_replaced, deleted_rows AS rows_deleted,
       inserted_rows AS rows_inserted;
"""


def insert_sql(*, target: str, staging: str, date_col: str, mode: str, keys: list[str],
               columns: list[str], prediction_col: str, partition_by: str | None = None,
               cluster_by: list[str] | None = None) -> str:
    """The BigQuery script write() runs for append / insert_new / merge, given @from_date
    @to_date: the staged rows' checks, the target's new columns added, then one INSERT or
    MERGE (a single statement, so atomic). insert_new and merge match on `keys`, and read only
    the window's partitions of the target when date_col is one of them."""
    dataset, target_table = target.rsplit(".", 1)
    staging_table = staging.rsplit(".", 1)[1]
    cols = ", ".join(f"`{c}`" for c in columns)
    checks = ""
    if mode != "append":
        key = ", ".join(f"`{k}`" for k in keys)
        checks = f"""
ASSERT (
  SELECT COUNT(*) FROM (SELECT 1 FROM `{staging}` GROUP BY {key} HAVING COUNT(*) > 1)
) = 0 AS 'predictions repeat a ({", ".join(keys)}) key';"""
    if mode == "append":
        write = f"INSERT INTO `{target}` ({cols}) SELECT {cols} FROM `{staging}`"
    else:
        on = [f"t.`{k}` = s.`{k}`" for k in keys]
        if date_col in keys:
            on.append(f"t.`{date_col}` BETWEEN @from_date AND @to_date")
        write = (f"MERGE `{target}` AS t USING `{staging}` AS s ON {' AND '.join(on)}\n"
                 f"WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({', '.join(f's.`{c}`' for c in columns)})")
        if mode == "merge":
            sets = ", ".join(f"`{c}` = s.`{c}`" for c in columns if c not in keys)
            write += f"\nWHEN MATCHED THEN UPDATE SET {sets}"
    return f"""
DECLARE staged_rows INT64;
DECLARE written_rows INT64;

{_create_sql(target, staging, date_col, partition_by, cluster_by)}

SET staged_rows = (SELECT COUNT(*) FROM `{staging}`);

ASSERT (SELECT COUNTIF(`{date_col}` NOT BETWEEN @from_date AND @to_date) FROM `{staging}`) = 0
  AS 'predictions fall outside the window';
ASSERT (SELECT COUNTIF(IS_INF(`{prediction_col}`)) FROM `{staging}`) = 0
  AS 'infinite predictions';{checks}

FOR c IN (
  SELECT s.column_name, s.data_type
  FROM `{dataset}`.INFORMATION_SCHEMA.COLUMNS AS s
  LEFT JOIN `{dataset}`.INFORMATION_SCHEMA.COLUMNS AS t
    ON t.table_name = '{target_table}' AND t.column_name = s.column_name
  WHERE s.table_name = '{staging_table}' AND t.column_name IS NULL
)
DO
  EXECUTE IMMEDIATE FORMAT('ALTER TABLE `{target}` ADD COLUMN `%s` %s', c.column_name, c.data_type);
END FOR;

{write};
SET written_rows = @@row_count;

DROP TABLE `{staging}`;
SELECT staged_rows AS rows_staged, written_rows AS rows_written;
"""


def write_window(df, table: str, from_date: str, to_date: str, *, entity_id_columns: list[str],
                 prediction_col: str, min_keep_ratio: float = 0.5, date_col: str,
                 partition_by: str | None = None, cluster_by: list[str] | None = None,
                 project: str | None = None, log=print) -> dict:
    """Replaces the predictions in BigQuery `table` ("project.dataset.table") for
    [from_date, to_date] with df's, by (date_col in the window, model_id), in one transaction,
    creating the table on the first write. Refuses a batch the checks reject or one with
    fewer rows than min_keep_ratio x the rows it replaces. An empty df changes nothing.
    Reruns replace, never duplicate. Returns the row counts."""
    if not len(df):
        log(f"{table} {from_date}..{to_date}: no predictions, nothing replaced")
        return {"rows_staged": 0, "rows_replaced": 0, "rows_deleted": 0, "rows_inserted": 0}
    check_predictions(df, prediction_col=prediction_col, entity_id_columns=entity_id_columns,
                      from_date=from_date, to_date=to_date, date_col=date_col)
    counts = _run_staged(
        df, table, date_col, project,
        lambda staging: replace_window_sql(target=table, staging=staging, date_col=date_col,
                                           entity_id_columns=entity_id_columns, prediction_col=prediction_col,
                                           partition_by=partition_by, cluster_by=cluster_by),
        {"from_date": ("DATE", from_date), "to_date": ("DATE", to_date),
         "min_keep_ratio": ("FLOAT64", min_keep_ratio)})
    log(f"{table} {from_date}..{to_date}: replaced {counts['rows_deleted']} rows with {counts['rows_inserted']}")
    return counts


def write(df, table: str, from_date: str, to_date: str, *, entity_id_columns: list[str],
          prediction_col: str, mode: str = "replace_window", keys: list[str] | None = None,
          stamps: dict | None = None, min_keep_ratio: float = 0.5, date_col: str,
          partition_by: str | None = None, cluster_by: list[str] | None = None,
          project: str | None = None, log=print) -> dict:
    """df's predictions for [from_date, to_date] into BigQuery `table` as `mode` says, as a
    Databricks batch inference job writes its Delta table:

      replace_window  write_window: the window's rows of df's versions replaced
      append          every row inserted
      insert_new      a row inserted only where `table` has none with its `keys`: a row,
                      once written, never changes, and a rerun inserts nothing twice
      merge           a row with its `keys` updated, the rest inserted

    `stamps` ({column: value}, e.g. the run's date) is set on every row first. The table is
    created on the first write, partitioned by partition_by (default date_col) and clustered
    by cluster_by (default model_id), and gains the columns df brings. Returns the row counts."""
    if mode not in MODES:
        raise ValueError(f"write mode {mode!r}: one of {MODES}")
    df = df.assign(**(stamps or {}))
    if mode == "replace_window":
        return write_window(df, table, from_date, to_date, entity_id_columns=entity_id_columns,
                            prediction_col=prediction_col, min_keep_ratio=min_keep_ratio,
                            date_col=date_col, partition_by=partition_by, cluster_by=cluster_by,
                            project=project, log=log)
    if mode in ("insert_new", "merge") and not keys:
        raise ValueError(f"write mode {mode}: give the keys it writes by")
    if not len(df):
        log(f"{table} {from_date}..{to_date}: no predictions, nothing written")
        return {"rows_staged": 0, "rows_written": 0}
    check_predictions(df, prediction_col=prediction_col, entity_id_columns=entity_id_columns,
                      from_date=from_date, to_date=to_date, date_col=date_col,
                      keys=keys if mode != "append" else None)
    columns = [*df.columns, *([] if "scored_at" in df.columns else ["scored_at"])]
    counts = _run_staged(
        df, table, date_col, project,
        lambda staging: insert_sql(target=table, staging=staging, date_col=date_col, mode=mode,
                                   keys=keys or [], columns=columns, prediction_col=prediction_col,
                                   partition_by=partition_by, cluster_by=cluster_by),
        {"from_date": ("DATE", from_date), "to_date": ("DATE", to_date)})
    log(f"{table} {from_date}..{to_date}: {mode} wrote {counts['rows_written']} of {counts['rows_staged']} rows")
    return counts


def _run_staged(df, table: str, date_col: str, project: str | None, sql_for, params: dict) -> dict:
    """df loaded into a staging table beside `table` (expiring in a day), then the script
    sql_for(staging) run with `params` ({name: (type, value)}); its last SELECT's row."""
    import pandas as pd
    from google.cloud import bigquery

    out = df.copy()
    out[date_col] = pd.to_datetime(out[date_col]).dt.date
    if "predicted_at" in out.columns:
        out["predicted_at"] = pd.to_datetime(out["predicted_at"], utc=True)
    out["scored_at"] = pd.Timestamp(datetime.now(UTC))

    client = job_labels.bigquery_client(project=project)
    staging = f"{table.rsplit('.', 1)[0]}._stg_{table.rsplit('.', 1)[1]}_{uuid.uuid4().hex[:12]}"
    client.load_table_from_dataframe(out, staging).result()
    try:
        # A failed write leaves no staging table for long.
        t = client.get_table(staging)
        t.expires = datetime.now(UTC) + timedelta(days=1)
        client.update_table(t, ["expires"])
        job = client.query(sql_for(staging), job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(name, kind, value) for name, (kind, value) in params.items()]))
        return dict(next(iter(job.result())).items())
    except Exception:
        client.delete_table(staging, not_found_ok=True)
        raise
