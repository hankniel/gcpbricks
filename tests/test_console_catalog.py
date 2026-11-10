"""The console's Catalog and lineage over a fake BigQuery, Data Lineage, Vertex and GCS."""
from __future__ import annotations

import pytest
import requests

from gcpbricks.console import catalog as C

P = "projects/p/locations/r"
FS = "p.ml_feature_store.fs"
PRED = "p.ml_predictions.m_predictions"
MANIFEST = {"feature_store": {"feature_table": "ml_feature_store.fs", "label_table": "ml_label.lbl"},
            "serving": {"predict_job": "m-predict"}, "storage": {"predictions_bq_table": PRED}}


def _http(status: int, message: str = "") -> requests.HTTPError:
    r = requests.Response()
    r.status_code = status
    r._content = f'{{"error": {{"message": "{message}"}}}}'.encode()
    return requests.HTTPError(response=r)


class FakeGcp:
    project = "p"

    def __init__(self):
        self.queries, self.audits = [], []
        self.tables = {
            "ml_feature_store/fs": {"type": "TABLE", "location": "US", "numRows": "2", "schema": {"fields": [
                {"name": "k", "type": "STRING", "mode": "REQUIRED"},
                {"name": "at", "type": "TIMESTAMP"},
                {"name": "tags", "type": "STRING", "mode": "REPEATED"},
                {"name": "rec", "type": "RECORD", "fields": [{"name": "x", "type": "INTEGER"}]}]}},
            "ml_feature_store/v": {"type": "VIEW", "location": "US", "view": {"query": "SELECT 1"},
                                   "schema": {"fields": [{"name": "one", "type": "INTEGER"}]}},
            "src/raw": {"type": "TABLE", "location": "US"},
            "ml_predictions/m_predictions": {"type": "TABLE", "location": "US"},
            "ml_label/lbl": {"type": "TABLE", "location": "US"},
        }
        self.links = {("target", "bigquery:p.ml_label.lbl"): [
            {"source": {"fullyQualifiedName": "bigquery:shared-proj.src.events"}},
            {"source": {"fullyQualifiedName": "bigquery:shared-proj.secret.t"}}],
            ("target", f"bigquery:{FS}"): [
            {"source": {"fullyQualifiedName": "bigquery:p.src.raw"}, "endTime": "2026-10-01"},
            {"source": {"fullyQualifiedName": "bigquery:p.src.raw"}, "endTime": "2026-10-09"},
            {"source": {"fullyQualifiedName": "gcs:bucket"}, "endTime": "2026-10-02"}],
            ("source", f"bigquery:{FS}"): [
            {"target": {"fullyQualifiedName": "bigquery:p._script1.tmp"}},
            {"target": {"fullyQualifiedName": "bigquery:p.scratch.dropped"}}]}
        self.manifest = {"train_date_start": "2026-01-01", "train_date_cutoff": "2026-06-30", "manifest": MANIFEST}

    def bq_get(self, path, project=None, **params):
        parts = path.split("/")
        if project == "shared-proj":
            if parts[1] == "secret":
                raise _http(403, "Access Denied")
            if path.endswith("/data"):
                return {"totalRows": "1", "rows": [{"f": [{"v": "x"}]}]}
            return {"type": "TABLE", "location": "US", "schema": {"fields": [{"name": "s", "type": "STRING"}]}}
        if path.endswith("/data"):
            assert params["formatOptions.useInt64Timestamp"] == "true"
            return {"totalRows": "2", "rows": [{"f": [
                {"v": "a"}, {"v": "1760000000000000"}, {"v": [{"v": "t1"}, {"v": "t2"}]}, {"v": {"f": [{"v": "7"}]}}]},
                {"f": [{"v": "b"}, {"v": None}, {"v": []}, {"v": None}]}]}
        key = f"{parts[1]}/{parts[3]}"
        if key not in self.tables:
            raise _http(404)
        return self.tables[key]

    def bq_query(self, body):
        self.queries.append(body)
        if "v`" not in body["query"]:
            raise _http(400, "Query exceeded limit for bytes billed: 104857600.")
        return {"schema": {"fields": [{"name": "one", "type": "INTEGER"}]}, "rows": [{"f": [{"v": "1"}]}],
                "totalRows": "1", "totalBytesBilled": "10485760", "jobComplete": True}

    def lineage_links(self, location, end, fqn):
        assert location == "us"
        return self.links.get((end, fqn), [])

    def ai_list(self, path, key, **params):
        if path == "models":
            return [{"name": f"{P}/models/m1", "displayName": "m"}]
        return [{"versionId": "2", "versionAliases": ["champion"], "artifactUri": "gs://b/m/v2"},
                {"versionId": "3", "versionAliases": [], "artifactUri": "gs://b/m/v3"}]

    def ai_get(self, path, **params):
        return {"name": f"{P}/models/m1", "displayName": "m"}

    def read_json(self, bucket, obj):
        if obj == "m/v2/train_window.json":
            return self.manifest
        if obj == "m/v3/train_window.json":
            return {"train_date_cutoff": "2026-07-01"}
        raise _http(404)

    def run_executions(self, job, n):
        return [{"name": "x/executions/m-predict-a", "startTime": "s", "completionTime": "e", "failedCount": 1},
                {"name": "x/executions/m-predict-b", "startTime": "s"}]

    def audit(self, payload):
        self.audits.append(payload)


