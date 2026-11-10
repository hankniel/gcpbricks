"""The console's AI/ML section over a fake Vertex AI."""
from __future__ import annotations

import pytest

from gcpbricks.console import ml as M

P = "projects/p/locations/r"


class FakeVertex:
    def __init__(self):
        self.posts, self.audits = [], []
        self.versions = {"1": ["default"], "2": ["champion"], "3": []}

    def ai_list(self, path, key, **params):
        if path == "metadataStores/default/contexts" and params.get("filter", "").startswith("schema_title"):
            return [{"name": f"{P}/metadataStores/default/contexts/roas", "displayName": "roas",
                     "updateTime": "2026-10-02", "metadata": {}},
                    {"name": f"{P}/metadataStores/default/contexts/gone", "displayName": "gone",
                     "metadata": {"experiment_deleted": True}}]
        if path == "metadataStores/default/contexts":
            return [{"name": f"{P}/c/run1", "displayName": "run1", "schemaTitle": "system.ExperimentRun",
                     "createTime": "2026-10-01", "metadata": {"_params": {"lr": 0.1}, "_metrics": {"rmse": 2.5}}},
                    {"name": f"{P}/c/pipe", "displayName": "pipe", "schemaTitle": "system.PipelineRun",
                     "createTime": "2026-10-02", "metadata": {}},
                    {"name": f"{P}/c/trial0", "displayName": "run1-trial-0", "schemaTitle": "system.ExperimentRun",
                     "createTime": "2026-10-01", "metadata": {"_params": {"tag_mlflow.parentRunId": "run1"}}}]
        if path == "models":
            return [{"name": f"{P}/models/m1", "displayName": "roas_d30"}]
        if path.endswith(":listVersions"):
            return [{"versionId": v, "versionAliases": a, "labels": {"git": v}} for v, a in self.versions.items()]
        if path == "endpoints":
            return [{"name": f"{P}/endpoints/9", "displayName": "roas-online",
                     "trafficSplit": {"d1": 90, "d2": 10},
                     "deployedModels": [{"id": "d1", "displayName": "main", "model": f"{P}/models/m1@2",
                                         "modelVersionId": "2", "dedicatedResources": {
                                             "machineSpec": {"machineType": "n1-standard-4"},
                                             "minReplicaCount": 1, "maxReplicaCount": 1}},
                                        {"id": "d2", "displayName": "canary", "model": f"{P}/models/m1@3",
                                         "modelVersionId": "3"}]}]
        raise KeyError(path)

    def ai_get(self, path, **params):
        if path.startswith("metadataStores"):
            return {"name": f"{P}/metadataStores/default/contexts/roas", "displayName": "roas"}
        if path.startswith("models/"):
            return {"name": f"{P}/models/m1", "displayName": "roas_d30"}
        return self.ai_list("endpoints", "endpoints")[0]

    def ai_post(self, path, body):
        self.posts.append((path.rsplit("@", 1)[-1].split(":")[0], body["versionAliases"]))
        return {}

    def read_json(self, bucket, obj):
        assert (bucket, obj) == ("my-ml-bucket", "feature_store/features.json")
        return {"groups": [{"name": "fs_x", "keys": ["k"], "features": [
            {"name": "cost", "family": "acquisition"}, {"name": "rev", "family": "revenue"},
            {"name": "rev_7", "family": "revenue"}]}]}

    def audit(self, payload):
        self.audits.append(payload)


@pytest.fixture()
def vx():
    return FakeVertex()


def test_experiments_hide_deleted_ones(vx):
    assert [e["name"] for e in M.experiments(vx)] == ["roas"]


def test_an_experiments_runs_carry_params_and_metrics(vx):
    runs = {r["name"]: r for r in M.experiment(vx, "roas")["runs"]}
    assert next(iter(runs)) == "pipe"  # newest first
    run1 = runs["run1"]
    assert (run1["type"], run1["params"], run1["metrics"]) == ("Run", {"lr": 0.1}, {"rmse": 2.5})
    assert runs["pipe"]["type"] == "Pipeline run"
    assert (run1["parent"], runs["run1-trial-0"]["parent"]) == (None, "run1")


def test_models_list_their_versions_and_aliases(vx):
    m = M.models(vx)[0]
    assert (m["name"], m["latest"], m["versions"], m["aliases"]) == ("roas_d30", "3", 3, {"champion": "2"})
    v = M.model(vx, "m1")["versions"]
    assert [x["version"] for x in v] == ["3", "2", "1"] and v[2]["default"] is True and v[2]["aliases"] == []


def test_moving_an_alias_takes_it_off_its_holder_first(vx):
    out = M.set_alias(vx, "u@x.com", "m1", "champion", "3")
    assert vx.posts == [("2", ["-champion"]), ("3", ["champion"])]
    assert out["from"] == ["2"] and vx.audits[0]["user"] == "u@x.com"


def test_removing_an_alias_and_refusing_vertexs_own(vx):
    M.set_alias(vx, "u", "m1", "champion", None)
    assert vx.posts == [("2", ["-champion"])]
    with pytest.raises(ValueError):
        M.set_alias(vx, "u", "m1", "default", "3")


def test_endpoints_show_traffic_per_deployed_model(vx):
    e = M.endpoints(vx)[0]
    assert [(d["name"], d["version"], d["traffic"]) for d in e["deployed"]] == [("main", "2", 90), ("canary", "3", 10)]
    assert e["deployed"][0]["machine"] == "n1-standard-4" and e["ready"]


