"""The feature store on its own query engine, over a dbt-features-shaped checkout: a segment feature
view (one row per segment per snapshot_date, two families) and a label view, declared in
_models.yml the way dbt-features declares them. Reads run on DuckDB; BigQuery's and Spark's are
checked as the SQL they compile to, and against a fake BigQuery client."""
from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest
import yaml

pytest.importorskip("ibis")
pytest.importorskip("duckdb")
pd = pytest.importorskip("pandas")

from gcpbricks import feature_config as C
from gcpbricks import features as F
from gcpbricks.features import FeatureService, FeatureStore

DAYS = [datetime(2026, 9, d) for d in range(1, 6)]
KEYS = [{"name": "segment", "data_type": "String", "meta": {"entity": True}},
        {"name": "snapshot_date", "data_type": "String", "meta": {"entity": True}},
        {"name": "feature_timestamp", "data_type": "DateTime",
         "meta": {"feature_timestamp": True}}]

def _models(fs_path, lbl_path):
    return {"version": 2, "models": [
        {"name": "fs_segment", "description": "segment features",
         "meta": {"feature_store": {"offline_table": str(fs_path), "display_name": "Segment features",
                                    "families": {"revenue": ["val_*"]}}},
         "columns": copy.deepcopy(KEYS) + [
             {"name": "cost", "data_type": "Nullable(Float64)", "meta": {"family": "acquisition"}},
             {"name": "signups", "data_type": "UInt32", "meta": {"family": "acquisition"}},
             {"name": "val_d7", "data_type": "Float64"},
             {"name": "category", "data_type": "LowCardinality(String)"}]},
        {"name": "lbl_segment", "meta": {"feature_store": {"offline_table": str(lbl_path), "kind": "label"}},
         "columns": copy.deepcopy(KEYS) + [{"name": "val_d30", "data_type": "Float64"}]},
        {"name": "stg_not_a_group", "columns": [{"name": "x", "data_type": "String"}]},
    ]}


@pytest.fixture()
def checkout(tmp_path):
    rows = [{"segment": c, "snapshot_date": d.strftime("%Y-%m-%d"), "feature_timestamp": d,
             "cost": 10.0 * i + j, "signups": 100 + j, "val_d7": 1.0 * i * j, "category": "puzzle",
             "val_d30": 3.0 * i * j}
            for i, c in enumerate(["a", "b"], start=1) for j, d in enumerate(DAYS)]
    df = pd.DataFrame(rows)
    # Microseconds, as BigQuery and Spark write them: Spark can't read pandas' nanoseconds.
    us = {"coerce_timestamps": "us", "allow_truncated_timestamps": True}
    df.drop(columns="val_d30").to_parquet(tmp_path / "fs.parquet", **us)
    df[["segment", "snapshot_date", "feature_timestamp", "val_d30"]].to_parquet(
        tmp_path / "lbl.parquet", **us)
    models = tmp_path / "dbt-features" / "segment_features" / "models" / "fs"
    models.mkdir(parents=True)
    (models / "_models.yml").write_text(
        yaml.safe_dump(_models(tmp_path / "fs.parquet", tmp_path / "lbl.parquet")))
    return tmp_path / "dbt-features"


@pytest.fixture()
def fs(checkout):
    return FeatureStore(str(checkout), engine="duckdb")


def _spine(dates=DAYS[1:3]):
    return pd.DataFrame([{"segment": c, "snapshot_date": d.strftime("%Y-%m-%d"),
                          "event_timestamp": d, "label": 1.0}
                         for c in ["a", "b"] for d in dates])


# --- the config ---------------------------------------------------------------------------

def test_groups_come_from_meta_feature_store_and_column_meta():
    groups = C.from_models_yml({"p/_models.yml": _models("f", "l")})
    g = groups["fs_segment"]
    assert sorted(groups) == ["fs_segment", "lbl_segment"]
    assert (g.keys, g.timestamp, g.project) == (["segment", "snapshot_date"], "feature_timestamp", "p")
    assert (g.display_name, g.description) == ("Segment features", "segment features")
    assert groups["lbl_segment"].display_name == ""
    assert (g.kind, groups["lbl_segment"].kind) == ("feature", "label")
    assert {f.name: (f.dtype, f.family) for f in g.features} == {
        "cost": ("Float64", "acquisition"), "signups": ("Int64", "acquisition"),
        "val_d7": ("Float64", "revenue"), "category": ("String", "default")}


