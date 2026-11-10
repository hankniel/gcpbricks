"""The feature store on its own query engine, over a dbt-features-shaped checkout: a cohort feature
view (one row per cohort per install_date, two families) and a label view, declared in
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
KEYS = [{"name": "cohort", "data_type": "String", "meta": {"entity": True}},
        {"name": "install_date", "data_type": "String", "meta": {"entity": True}},
        {"name": "feature_timestamp", "data_type": "DateTime",
         "meta": {"feature_timestamp": True}}]

def _models(fs_path, lbl_path):
    return {"version": 2, "models": [
        {"name": "fs_cohort", "description": "cohort features",
         "meta": {"feature_store": {"offline_table": str(fs_path), "display_name": "Cohort features",
                                    "families": {"revenue": ["rev_*"]}}},
         "columns": copy.deepcopy(KEYS) + [
             {"name": "cost", "data_type": "Nullable(Float64)", "meta": {"family": "acquisition"}},
             {"name": "installs", "data_type": "UInt32", "meta": {"family": "acquisition"}},
             {"name": "rev_d7", "data_type": "Float64"},
             {"name": "genre", "data_type": "LowCardinality(String)"}]},
        {"name": "lbl_cohort", "meta": {"feature_store": {"offline_table": str(lbl_path), "kind": "label"}},
         "columns": copy.deepcopy(KEYS) + [{"name": "rev_d30", "data_type": "Float64"}]},
        {"name": "stg_not_a_group", "columns": [{"name": "x", "data_type": "String"}]},
    ]}


@pytest.fixture()
def checkout(tmp_path):
    rows = [{"cohort": c, "install_date": d.strftime("%Y-%m-%d"), "feature_timestamp": d,
             "cost": 10.0 * i + j, "installs": 100 + j, "rev_d7": 1.0 * i * j, "genre": "puzzle",
             "rev_d30": 3.0 * i * j}
            for i, c in enumerate(["a", "b"], start=1) for j, d in enumerate(DAYS)]
    df = pd.DataFrame(rows)
    # Microseconds, as BigQuery and Spark write them: Spark can't read pandas' nanoseconds.
    us = {"coerce_timestamps": "us", "allow_truncated_timestamps": True}
    df.drop(columns="rev_d30").to_parquet(tmp_path / "fs.parquet", **us)
    df[["cohort", "install_date", "feature_timestamp", "rev_d30"]].to_parquet(
        tmp_path / "lbl.parquet", **us)
    models = tmp_path / "dbt-features" / "cohort_features" / "models" / "fs"
    models.mkdir(parents=True)
    (models / "_models.yml").write_text(
        yaml.safe_dump(_models(tmp_path / "fs.parquet", tmp_path / "lbl.parquet")))
    return tmp_path / "dbt-features"


@pytest.fixture()
def fs(checkout):
    return FeatureStore(str(checkout), engine="duckdb")


def _spine(dates=DAYS[1:3]):
    return pd.DataFrame([{"cohort": c, "install_date": d.strftime("%Y-%m-%d"),
                          "event_timestamp": d, "label": 1.0}
                         for c in ["a", "b"] for d in dates])


# --- the config ---------------------------------------------------------------------------

def test_groups_come_from_meta_feature_store_and_column_meta():
    groups = C.from_models_yml({"p/_models.yml": _models("f", "l")})
    g = groups["fs_cohort"]
    assert sorted(groups) == ["fs_cohort", "lbl_cohort"]
    assert (g.keys, g.timestamp, g.project) == (["cohort", "install_date"], "feature_timestamp", "p")
    assert (g.display_name, g.description) == ("Cohort features", "cohort features")
    assert groups["lbl_cohort"].display_name == ""
    assert (g.kind, groups["lbl_cohort"].kind) == ("feature", "label")
    assert {f.name: (f.dtype, f.family) for f in g.features} == {
        "cost": ("Float64", "acquisition"), "installs": ("Int64", "acquisition"),
        "rev_d7": ("Float64", "revenue"), "genre": ("String", "default")}


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
        "fs_cohort", "lbl_cohort"]
    assert FeatureStore(str(rendered), engine="duckdb").get_feature_view("fs_cohort").families == {
        "acquisition": ["cost", "installs"], "revenue": ["rev_d7"], "default": ["genre"]}



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
    assert [v.name for v in fs.list_feature_views(family="revenue")] == ["fs_cohort"]
    assert [v.name for v in fs.list_feature_views(kind="label")] == ["lbl_cohort"]
    fv = fs.get_feature_view("lbl_cohort")
    assert (fv.join_keys, fv.timestamp_field, fv.feature_names) == (
        ["cohort", "install_date"], "feature_timestamp", ["rev_d30"])
    with pytest.raises(KeyError, match="no feature view"):
        fs.get_feature_view("nope")


def test_a_view_selects_features_by_name_and_family(fs):
    fv = fs.get_feature_view("fs_cohort")
    assert fv.family("acquisition").refs == ["fs_cohort:cost", "fs_cohort:installs"]
    assert fv[["rev_d7", "cost"]].feature_names == ["rev_d7", "cost"]
    assert fv["genre"].refs == ["fs_cohort:genre"]
    with pytest.raises(KeyError, match="no feature"):
        fv[["nope"]]
    with pytest.raises(KeyError):
        fv.family("nope")


def test_get_features_in_range_returns_every_row_in_the_window(fs):
    fv = fs.get_feature_view("fs_cohort")
    df = fv.family("acquisition").get_features_in_range(DAYS[1], DAYS[3])
    assert len(df) == 2 * 3
    assert set(df.columns) == {"cohort", "install_date", "feature_timestamp", "cost", "installs"}
    assert len(fv.get_features_in_range()) == 2 * len(DAYS)
    assert set(fv[[]].get_features_in_range(DAYS[1], DAYS[1]).columns) == {
        "cohort", "install_date", "feature_timestamp"}
    assert sorted(fv["cost"].get_features_in_range(where="cost > 22").cost) == [23.0, 24.0]


# --- training sets -------------------------------------------------------------------------

def test_a_training_set_joins_features_and_labels_as_of_each_event(fs):
    fv = fs.get_feature_view("fs_cohort")
    ts = fs.create_training_set(_spine(), [fv.family("acquisition"), "fs_cohort:rev_d7", "lbl_cohort"])
    assert ts.feature_service.features == ["fs_cohort:cost", "fs_cohort:installs",
                                           "fs_cohort:rev_d7", "lbl_cohort:rev_d30"]
    df = ts.load_df()
    row = df[(df.cohort == "b") & (df.install_date == "2026-09-02")].iloc[0]
    assert (row.cost, row.installs, row.rev_d7, row.rev_d30) == (21.0, 101, 2.0, 6.0)
    assert len(df) == 4 and df["label"].tolist() == [1.0] * 4
    assert fs.get_features_for_events(_spine(), ["lbl_cohort"]).rev_d30.notna().all()


def test_install_date_as_a_key_keeps_the_join_inside_one_cohort(fs):
    # No feature row for 2026-09-09: with no age limit on the as-of match, only the
    # install_date key stops it from taking 2026-09-05's row.
    df = fs.get_features_for_events(_spine([datetime(2026, 9, 9)]), ["fs_cohort/acquisition"])
    assert df["cost"].isna().all() and len(df) == 2


def test_the_latest_row_at_or_before_the_event_is_taken(checkout):
    """Two rows of one cohort and install_date: the event between them gets the earlier."""
    import pyarrow.parquet as pq

    fs_path = checkout.parent / "fs.parquet"
    df = pq.read_table(fs_path).to_pandas()
    later = df[(df.cohort == "a") & (df.install_date == "2026-09-02")].assign(
        feature_timestamp=datetime(2026, 9, 2, 12), cost=99.0)
    pd.concat([df, later]).to_parquet(fs_path, coerce_timestamps="us", allow_truncated_timestamps=True)
    store = FeatureStore(str(checkout), engine="duckdb")
    events = pd.DataFrame({"cohort": ["a", "a"], "install_date": ["2026-09-02"] * 2,
                           "event_timestamp": [datetime(2026, 9, 2, 6), datetime(2026, 9, 2, 13)]})
    got = store.get_features_for_events(events, ["fs_cohort:cost"]).sort_values("event_timestamp")
    assert got.cost.tolist() == [11.0, 99.0]


def test_a_named_event_timestamp_is_kept(fs):
    spine = _spine().rename(columns={"event_timestamp": "as_of"})
    df = fs.create_training_set(spine, ["fs_cohort/revenue"], timestamp_key="as_of").load_df()
    assert "as_of" in df.columns and "event_timestamp" not in df.columns
    with pytest.raises(KeyError, match="as_of"):
        fs.create_training_set(_spine(), ["fs_cohort/revenue"], timestamp_key="as_of").load_df()


def test_unknown_and_repeated_features(fs):
    with pytest.raises(KeyError):
        fs.create_training_set(_spine(), ["fs_cohort:nope"])
    ts = fs.create_training_set(_spine(), ["fs_cohort/revenue", "fs_cohort:rev_d7"])
    assert ts.feature_service.features == ["fs_cohort:rev_d7"]
    with pytest.raises(ValueError, match="not both"):
        fs.create_training_set(_spine(), ["fs_cohort"], feature_service=FeatureService("s", []))


def test_a_spine_is_a_views_rows_with_other_views_joined_on(fs):
    """Labels come in as features of the label view; its own features off its rows."""
    fv = fs.get_feature_view("fs_cohort")
    df = fs.create_training_set(fv.spine(DAYS[1], DAYS[2]), [fv["cost"], "lbl_cohort:rev_d30"]).load_df()
    got = sorted(zip(df.cohort, df.install_date, df.cost, df.rev_d30))
    assert got == [(c, d.strftime("%Y-%m-%d"), 10.0 * i + j, 3.0 * i * j)
                   for i, c in enumerate(["a", "b"], start=1) for j, d in enumerate(DAYS) if j in (1, 2)]
    assert "feature_timestamp" in df.columns


def test_a_spines_where_drops_its_rows_before_the_join(fs):
    """Databricks filters the spine DataFrame before create_training_set; spine(where=)."""
    fv = fs.get_feature_view("fs_cohort")
    ts = fs.create_training_set(fv.spine(DAYS[0], DAYS[4], where="cost > 12 OR cost IS NULL"),
                                ["fs_cohort:cost", "lbl_cohort:rev_d30"])
    assert sorted(ts.load_df().cost) == [13.0, 14.0, 20.0, 21.0, 22.0, 23.0, 24.0]


def test_exclude_columns_leaves_spine_columns_out(fs):
    fv = fs.get_feature_view("fs_cohort")
    ts = fs.create_training_set(fv.spine(DAYS[0], DAYS[1]), ["fs_cohort:cost"],
                                exclude_columns=["feature_timestamp"])
    assert list(ts.load_df().columns) == ["cohort", "install_date", "cost"]


def test_load_df_float32_halves_the_float_columns(fs):
    df = fs.create_training_set(_spine(), ["fs_cohort:cost", "fs_cohort:installs"]).load_df(float32=True)
    assert df["cost"].dtype == "float32"
    assert df["installs"].dtype != "float32"


def test_write_parquet_writes_load_dfs_rows_in_row_groups(fs, tmp_path, monkeypatch):
    pq = pytest.importorskip("pyarrow.parquet")
    monkeypatch.setattr(F, "ROW_GROUP_ROWS", 4)
    fv = fs.get_feature_view("fs_cohort")
    ts = fs.create_training_set(fv.spine(DAYS[0], DAYS[4], where="cost > 12"),
                                ["fs_cohort:cost", "lbl_cohort:rev_d30"], exclude_columns=["feature_timestamp"])
    assert ts.write_parquet(tmp_path / "t.parquet") == 7

    def rows(df):
        return df.sort_values(["cohort", "install_date"], ignore_index=True)

    pd.testing.assert_frame_equal(rows(pq.read_table(tmp_path / "t.parquet").to_pandas()), rows(ts.load_df()))
    f = pq.ParquetFile(tmp_path / "t.parquet")
    assert [f.metadata.row_group(i).num_rows for i in range(f.num_row_groups)] == [4, 3]


def test_a_query_with_no_rows_writes_an_empty_file_with_its_columns(fs, tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    ts = fs.create_training_set(fs.get_feature_view("fs_cohort").spine(where="cost < 0"), ["fs_cohort:cost"])
    assert ts.write_parquet(tmp_path / "empty.parquet") == 0
    assert pq.read_table(tmp_path / "empty.parquet").column_names == [
        "cohort", "install_date", "feature_timestamp", "cost"]


# --- feature services ----------------------------------------------------------------------

def test_a_service_names_features_by_view(fs):
    fv = fs.get_feature_view("fs_cohort")
    svc = FeatureService("m", [fv.family("acquisition"), fv["cost"], "lbl_cohort:rev_d30"],
                         spine_inputs=["install_date"])
    assert svc.features == ["fs_cohort:cost", "fs_cohort:installs", "lbl_cohort:rev_d30"]
    assert svc.feature_names == ["cost", "installs", "rev_d30"]
    with pytest.raises(ValueError, match="view:feature"):
        FeatureService("m", ["fs_cohort"])
    with pytest.raises(ValueError, match="named the same"):
        FeatureService("m", ["fs_cohort:cost", "lbl_cohort:cost"])


def test_score_batch_reads_what_training_saved(fs, tmp_path):
    fv = fs.get_feature_view("fs_cohort")
    FeatureService("m", [fv["cost"], fv.family("revenue")]).save(tmp_path)
    svc = FeatureService.load(tmp_path)
    assert svc == FeatureService("m", ["fs_cohort:cost", "fs_cohort:rev_d7"])

    class Model:
        def predict(self, x):
            assert list(x.columns) == ["cost", "rev_d7"]
            return x["cost"] + x["rev_d7"]

    out = fs.score_batch(Model(), _spine(), svc)
    row = out[(out.cohort == "a") & (out.install_date == "2026-09-03")].iloc[0]
    assert row.prediction == 12.0 + 2.0
    with pytest.raises(KeyError):
        fs.score_batch(Model(), _spine(), FeatureService("m", ["fs_cohort:dropped_since"]))


def test_a_spec_saved_before_services_had_names_loads(tmp_path):
    (tmp_path / F.SPEC_FILE).write_text(json.dumps(
        {"features": ["fs_cohort:cost"], "spine_inputs": ["install_date"]}))
    svc = FeatureService.load(tmp_path)
    assert (svc.name, svc.feature_names, svc.spine_inputs) == ("", ["cost"], ["install_date"])


# --- the live table ------------------------------------------------------------------------

def test_table_schema_and_max_timestamp_read_the_live_table(fs):
    fv = fs.get_feature_view("fs_cohort")
    schema = fv.table_schema()
    assert {"cohort", "cost", "installs", "rev_d7", "genre"} <= set(schema)
    assert schema["cost"] == "double"
    assert fv.max_timestamp().replace(tzinfo=None) == DAYS[-1]


def test_max_timestamp_reads_the_recent_days_first_and_the_whole_table_only_when_they_are_empty(fs, monkeypatch):
    """A view's latest timestamp in one pruned query when its data is recent, as the frontier of a daily
    serve; an older table (these fixtures' days, months back) falls back to the whole read."""
    import gcpbricks.features as F
    calls = []
    real = fs._execute
    monkeypatch.setattr(fs, "_execute", lambda q: calls.append(q) or real(q))
    fv = fs.get_feature_view("fs_cohort")
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
    fv = fs.get_feature_view("fs_cohort")
    sql = fs.create_training_set(fv.spine(DAYS[1], DAYS[2], where="cost > 0"),
                                 ["fs_cohort:cost", "lbl_cohort:rev_d30"]).to_sql()
    # Every read of the spine's table carries its where, as reads of the old spine CTE did.
    assert sql.count("`proj.ds.fs`") == sql.count("(SELECT * FROM `proj.ds.fs` WHERE (cost > 0))") > 0
    assert sql.count("`proj.ds.lbl`") == 1
    # One as-of match, the label view's.
    assert sql.count("ROW_NUMBER() OVER") == 1
    assert "TIMESTAMP('2026-09-02T00:00:00+00:00')" in sql


def test_on_bigquery_as_of_reads_every_table_at_that_time(tmp_path):
    fs = _bigquery_store(tmp_path, as_of=datetime(2025, 10, 7, 1, 20))  # naive: UTC
    sql = fs.create_training_set(_spine(), ["fs_cohort:cost", "lbl_cohort:rev_d30"]).to_sql()
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
    batch = pa.record_batch({"cohort": ["a", "b"], "cost": pa.array([1.5, 2.5], pa.float64())})
    sent = _fake_bigquery(fs, monkeypatch, [batch] * 3, batch.schema)
    monkeypatch.setattr(F, "ROW_GROUP_ROWS", 4)
    fv = fs.get_feature_view("fs_cohort")
    ts = fs.create_training_set(fv.spine(DAYS[0], DAYS[1]), ["fs_cohort:cost"],
                                exclude_columns=["install_date", "feature_timestamp"])
    assert ts.write_parquet(tmp_path / "t.parquet", float32=True) == 6
    assert "`install_date`" not in sent[0].split(" FROM ")[0]
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
    sql = fs.create_training_set(_spine(), ["fs_cohort:cost"]).to_sql()
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
    refs = ["fs_cohort/acquisition", "fs_cohort:rev_d7", "lbl_cohort:rev_d30"]
    cols = ["cohort", "install_date", "cost", "installs", "rev_d7", "rev_d30"]
    by_spark = FeatureStore(str(checkout), engine="spark", spark_conf=SPARK_CONF).get_features_for_events(
        _spine(), refs)
    by_duckdb = FeatureStore(str(checkout), engine="duckdb").get_features_for_events(_spine(), refs)
    key = ["cohort", "install_date"]
    pd.testing.assert_frame_equal(
        by_spark[cols].sort_values(key).reset_index(drop=True),
        by_duckdb[cols].sort_values(key).reset_index(drop=True), check_dtype=False)


@spark
def test_spark_reads_a_window(checkout):
    fv = FeatureStore(str(checkout), engine="spark", spark_conf=SPARK_CONF).get_feature_view("fs_cohort")
    df = fv.family("acquisition").get_features_in_range(DAYS[1], DAYS[3])
    assert sorted(df.install_date.unique()) == ["2026-09-02", "2026-09-03", "2026-09-04"]


# --- deprecated names, while demo_model's main reads through them ----------------------------

def test_the_deprecated_names_read_the_same(fs):
    from gcpbricks.features import FeatureSpec, score_batch

    group = fs.describe("fs_cohort")
    assert (group["keys"], group["timestamp"]) == (["cohort", "install_date"], "feature_timestamp")
    assert [g["name"] for g in fs.list_groups(family="revenue")] == ["fs_cohort"]
    assert len(fs.read_table("fs_cohort", DAYS[1], DAYS[3], family="acquisition")) == 6
    ts = fs.training_set(fs.spine("fs_cohort", DAYS[1], DAYS[2], where="cost > 0"),
                         ["fs_cohort:cost", "lbl_cohort:rev_d30"], exclude_columns=["feature_timestamp"])
    assert ts.spec.features == ["fs_cohort:cost", "lbl_cohort:rev_d30"]
    assert len(ts.load_df()) == 4
    assert fs.resolve(["fs_cohort/revenue"]) == ["fs_cohort:rev_d7"]
    assert fs.table_schema("fs_cohort")["cost"] == "double"
    assert fs.max_timestamp("fs_cohort").replace(tzinfo=None) == DAYS[-1]
    spec = FeatureSpec(["fs_cohort:cost"], spine_inputs=["cohort"])
    assert (spec.feature_names, spec.spine_inputs) == (["cost"], ["cohort"])

    class Model:
        def predict(self, x):
            return x["cost"]

    assert score_batch(fs, Model(), _spine(), spec)["prediction"].notna().all()