@pytest.fixture()
def gcp():
    C._flows_cache.update(at=0.0, flows=[])
    return FakeGcp()


def test_preview_decodes_rows_without_a_query(gcp):
    s = C.preview(gcp, "ml_feature_store", "fs")
    assert s["columns"] == ["k", "at", "tags", "rec"]
    assert s["rows"][0] == ["a", "2025-10-09T08:53:20+00:00", ["t1", "t2"], {"x": "7"}]
    assert s["rows"][1] == ["b", None, [], None]
    assert (s["billed_bytes"], s["source"], gcp.queries) == (0, "preview", [])


def test_a_view_has_no_free_preview(gcp):
    with pytest.raises(ValueError, match="view has no free preview"):
        C.preview(gcp, "ml_feature_store", "v")


def test_query_preview_runs_under_the_bytes_cap_and_is_audited(gcp):
    s = C.query_preview(gcp, "me@x", "ml_feature_store", "v")
    q = gcp.queries[0]
    assert q["query"] == "SELECT * FROM `p.ml_feature_store.v` LIMIT 50"
    assert q["maximumBytesBilled"] == str(C.MAX_QUERY_BYTES)
    assert (s["rows"], s["billed_bytes"]) == ([["1"]], 10485760)
    assert gcp.audits[0]["action"] == "query_preview" and gcp.audits[0]["user"] == "me@x"


def test_query_preview_over_the_cap_says_why(gcp):
    with pytest.raises(ValueError, match="bytes billed"):
        C.query_preview(gcp, "me@x", "ml_feature_store", "fs")


def test_names_are_checked_before_they_reach_sql(gcp):
    with pytest.raises(ValueError, match="not a BigQuery name"):
        C.table(gcp, "ml_feature_store", "fs` WHERE 1=1 --")


def test_a_tables_lineage_drops_temporaries_and_adds_the_models_reading_it(gcp):
    lin = C.lineage(gcp, f"table:{FS}")
    up = {n["id"]: n for n in lin["upstream"]}
    assert set(up) == {"table:p.src.raw", "path:gs://bucket"}
    assert up["table:p.src.raw"]["last_seen"] == "2026-10-09"
    assert up["table:p.src.raw"]["label"] == "src.raw"
    # _script1 is hidden, scratch.dropped no longer exists: only the model is downstream.
    assert [(n["id"], n["role"]) for n in lin["downstream"]] == [("model:m1@2", "features")]


def test_predictions_come_from_the_model_through_its_predict_job(gcp):
    [model] = C.lineage(gcp, f"table:{PRED}")["upstream"]
    assert (model["id"], model["via"]) == ("model:m1@2", {"kind": "job", "name": "m-predict"})
    lin = C.lineage(gcp, "model:m1@2")
    assert (lin["node"]["label"], lin["node"]["aliases"]) == ("m v2", ["champion"])
    assert [(n["id"], n["role"]) for n in lin["upstream"]] == [(f"table:{FS}", "features"), ("table:p.ml_label.lbl", "labels")]
    assert [(n["id"], n["via"]["name"]) for n in lin["downstream"]] == [(f"table:{PRED}", "m-predict")]


def test_table_nodes_carry_their_columns_for_the_graphs_cards(gcp):
    lin = C.lineage(gcp, f"table:{FS}")
    assert (lin["node"]["type"], [c["name"] for c in lin["node"]["columns"]]) == ("TABLE", ["k", "at", "tags", "rec"])


def test_version_lineage_reads_its_manifest_and_its_jobs_runs(gcp):
    v = C.version_lineage(gcp, "m1", "2")
    assert v["flow"]["train_start"] == "2026-01-01" and v["flow"]["predictions"] == PRED
    assert [r["state"] for r in v["runs"]] == ["FAILED", "RUNNING"]


def test_a_version_without_a_manifest_has_no_known_lineage(gcp):
    assert C.version_lineage(gcp, "m1", "3") == {"flow": None, "runs": []}
    with pytest.raises(KeyError):
        C.version_lineage(gcp, "m1", "9")


def test_another_projects_table_reads_when_the_console_may_and_stays_unreadable_when_not(gcp):
    up = {n["id"]: n for n in C.lineage(gcp, "table:p.ml_label.lbl")["upstream"]}
    shared, secret = up["table:shared-proj.src.events"], up["table:shared-proj.secret.t"]
    assert ([c["name"] for c in shared["columns"]], shared.get("readable")) == (["s"], None)
    assert (secret["readable"], "columns" in secret) == (False, False)


def test_another_projects_table_has_a_page_and_a_free_preview(gcp):
    t = C.table(gcp, "src", "events", "shared-proj")
    assert (t["full_name"], t["project"]) == ("shared-proj.src.events", "shared-proj")
    assert C.preview(gcp, "src", "events", "shared-proj")["rows"] == [["x"]]
    with pytest.raises(ValueError, match="can't read shared-proj.secret.t"):
        C.table(gcp, "secret", "t", "shared-proj")