def test_meta_under_config_reads_the_same():
    # dbt-autofix moves model and column meta under config; the v2 parser rejects it top-level
    doc = _models("f", "l")
    for node in doc["models"] + [c for m in doc["models"] for c in m["columns"]]:
        if "meta" in node:
            node["config"] = {"meta": node.pop("meta")}
    assert C.from_models_yml({"p/_models.yml": doc}) == C.from_models_yml(
        {"p/_models.yml": _models("f", "l")})


@pytest.mark.parametrize("bad, message", [
    (lambda m: [c["meta"].pop("entity", None) for c in m["columns"][:2]], "meta.entity"),
    (lambda m: m["columns"].pop(2), "feature_timestamp"),
    (lambda m: m["columns"].append({"name": "m", "data_type": "Map(String, Int8)"}), "data_type"),
    (lambda m: m["meta"]["feature_store"].update(window=3), "window"),
    (lambda m: m["meta"]["feature_store"].update(kind="target"), "kind"),
])
def test_a_bad_declaration_is_refused(bad, message):
    doc = _models("f", "l")
    bad(doc["models"][0])
    with pytest.raises(ValueError, match=message):
        C.from_models_yml({"p": doc})



def test_render_writes_what_the_store_reads(checkout, tmp_path):
    rendered = tmp_path / "features.json"
    assert C.main(["render", str(checkout), "--out", str(rendered)]) == 0
    assert [g["name"] for g in json.loads(rendered.read_text())["groups"]] == [
        "fs_segment", "lbl_segment"]
    assert FeatureStore(str(rendered), engine="duckdb").get_feature_view("fs_segment").families == {
        "acquisition": ["cost", "signups"], "revenue": ["val_d7"], "default": ["category"]}



@pytest.mark.parametrize("data_type, feature", [
    # BigQuery
    ("INT64", "Int64"), ("FLOAT64", "Float64"), ("NUMERIC(10,2)", "Float64"), ("BIGNUMERIC", "Float64"),
    ("BOOL", "Bool"), ("STRING", "String"), ("STRING(64)", "String"), ("DATE", "UnixTimestamp"),
    ("TIMESTAMP", "UnixTimestamp"), ("DATETIME", "UnixTimestamp"),
    # Spark
    ("bigint", "Int64"), ("int", "Int64"), ("double", "Float64"), ("decimal(18,4)", "Float64"),
    ("boolean", "Bool"), ("string", "String"), ("varchar(10)", "String"), ("timestamp_ntz", "UnixTimestamp"),
    # ClickHouse, while dbt-features still builds there
    ("UInt32", "Int64"), ("Nullable(Float64)", "Float64"), ("LowCardinality(String)", "String"),
    ("DateTime64(3)", "UnixTimestamp"),
])
def test_every_adapters_data_type_has_a_feature_type(data_type, feature):
    assert C.feature_type(data_type) == feature


@pytest.mark.parametrize("data_type", ["ARRAY<INT64>", "STRUCT<a INT64>", "map<string,int>", "BYTES"])
def test_a_nested_type_is_not_a_feature(data_type):
    with pytest.raises(ValueError, match="no feature type"):
        C.feature_type(data_type)


def test_a_date_key_is_refused_with_how_to_fix_it(tmp_path):
    models = copy.deepcopy(_models("fs.parquet", "lbl.parquet"))
    models["models"][0]["columns"][1]["data_type"] = "DATE"
    with pytest.raises(ValueError, match="cast a date key to STRING"):
        C.from_models_yml({"x/models/_models.yml": models})


# --- views ---------------------------------------------------------------------------------

def test_views_are_listed_by_family_and_kind(fs):
    assert [v.name for v in fs.list_feature_views(family="revenue")] == ["fs_segment"]
    assert [v.name for v in fs.list_feature_views(kind="label")] == ["lbl_segment"]
    fv = fs.get_feature_view("lbl_segment")
    assert (fv.join_keys, fv.timestamp_field, fv.feature_names) == (
        ["segment", "snapshot_date"], "feature_timestamp", ["val_d30"])
    with pytest.raises(KeyError, match="no feature view"):
        fs.get_feature_view("nope")