def test_feature_groups_count_their_families(vx):
    g = M.feature_groups(vx)[0]
    assert g["families"] == {"acquisition": 1, "revenue": 2} and g["n_features"] == 3


def test_a_version_links_its_training_run_and_the_endpoints_serving_it(vx):
    real = vx.ai_list
    vx.ai_list = lambda path, key, **p: ([{"versionId": "2", "versionAliases": ["champion"], "labels": {"model_id": "run1"},
                                          "containerSpec": {"imageUri": "img"}}, {"versionId": "1", "labels": {}}]
                                         if path.endswith(":listVersions") else real(path, key, **p))
    m = M.model(vx, "m1")
    assert m["experiment"] == "roas-d30"
    v2, v1 = m["versions"]
    assert v2["source_run"] == {"experiment": "roas-d30", "id": "run1", "name": "run1", "state": "",
                                "params": {"lr": 0.1}, "metrics": {"rmse": 2.5}}
    assert v2["serving"] == [{"id": "9", "name": "roas-online", "traffic": 90}] and v2["image"] == "img"
    assert v1["source_run"] is None and v1["serving"] == []


def test_a_version_logged_by_mlflow_names_its_own_experiment_and_run(vx):
    real = vx.ai_list

    def listing(path, key, **p):
        if path.endswith(":listVersions"):
            return [{"versionId": "3", "labels": {"mlflow_experiment": "roas-d30", "mlflow_run_id": "run1",
                                                  "model_id": "elsewhere"}}]
        return real(path, key, **p)
    vx.ai_list = listing
    v3 = M.model(vx, "m1")["versions"][0]
    assert (v3["source_run"]["experiment"], v3["source_run"]["name"]) == ("roas-d30", "run1")


def test_a_model_without_an_experiment_has_no_source_runs(vx):
    import requests
    def missing(path, **params):
        if path.startswith("metadataStores"):
            r = requests.Response(); r.status_code = 404
            raise requests.HTTPError(response=r)
        return {"name": f"{P}/models/m1", "displayName": "roas_d30"}
    vx.ai_get = missing
    m = M.model(vx, "m1")
    assert m["experiment"] is None and all(v["source_run"] is None for v in m["versions"])


def test_a_feature_group_shows_its_live_table_against_what_it_declares(monkeypatch):
    from gcpbricks.console import catalog as C
    group = {"name": "fs_x", "offline_table": "ml_feature_store.fs_x", "keys": ["id"], "timestamp": "ts",
             "features": [{"name": "cost"}, {"name": "gone"}], "families": {}, "n_features": 2, "project": "p"}
    monkeypatch.setattr(M, "feature_groups", lambda g: [group])
    seen = {}

    def table(g, dataset, table, project=None):
        seen.update(dataset=dataset, table=table, project=project)
        return {"project": "p", "dataset": dataset, "id": table, "full_name": f"p.{dataset}.{table}", "rows": 10,
                "bytes": 99, "updated": "2026-10-12T00:00:00Z", "partitioning": None, "clustering": [],
                "previewable": True, "schema": [{"name": n, "type": t} for n, t in
                                                (("id", "STRING"), ("ts", "TIMESTAMP"), ("cost", "FLOAT"), ("extra", "INTEGER"))]}
    monkeypatch.setattr(C, "table", table)
    gcp = type("G", (), {"project": "p"})()
    d = M.feature_group(gcp, "fs_x")
    assert seen == {"dataset": "ml_feature_store", "table": "fs_x", "project": None}
    assert (d["live"]["rows"], d["live"]["types"]["cost"]) == (10, "FLOAT")
    assert (d["declared_not_live"], d["live_not_declared"]) == (["gone"], ["extra"])
    monkeypatch.setattr(C, "table", lambda *a, **k: (_ for _ in ()).throw(KeyError("no table p.ml_feature_store.fs_x")))
    assert M.feature_group(gcp, "fs_x")["live_error"] == "no table p.ml_feature_store.fs_x"
    with pytest.raises(KeyError):
        M.feature_group(gcp, "nope")


def test_only_a_retired_resource_is_deleted():
    vx = FakeVertex()
    deleted = []
    vx.ai_delete = lambda path: deleted.append(path) or {}
    labels = {}
    vx.ai_get = lambda path, **p: {"name": f"{P}/endpoints/7", "displayName": "old", "labels": labels}
    with pytest.raises(ValueError, match="not retired"):
        M.delete_endpoint(vx, "admin@example.com", "7")
    labels["mlops_retired"] = "2026-10-19"
    assert M.delete_endpoint(vx, "admin@example.com", "7") == {"deleted": "old"}
    assert deleted == ["endpoints/7"] and vx.audits[-1]["action"] == "delete_endpoint"


def test_a_retired_model_still_served_is_not_deleted():
    vx = FakeVertex()
    vx.ai_delete = lambda path: pytest.fail("deleted a served model")
    get = vx.ai_get
    vx.ai_get = lambda path, **p: ({"name": f"{P}/models/m1", "displayName": "roas_d30",
                                    "labels": {"mlops_retired": "2026-10-19"}} if path == "models/m1" else get(path, **p))
    with pytest.raises(ValueError, match="still deployed"):
        M.delete_model(vx, "admin@example.com", "m1")
