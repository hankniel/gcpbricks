"""A model's batch_serve job's building blocks: window() from the Workflow's env, a task's
chunks, score_batch over a registered model (fake Vertex) returning predictions, and
write_window's checks and SQL (no BigQuery)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from test_mlflow import FakeVertex  # pytest puts tests/ on sys.path (no __init__.py)

from gcpbricks import batch_serve as C
from gcpbricks import mlflow

KEYS = ["install_date", "campaign"]


def _rows(f, t):
    days = pd.date_range(f, t).date
    return pd.DataFrame({"install_date": days, "campaign": "c1", "x": range(len(days))})


def _preds(f="2026-09-01", t="2026-09-03"):
    df = _rows(f, t)
    return df.assign(pred=df["x"] * 2.0, model_id="m-v1")


def test_window_reads_the_workflows_env(monkeypatch):
    for k, v in {"SERVE_MODEL": "m", "FROM_DATE": "2026-09-01", "TO_DATE": "2026-09-10", "SERVE_RUN_ID": "r1",
                 "SERVE_STARTED_MS": "1790000000000"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(C.PROGRESS_ENV, raising=False)
    for k in (C.RUN_DATE_ENV, C.OUTPUT_ENV, C.BACKFILL_ENV):
        monkeypatch.delenv(k, raising=False)
    assert C.window() == ("m", "2026-09-01", "2026-09-10", "r1", 1790000000000, "", "2026-09-10", {}, False)
    monkeypatch.setenv(C.RUN_DATE_ENV, "2026-09-11")
    monkeypatch.setenv(C.OUTPUT_ENV, '{"mode": "insert_new", "keys": ["k"], "stamps": {}}')
    monkeypatch.setenv(C.BACKFILL_ENV, "true")
    assert C.window()[-3:] == ("2026-09-11", {"mode": "insert_new", "keys": ["k"], "stamps": {}}, True)


def test_a_job_not_started_by_the_workflow_says_so(monkeypatch):
    for k in C.ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="not started by a batch_serve_task"):
        C.window()


def test_chunks_cover_the_window_exactly():
    assert C.chunks("2026-09-01", "2026-09-03", None) == [("2026-09-01", "2026-09-03")]
    assert C.chunks("2026-09-01", "2026-09-03", 2) == [("2026-09-01", "2026-09-02"), ("2026-09-03", "2026-09-03")]


def test_tasks_split_the_window_by_date(monkeypatch):
    seen = {}
    for task in (0, 1):
        monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", str(task))
        monkeypatch.setenv("CLOUD_RUN_TASK_COUNT", "2")
        seen[task] = C.task_chunks("2026-09-01", "2026-09-10")
    assert seen == {0: [("2026-09-01", "2026-09-05")], 1: [("2026-09-06", "2026-09-10")]}
    monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", "1")
    assert C.task_chunks("2026-09-01", "2026-09-10", chunk_days=3) == [("2026-09-04", "2026-09-06"), ("2026-09-10", "2026-09-10")]


class FakeBucket:
    def __init__(self):
        self.names = set()

    def blob(self, name):
        bucket = self

        class Blob:
            def upload_from_string(self, data):
                bucket.names.add(name)
        return Blob()

    def list_blobs(self, prefix):
        return [type("B", (), {"name": n})() for n in sorted(self.names) if n.startswith(prefix)]


def test_a_chunk_noted_done_is_skipped_by_the_runs_repair(monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(C, "_progress", lambda: (bucket, "serve_progress/run-1/m/"))
    for k in ("CLOUD_RUN_TASK_INDEX", "CLOUD_RUN_TASK_COUNT"):
        monkeypatch.delenv(k, raising=False)
    C.chunk_done("2026-09-01", "2026-09-03")
    assert bucket.names == {"serve_progress/run-1/m/2026-09-01_2026-09-03"}
    assert C.task_chunks("2026-09-01", "2026-09-09", chunk_days=3) == [("2026-09-04", "2026-09-06"),
                                                                     ("2026-09-07", "2026-09-09")]
    # Another chunking is another set of chunks: nothing skipped.
    assert len(C.task_chunks("2026-09-01", "2026-09-09", chunk_days=2)) == 5


def test_outside_a_batch_serve_task_nothing_is_noted_or_skipped(monkeypatch):
    monkeypatch.delenv(C.PROGRESS_ENV, raising=False)
    C.chunk_done("2026-09-01", "2026-09-03")
    assert C.task_chunks("2026-09-01", "2026-09-03") == [("2026-09-01", "2026-09-03")]


class Doubler(mlflow.pyfunc.PythonModel):
    def predict(self, context, model_input, params=None):
        return model_input["x"] * 2.0


def test_score_batch_returns_predictions_from_the_aliased_version(monkeypatch, tmp_path):
    fake = FakeVertex()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "vertex://p/us-central1")
    monkeypatch.setenv("MLFLOW_ARTIFACT_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(mlflow, "_backends", {"vertex://p/us-central1": fake})
    monkeypatch.setattr(mlflow, "_runs", [])
    mlflow.set_experiment("e")
    with mlflow.start_run(run_name="r"):
        info = mlflow.pyfunc.log_model("model", python_model=Doubler(), registered_model_name="m",
                                       tags={"model_id": "m-20260901"})
    mlflow.MlflowClient().set_registered_model_alias("m", "champion", info.registered_model_version)
    model = C.load_model("models:/m@champion")
    assert (model.version, model.model_id) == ("1", "m-20260901")
    out = C.score_batch(model, _rows("2026-09-01", "2026-09-05"), prediction_col="pred", keep_columns=KEYS)
    assert list(out.columns) == ["install_date", "campaign", "pred", "model_id", "predicted_at"]
    assert list(out["pred"]) == [0.0, 2.0, 4.0, 6.0, 8.0] and set(out["model_id"]) == {"m-20260901"}
    assert C.score_batch(model, _rows("2026-09-01", "2026-09-05").iloc[0:0], keep_columns=KEYS).empty


def test_load_model_needs_a_models_uri():
    with pytest.raises(ValueError, match="models:/<name>@<alias>"):
        C.load_model("gs://b/model")


@pytest.mark.parametrize("bad, message", [
    (lambda df: df.drop(columns="model_id"), "lack columns"),
    (lambda df: df.assign(model_id=""), "without a model_id"),
    (lambda df: df.assign(install_date=pd.Timestamp("2026-08-01").date()), "outside the window"),
    (lambda df: df.assign(pred=float("inf")), "infinite"),
    (lambda df: pd.concat([df, df]), "repeat"),
])
def test_a_batch_that_would_corrupt_a_window_is_refused(bad, message):
    with pytest.raises(ValueError, match=message):
        C.check_predictions(bad(_preds()), date_col="install_date", prediction_col="pred", entity_id_columns=KEYS,
                            from_date="2026-09-01", to_date="2026-09-03")


def test_an_empty_batch_writes_nothing():
    counts = C.write_window(_preds().iloc[0:0], "p.d.t", "2026-09-01", "2026-09-03", date_col="install_date", entity_id_columns=KEYS,
                            prediction_col="pred", log=lambda _: None)
    assert counts == {"rows_staged": 0, "rows_replaced": 0, "rows_deleted": 0, "rows_inserted": 0}


def test_the_replace_checks_everything_before_it_deletes():
    sql = C.replace_window_sql(target="p.d.t", staging="p.d._stg_t_1", date_col="install_date",
                               entity_id_columns=KEYS, prediction_col="pred")
    delete = sql.index("DELETE FROM")
    assert max(i for i in range(len(sql)) if sql.startswith("ASSERT", i)) < delete
    assert sql.index("BEGIN TRANSACTION") < delete < sql.index("COMMIT TRANSACTION")
    assert sql.index("INSERT INTO `p.d.t`") < sql.index("COMMIT TRANSACTION")
    key = sql[delete:sql.index(";", delete)]
    assert "`install_date` BETWEEN @from_date AND @to_date" in key and "model_id IN UNNEST(staged_versions)" in key
    assert set(re_params(sql)) == {"from_date", "to_date", "min_keep_ratio"}


def re_params(sql: str) -> set[str]:
    import re

    return set(re.findall(r"@(\w+)", sql)) - {"row_count"}  # @@row_count is a system variable


@pytest.mark.parametrize("mode", ["append", "insert_new", "merge"])
def test_an_insert_checks_the_batch_before_it_writes(mode):
    sql = C.insert_sql(target="p.d.t", staging="p.d._stg_t_1", date_col="install_date", mode=mode,
                       keys=KEYS, columns=[*KEYS, "pred", "model_id", "scored_at"], prediction_col="pred")
    write = sql.index("MERGE `p.d.t`" if mode != "append" else "INSERT INTO `p.d.t`")
    assert max(i for i in range(len(sql)) if sql.startswith("ASSERT", i)) < write
    assert sql.index("ALTER TABLE") < write < sql.index("DROP TABLE `p.d._stg_t_1`")
    assert "DELETE" not in sql and set(re_params(sql)) <= {"from_date", "to_date"}
    assert ("GROUP BY `install_date`, `campaign`" in sql) == (mode != "append")


def test_insert_new_never_changes_a_written_row_and_reads_only_the_window():
    sql = C.insert_sql(target="p.d.t", staging="p.d._stg_t_1", date_col="install_date", mode="insert_new",
                       keys=KEYS, columns=[*KEYS, "pred"], prediction_col="pred")
    assert "WHEN NOT MATCHED THEN INSERT" in sql and "WHEN MATCHED" not in sql
    assert "t.`install_date` BETWEEN @from_date AND @to_date" in sql
    merge = C.insert_sql(target="p.d.t", staging="p.d._stg_t_1", date_col="install_date", mode="merge",
                         keys=KEYS, columns=[*KEYS, "pred"], prediction_col="pred")
    update = merge[merge.index("WHEN MATCHED THEN UPDATE SET"):].split(";")[0]
    assert "`pred` = s.`pred`" in update and "`campaign`" not in update


def test_write_refuses_an_unknown_mode_or_a_keyless_insert_new():
    with pytest.raises(ValueError, match="one of"):
        C.write(_preds(), "p.d.t", "2026-09-01", "2026-09-03", date_col="install_date", entity_id_columns=KEYS, prediction_col="pred",
                mode="upsert")
    with pytest.raises(ValueError, match="keys"):
        C.write(_preds(), "p.d.t", "2026-09-01", "2026-09-03", date_col="install_date", entity_id_columns=KEYS, prediction_col="pred",
                mode="insert_new")


def test_an_empty_insert_writes_nothing():
    counts = C.write(_preds().iloc[0:0], "p.d.t", "2026-09-01", "2026-09-03", date_col="install_date", entity_id_columns=KEYS,
                     prediction_col="pred", mode="insert_new", keys=KEYS, log=lambda _: None)
    assert counts == {"rows_staged": 0, "rows_written": 0}


def test_insert_new_refuses_a_batch_repeating_its_keys():
    df = pd.concat([_preds(), _preds().assign(model_id="m-v2")])
    with pytest.raises(ValueError, match="repeat"):
        C.check_predictions(df, date_col="install_date", prediction_col="pred", entity_id_columns=KEYS, from_date="2026-09-01",
                            to_date="2026-09-03", keys=KEYS)


def test_a_table_is_created_with_the_outputs_partition_and_clustering():
    kw = {"target": "p.d.t", "staging": "p.d._stg_t_1", "date_col": "install_date", "prediction_col": "pred"}
    default = C.insert_sql(mode="append", keys=[], columns=["pred"], **kw)
    assert "PARTITION BY `install_date`\nCLUSTER BY `model_id`\n" in default
    assert "PARTITION BY `install_date`\nCLUSTER BY `model_id`\n" in C.replace_window_sql(entity_id_columns=KEYS, **kw)
    sql = C.insert_sql(mode="append", keys=[], columns=["pred"], partition_by="predict_date",
                       cluster_by=["product", "install_date"], **kw)
    assert "PARTITION BY `predict_date`\nCLUSTER BY `product`, `install_date`\nAS SELECT" in sql
    assert "CLUSTER BY" not in C.insert_sql(mode="append", keys=[], columns=["pred"], cluster_by=[], **kw)


class _Table:
    def __init__(self, created):
        self.created = created


class _WatermarkClient:
    def __init__(self, created=None, rows=None):
        self.created, self.rows, self.sql = created, rows, None

    def get_table(self, table):
        from google.api_core.exceptions import NotFound
        if self.created is None:
            raise NotFound(table)
        return _Table(self.created)

    def query(self, sql, job_config=None):
        self.sql = sql
        rows = self.rows

        class _Job:
            def to_dataframe(self):
                return rows
        return _Job()


def test_watermarks_read_each_groups_latest_date_as_of_the_run(monkeypatch):
    from gcpbricks import job_labels

    rows = pd.DataFrame({"product": ["a", "b"], "install_date": [date(2026, 9, 5), None]})
    client = _WatermarkClient(datetime(2026, 9, 1, tzinfo=UTC), rows)
    monkeypatch.setattr(job_labels, "bigquery_client", lambda project=None: client)
    got = C.watermarks("p.d.t", ["product"], date_col="install_date", since=("predict_date", "2026-08-25"), as_of_ms=1790000000000)
    assert got.to_dict("records") == [{"product": "a", "install_date": date(2026, 9, 5)}]
    assert "FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS(1790000000000)" in client.sql
    assert "WHERE `predict_date` >= @since GROUP BY `product`" in client.sql


@pytest.mark.parametrize("created", [None, datetime(2030, 1, 1, tzinfo=UTC)])
def test_no_watermark_while_the_table_is_missing_or_newer_than_the_run(monkeypatch, created):
    from gcpbricks import job_labels

    client = _WatermarkClient(created)
    monkeypatch.setattr(job_labels, "bigquery_client", lambda project=None: client)
    got = C.watermarks("p.d.t", ["product"], date_col="install_date", as_of_ms=1790000000000)
    assert got.empty and list(got.columns) == ["product", "install_date"] and client.sql is None