def test_a_view_selects_features_by_name_and_family(fs):
    fv = fs.get_feature_view("fs_segment")
    assert fv.family("acquisition").refs == ["fs_segment:cost", "fs_segment:signups"]
    assert fv[["val_d7", "cost"]].feature_names == ["val_d7", "cost"]
    assert fv["category"].refs == ["fs_segment:category"]
    with pytest.raises(KeyError, match="no feature"):
        fv[["nope"]]
    with pytest.raises(KeyError):
        fv.family("nope")


def test_get_features_in_range_returns_every_row_in_the_window(fs):
    fv = fs.get_feature_view("fs_segment")
    df = fv.family("acquisition").get_features_in_range(DAYS[1], DAYS[3])
    assert len(df) == 2 * 3
    assert set(df.columns) == {"segment", "snapshot_date", "feature_timestamp", "cost", "signups"}
    assert len(fv.get_features_in_range()) == 2 * len(DAYS)
    assert set(fv[[]].get_features_in_range(DAYS[1], DAYS[1]).columns) == {
        "segment", "snapshot_date", "feature_timestamp"}
    assert sorted(fv["cost"].get_features_in_range(where="cost > 22").cost) == [23.0, 24.0]


# --- training sets -------------------------------------------------------------------------

def test_a_training_set_joins_features_and_labels_as_of_each_event(fs):
    fv = fs.get_feature_view("fs_segment")
    ts = fs.create_training_set(_spine(), [fv.family("acquisition"), "fs_segment:val_d7", "lbl_segment"])
    assert ts.feature_service.features == ["fs_segment:cost", "fs_segment:signups",
                                           "fs_segment:val_d7", "lbl_segment:val_d30"]
    df = ts.load_df()
    row = df[(df.segment == "b") & (df.snapshot_date == "2026-09-02")].iloc[0]
    assert (row.cost, row.signups, row.val_d7, row.val_d30) == (21.0, 101, 2.0, 6.0)
    assert len(df) == 4 and df["label"].tolist() == [1.0] * 4
    assert fs.get_features_for_events(_spine(), ["lbl_segment"]).val_d30.notna().all()


def test_snapshot_date_as_a_key_keeps_the_join_inside_one_segment(fs):
    # No feature row for 2026-09-09: with no age limit on the as-of match, only the
    # snapshot_date key stops it from taking 2026-09-05's row.
    df = fs.get_features_for_events(_spine([datetime(2026, 9, 9)]), ["fs_segment/acquisition"])
    assert df["cost"].isna().all() and len(df) == 2


def test_the_latest_row_at_or_before_the_event_is_taken(checkout):
    """Two rows of one segment and snapshot_date: the event between them gets the earlier."""
    import pyarrow.parquet as pq

    fs_path = checkout.parent / "fs.parquet"
    df = pq.read_table(fs_path).to_pandas()
    later = df[(df.segment == "a") & (df.snapshot_date == "2026-09-02")].assign(
        feature_timestamp=datetime(2026, 9, 2, 12), cost=99.0)
    pd.concat([df, later]).to_parquet(fs_path, coerce_timestamps="us", allow_truncated_timestamps=True)
    store = FeatureStore(str(checkout), engine="duckdb")
    events = pd.DataFrame({"segment": ["a", "a"], "snapshot_date": ["2026-09-02"] * 2,
                           "event_timestamp": [datetime(2026, 9, 2, 6), datetime(2026, 9, 2, 13)]})
    got = store.get_features_for_events(events, ["fs_segment:cost"]).sort_values("event_timestamp")
    assert got.cost.tolist() == [11.0, 99.0]


def test_a_named_event_timestamp_is_kept(fs):
    spine = _spine().rename(columns={"event_timestamp": "as_of"})
    df = fs.create_training_set(spine, ["fs_segment/revenue"], timestamp_key="as_of").load_df()
    assert "as_of" in df.columns and "event_timestamp" not in df.columns
    with pytest.raises(KeyError, match="as_of"):
        fs.create_training_set(_spine(), ["fs_segment/revenue"], timestamp_key="as_of").load_df()


def test_unknown_and_repeated_features(fs):
    with pytest.raises(KeyError):
        fs.create_training_set(_spine(), ["fs_segment:nope"])
    ts = fs.create_training_set(_spine(), ["fs_segment/revenue", "fs_segment:val_d7"])
    assert ts.feature_service.features == ["fs_segment:val_d7"]
    with pytest.raises(ValueError, match="not both"):
        fs.create_training_set(_spine(), ["fs_segment"], feature_service=FeatureService("s", []))


def test_a_spine_is_a_views_rows_with_other_views_joined_on(fs):
    """Labels come in as features of the label view; its own features off its rows."""
    fv = fs.get_feature_view("fs_segment")
    df = fs.create_training_set(fv.spine(DAYS[1], DAYS[2]), [fv["cost"], "lbl_segment:val_d30"]).load_df()
    got = sorted(zip(df.segment, df.snapshot_date, df.cost, df.val_d30))
    assert got == [(c, d.strftime("%Y-%m-%d"), 10.0 * i + j, 3.0 * i * j)
                   for i, c in enumerate(["a", "b"], start=1) for j, d in enumerate(DAYS) if j in (1, 2)]
    assert "feature_timestamp" in df.columns


def test_a_spines_where_drops_its_rows_before_the_join(fs):
    """Databricks filters the spine DataFrame before create_training_set; spine(where=)."""
    fv = fs.get_feature_view("fs_segment")
    ts = fs.create_training_set(fv.spine(DAYS[0], DAYS[4], where="cost > 12 OR cost IS NULL"),
                                ["fs_segment:cost", "lbl_segment:val_d30"])
    assert sorted(ts.load_df().cost) == [13.0, 14.0, 20.0, 21.0, 22.0, 23.0, 24.0]


def test_exclude_columns_leaves_spine_columns_out(fs):
    fv = fs.get_feature_view("fs_segment")
    ts = fs.create_training_set(fv.spine(DAYS[0], DAYS[1]), ["fs_segment:cost"],
                                exclude_columns=["feature_timestamp"])
    assert list(ts.load_df().columns) == ["segment", "snapshot_date", "cost"]


def test_load_df_float32_halves_the_float_columns(fs):
    df = fs.create_training_set(_spine(), ["fs_segment:cost", "fs_segment:signups"]).load_df(float32=True)
    assert df["cost"].dtype == "float32"
    assert df["signups"].dtype != "float32"


def test_write_parquet_writes_load_dfs_rows_in_row_groups(fs, tmp_path, monkeypatch):
    pq = pytest.importorskip("pyarrow.parquet")
    monkeypatch.setattr(F, "ROW_GROUP_ROWS", 4)
    fv = fs.get_feature_view("fs_segment")
    ts = fs.create_training_set(fv.spine(DAYS[0], DAYS[4], where="cost > 12"),
                                ["fs_segment:cost", "lbl_segment:val_d30"], exclude_columns=["feature_timestamp"])
    assert ts.write_parquet(tmp_path / "t.parquet") == 7

    def rows(df):
        return df.sort_values(["segment", "snapshot_date"], ignore_index=True)

    pd.testing.assert_frame_equal(rows(pq.read_table(tmp_path / "t.parquet").to_pandas()), rows(ts.load_df()))
    f = pq.ParquetFile(tmp_path / "t.parquet")
    assert [f.metadata.row_group(i).num_rows for i in range(f.num_row_groups)] == [4, 3]


def test_a_query_with_no_rows_writes_an_empty_file_with_its_columns(fs, tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    ts = fs.create_training_set(fs.get_feature_view("fs_segment").spine(where="cost < 0"), ["fs_segment:cost"])
    assert ts.write_parquet(tmp_path / "empty.parquet") == 0
    assert pq.read_table(tmp_path / "empty.parquet").column_names == [
        "segment", "snapshot_date", "feature_timestamp", "cost"]


# --- feature services ----------------------------------------------------------------------

def test_a_service_names_features_by_view(fs):
    fv = fs.get_feature_view("fs_segment")
    svc = FeatureService("m", [fv.family("acquisition"), fv["cost"], "lbl_segment:val_d30"],
                         spine_inputs=["snapshot_date"])
    assert svc.features == ["fs_segment:cost", "fs_segment:signups", "lbl_segment:val_d30"]
    assert svc.feature_names == ["cost", "signups", "val_d30"]
    with pytest.raises(ValueError, match="view:feature"):
        FeatureService("m", ["fs_segment"])
    with pytest.raises(ValueError, match="named the same"):
        FeatureService("m", ["fs_segment:cost", "lbl_segment:cost"])


def test_score_batch_reads_what_training_saved(fs, tmp_path):
    fv = fs.get_feature_view("fs_segment")
    FeatureService("m", [fv["cost"], fv.family("revenue")]).save(tmp_path)
    svc = FeatureService.load(tmp_path)
    assert svc == FeatureService("m", ["fs_segment:cost", "fs_segment:val_d7"])

    class Model:
        def predict(self, x):
            assert list(x.columns) == ["cost", "val_d7"]
            return x["cost"] + x["val_d7"]

    out = fs.score_batch(Model(), _spine(), svc)
    row = out[(out.segment == "a") & (out.snapshot_date == "2026-09-03")].iloc[0]
    assert row.prediction == 12.0 + 2.0
    with pytest.raises(KeyError):
        fs.score_batch(Model(), _spine(), FeatureService("m", ["fs_segment:dropped_since"]))


def test_a_spec_saved_before_services_had_names_loads(tmp_path):
    (tmp_path / F.SPEC_FILE).write_text(json.dumps(
        {"features": ["fs_segment:cost"], "spine_inputs": ["snapshot_date"]}))
    svc = FeatureService.load(tmp_path)
    assert (svc.name, svc.feature_names, svc.spine_inputs) == ("", ["cost"], ["snapshot_date"])


# --- the live table ------------------------------------------------------------------------

def test_table_schema_and_max_timestamp_read_the_live_table(fs):
    fv = fs.get_feature_view("fs_segment")
    schema = fv.table_schema()
    assert {"segment", "cost", "signups", "val_d7", "category"} <= set(schema)
    assert schema["cost"] == "double"
    assert fv.max_timestamp().replace(tzinfo=None) == DAYS[-1]


def test_max_timestamp_reads_the_recent_days_first_and_the_whole_table_only_when_they_are_empty(fs, monkeypatch):
    """A view's latest timestamp in one pruned query when its data is recent, as the frontier of a daily
    serve; an older table (these fixtures' days, months back) falls back to the whole read."""
    import gcpbricks.features as F
    calls = []
    real = fs._execute
    monkeypatch.setattr(fs, "_execute", lambda q: calls.append(q) or real(q))
    fv = fs.get_feature_view("fs_segment")
    assert fv.max_timestamp().replace(tzinfo=None) == DAYS[-1] and len(calls) == 2
    calls.clear()
    monkeypatch.setattr(F, "RECENT_DAYS", (datetime.now() - DAYS[0]).days + 30)
    assert fv.max_timestamp().replace(tzinfo=None) == DAYS[-1] and len(calls) == 1


# --- engines -------------------------------------------------------------------------------

def test_an_unknown_engine_is_refused(checkout):
    with pytest.raises(ValueError, match="engine"):
        FeatureStore(str(checkout), engine="snowflake")


def test_as_of_is_bigquery_only(checkout):
    with pytest.raises(ValueError, match="as_of"):
        FeatureStore(str(checkout), engine="duckdb", as_of=datetime(2026, 9, 1))


def _bigquery_store(tmp_path, **kw):
    models = tmp_path / "bq" / "p" / "models"
    models.mkdir(parents=True)
    (models / "_models.yml").write_text(yaml.safe_dump(_models("ds.fs", "ds.lbl")))
    return FeatureStore(str(tmp_path / "bq"), engine="bigquery", bigquery_project="proj", **kw)


def test_on_bigquery_a_spine_reads_its_own_features_off_its_rows(tmp_path):
    fs = _bigquery_store(tmp_path)
    fv = fs.get_feature_view("fs_segment")
    sql = fs.create_training_set(fv.spine(DAYS[1], DAYS[2], where="cost > 0"),
                                 ["fs_segment:cost", "lbl_segment:val_d30"]).to_sql()
    # Every read of the spine's table carries its where, as reads of the old spine CTE did.
    assert sql.count("`proj.ds.fs`") == sql.count("(SELECT * FROM `proj.ds.fs` WHERE (cost > 0))") > 0
    assert sql.count("`proj.ds.lbl`") == 1
    # One as-of match, the label view's.
    assert sql.count("ROW_NUMBER() OVER") == 1
    assert "TIMESTAMP('2026-09-02T00:00:00+00:00')" in sql


def test_on_bigquery_as_of_reads_every_table_at_that_time(tmp_path):
    fs = _bigquery_store(tmp_path, as_of=datetime(2025, 10, 7, 1, 20))  # naive: UTC
    sql = fs.create_training_set(_spine(), ["fs_segment:cost", "lbl_segment:val_d30"]).to_sql()
    travel = "FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS(1759800000000)"
    assert f"`proj.ds.fs` {travel}" in sql and f"`proj.ds.lbl` {travel}" in sql
    assert "FROM `__spine`" in sql


def _fake_bigquery(fs, monkeypatch, batches, schema):
    """fs's BigQuery client, faked: every query returns `batches`; the SQL it got."""
    from google.cloud import bigquery_storage

    sent, tables, deleted, streams = [], [], [], []
    fs._bigquery_tables = (tables, deleted, streams)

    class Rows:
        def to_arrow_iterable(self, bqstorage_client, max_queue_size, max_stream_count=None):
            streams.append(max_stream_count)
            return iter(batches)

        def to_arrow(self, create_bqstorage_client=False):
            import pyarrow as pa
            return pa.Table.from_batches(batches, schema=schema)

    class Client:
        def query(self, sql, job_config=None):
            sent.append(sql)
            d = job_config.destination
            tables.append(f"{d.project}.{d.dataset_id}.{d.table_id}")
            return type("Job", (), {"result": lambda self: Rows()})()

        def get_table(self, ref):
            return type("Table", (), {"expires": None})()

        def update_table(self, table, fields):
            pass

        def delete_table(self, ref, not_found_ok=False):
            deleted.append(str(ref))

    monkeypatch.setattr(F.job_labels, "bigquery_client", lambda **kw: Client())
    monkeypatch.setattr(bigquery_storage, "BigQueryReadClient", lambda: None)
    return sent


def test_on_bigquery_write_parquet_streams_the_query_in_row_groups(tmp_path, monkeypatch):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    pytest.importorskip("google.cloud.bigquery_storage")

    fs = _bigquery_store(tmp_path)
    batch = pa.record_batch({"segment": ["a", "b"], "cost": pa.array([1.5, 2.5], pa.float64())})
    sent = _fake_bigquery(fs, monkeypatch, [batch] * 3, batch.schema)
    monkeypatch.setattr(F, "ROW_GROUP_ROWS", 4)
    fv = fs.get_feature_view("fs_segment")
    ts = fs.create_training_set(fv.spine(DAYS[0], DAYS[1]), ["fs_segment:cost"],
                                exclude_columns=["snapshot_date", "feature_timestamp"])
    assert ts.write_parquet(tmp_path / "t.parquet", float32=True) == 6
    assert "`snapshot_date`" not in sent[0].split(" FROM ")[0]
    f = pq.ParquetFile(tmp_path / "t.parquet")
    assert [f.metadata.row_group(i).num_rows for i in range(f.num_row_groups)] == [4, 2]
    assert f.schema_arrow.field("cost").type == pa.float32()
    out = ts.load_df(float32=True)
    assert out["cost"].dtype == "float32" and len(out) == 6
    # Each result lands in its own scratch table, past the anonymous one's 10 GB, and is dropped.
    tables, deleted, streams = fs._bigquery_tables
    assert len(set(tables)) == 2 and all(".feast.result_" in t for t in tables)
    assert sorted(deleted) == sorted(tables)
    # A bounded number of read streams, so a large result's download holds a bounded memory.
    assert streams == [F.READ_STREAMS]


def test_on_spark_a_parquet_view_reads_as_a_path_and_a_table_takes_the_catalog(tmp_path, checkout):
    pytest.importorskip("pyspark")
    fs = FeatureStore(str(checkout), engine="spark")
    sql = fs.create_training_set(_spine(), ["fs_segment:cost"]).to_sql()
    assert f"parquet.`{checkout.parent / 'fs.parquet'}`" in sql
    fs.spark_catalog = "lake"
    g = C.FeatureGroup(name="g", offline_table="ml_feature_store.fs_x", keys=["k"],
                       key_types={"k": "String"}, timestamp="ts",
                       features=[C.Feature(name="f", dtype="Float64")])
    assert fs._from(g) == "lake.ml_feature_store.fs_x"
    assert fs._from(g.model_copy(update={"offline_table": "other.ml_feature_store.fs_x"})) == \
        "other.ml_feature_store.fs_x"


def _spark_ready() -> bool:
    import shutil
    import subprocess

    try:
        import pyspark  # noqa: F401
    except ImportError:
        return False
    java = shutil.which("java")
    out = subprocess.run([java, "-version"], capture_output=True, text=True, check=False).stderr if java else ""
    return any(f'version "{v}' in out for v in ("17", "21", "25"))


spark = pytest.mark.skipif(not _spark_ready(), reason="needs pyspark 4 and Java 17+")
SPARK_CONF = {"spark.master": "local[1]", "spark.sql.session.timeZone": "UTC", "spark.ui.enabled": "false"}


@spark
def test_spark_joins_what_duckdb_joins(checkout):
    """The same views and events through both engines give the same training set."""
    refs = ["fs_segment/acquisition", "fs_segment:val_d7", "lbl_segment:val_d30"]
    cols = ["segment", "snapshot_date", "cost", "signups", "val_d7", "val_d30"]
    by_spark = FeatureStore(str(checkout), engine="spark", spark_conf=SPARK_CONF).get_features_for_events(
        _spine(), refs)
    by_duckdb = FeatureStore(str(checkout), engine="duckdb").get_features_for_events(_spine(), refs)
    key = ["segment", "snapshot_date"]
    pd.testing.assert_frame_equal(
        by_spark[cols].sort_values(key).reset_index(drop=True),
        by_duckdb[cols].sort_values(key).reset_index(drop=True), check_dtype=False)


@spark
def test_spark_reads_a_window(checkout):
    fv = FeatureStore(str(checkout), engine="spark", spark_conf=SPARK_CONF).get_feature_view("fs_segment")
    df = fv.family("acquisition").get_features_in_range(DAYS[1], DAYS[3])
    assert sorted(df.snapshot_date.unique()) == ["2026-09-02", "2026-09-03", "2026-09-04"]


# --- deprecated names, while demo_model's main reads through them ----------------------------

def test_the_deprecated_names_read_the_same(fs):
    from gcpbricks.features import FeatureSpec, score_batch

    group = fs.describe("fs_segment")
    assert (group["keys"], group["timestamp"]) == (["segment", "snapshot_date"], "feature_timestamp")
    assert [g["name"] for g in fs.list_groups(family="revenue")] == ["fs_segment"]
    assert len(fs.read_table("fs_segment", DAYS[1], DAYS[3], family="acquisition")) == 6
    ts = fs.training_set(fs.spine("fs_segment", DAYS[1], DAYS[2], where="cost > 0"),
                         ["fs_segment:cost", "lbl_segment:val_d30"], exclude_columns=["feature_timestamp"])
    assert ts.spec.features == ["fs_segment:cost", "lbl_segment:val_d30"]
    assert len(ts.load_df()) == 4
    assert fs.resolve(["fs_segment/revenue"]) == ["fs_segment:val_d7"]
    assert fs.table_schema("fs_segment")["cost"] == "double"
    assert fs.max_timestamp("fs_segment").replace(tzinfo=None) == DAYS[-1]
    spec = FeatureSpec(["fs_segment:cost"], spine_inputs=["segment"])
    assert (spec.feature_names, spec.spine_inputs) == (["cost"], ["segment"])

    class Model:
        def predict(self, x):
            return x["cost"]

    assert score_batch(fs, Model(), _spine(), spec)["prediction"].notna().all()
