"""0.4.2's bundle resources: served models on job clusters, schemas (BigQuery datasets) and
dashboards (the console's), from mlops.yaml to the module's plan."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from gcpbricks import bundle as B
from gcpbricks import infra

BASE = {
    "bundle": {"name": "demo", "mlops_version": "0.4.2"},
    "gcp": {"project": "p", "bucket": "b"},
    "run_as": {"service_principal_name": "sa@p.iam.gserviceaccount.com"},
    "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}},
}
PREDICT = {"job_cluster_key": "roas-d90", "new_cluster": {
    "docker_image": {"url": "reg/predict-d90:abc"}, "node_type_id": "e2-standard-8", "num_workers": 4,
    "spark_env_vars": {"MODEL": "roas_d90"}}}


#: What every job cluster's code finds gcpbricks.mlflow configured with, on dev.
MLFLOW_DEV = {"MLFLOW_TRACKING_URI": "vertex://p/us-central1", "MLFLOW_ARTIFACT_ROOT": "gs://b/dev/mlflow"}


def _serve(models: list[dict], clusters=(PREDICT,)) -> dict:
    return {"job_clusters": list(clusters), "tasks": [{"task_key": "serve", "batch_serve_task": {
        "models": models, "window_days": 14}}]}


def _load(tmp_path: Path, resources: dict, target="dev") -> B.Loaded:
    path = tmp_path / "mlops.yaml"
    path.write_text(yaml.safe_dump({**BASE, "resources": resources}))
    return B.load(path, target)


def test_a_served_model_runs_on_its_job_cluster_which_the_platform_creates(tmp_path):
    lb = _load(tmp_path, {"jobs": {"serve": _serve([{"model": "roas_d90", "job_cluster_key": "roas-d90"}])}})
    plan = B.render(lb)
    run_job = "demo-serve-roas-d90-dev"
    assert plan["run_jobs"][run_job] | {"labels": None} == {
        "image": "reg/predict-d90:abc", "env": {**MLFLOW_DEV, "MODEL": "roas_d90"}, "tasks": 4,
        "cpu": "8", "memory": "32Gi", "labels": None, "args": None}
    arg = B.job_definition(lb, "serve")["tasks"]["serve"]["argument"]
    assert arg["models"] == [{"model": "roas_d90", "job": run_job, "from_days": -14, "to_days": 0}]


def test_a_clusters_own_env_overrides_the_mlflow_defaults(tmp_path):
    own = {**PREDICT, "new_cluster": {**PREDICT["new_cluster"],
                                       "spark_env_vars": {"MLFLOW_ARTIFACT_ROOT": "gs://elsewhere"}}}
    lb = _load(tmp_path, {"jobs": {"serve": _serve([{"model": "roas_d90", "job_cluster_key": "roas-d90"}],
                                                   clusters=(own,))}}, target="prod")
    assert B.render(lb)["run_jobs"]["demo-serve-roas-d90"]["env"] == {
        "MLFLOW_TRACKING_URI": "vertex://p/us-central1", "MLFLOW_ARTIFACT_ROOT": "gs://elsewhere"}


def test_a_served_model_may_still_name_an_existing_job(tmp_path):
    lb = _load(tmp_path, {"jobs": {"serve": _serve([{"model": "m", "job": "m-predict"}], clusters=())}})
    assert B.job_definition(lb, "serve")["tasks"]["serve"]["argument"]["models"] == [
        {"model": "m", "job": "m-predict-dev", "from_days": -14, "to_days": 0}]


@pytest.mark.parametrize("model, message", [
    ({"model": "m"}, "give one of job_cluster_key, job or vertex_batch_prediction"),
    ({"model": "m", "job": "x", "job_cluster_key": "roas-d90"}, "give one of job_cluster_key, job or vertex_batch_prediction"),
    ({"model": "m", "job_cluster_key": "nope"}, "not in job_clusters"),
])
def test_a_served_model_names_exactly_one_runner_that_exists(tmp_path, model, message):
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, {"jobs": {"serve": _serve([model])}})


def test_schemas_are_datasets_named_per_target_and_referable(tmp_path):
    res = {"schemas": {"predictions": {"name": "ml_predictions", "comment": "scored cohorts"}},
           "jobs": {"serve": _serve([{"model": "m", "vertex_batch_prediction": {
               **VERTEX, "output_table": "p.${resources.schemas.predictions.name}.churn"}}], clusters=())}}
    dev = B.render(_load(tmp_path, res))
    assert dev["schemas"] == {"predictions": {"project": "p", "dataset_id": "ml_predictions_dev", "location": "us-central1",
                                              "description": "scored cohorts",
                                              "labels": {"mlops_bundle": "demo", "mlops_target": "dev"}}}
    assert B.render(_load(tmp_path, res, "prod"))["schemas"]["predictions"]["dataset_id"] == "ml_predictions"
    lb = _load(tmp_path, res)
    [m] = B.job_definition(lb, "serve")["tasks"]["serve"]["argument"]["models"]
    assert m["vertex"]["output_table"] == "p.ml_predictions_dev.churn"


def test_an_existing_dataset_is_imported_not_recreated(tmp_path):
    lb = _load(tmp_path, {"schemas": {"predictions": {"name": "ml_predictions"}}}, "prod")

    found = infra.existing_schemas(lb, _Datasets({"ml_predictions": "US-CENTRAL1"}))
    assert found == ["predictions"]
    assert infra.root(lb, found)["import"] == [{"to": 'module.bundle.google_bigquery_dataset.this["predictions"]',
                                               "id": "projects/p/datasets/ml_predictions"}]
    assert "import" not in infra.root(lb, [])


def test_an_existing_dataset_in_another_location_stops_the_deploy(tmp_path):
    # Its location forces replacement: terraform would delete the dataset to move it.
    lb = _load(tmp_path, {"schemas": {"predictions": {"name": "ml_predictions", "location": "us-central1"}}}, "prod")
    with pytest.raises(ValueError, match="ml_predictions is in US, declared in us-central1"):
        infra.existing_schemas(lb, _Datasets({"ml_predictions": "US"}))
    assert infra.existing_schemas(lb, _Datasets({"ml_predictions": "US-CENTRAL1"})) == ["predictions"]


class _Datasets:
    """BigQuery's datasets.get over {dataset_id: location}."""

    def __init__(self, live):
        self.live = live

    def get(self, url):
        name = url.rsplit("/", 1)[1]
        r = type("R", (), {})()
        r.status_code = 200 if name in self.live else 404
        r.json = lambda: {"location": self.live[name]}
        return r


def test_a_dashboard_is_its_checked_spec_beside_the_job_definitions(tmp_path):
    (tmp_path / "dashboards").mkdir()
    (tmp_path / "dashboards" / "ops.yaml").write_text(yaml.safe_dump({"widgets": [
        {"title": "Failed runs", "type": "counter", "source": "runs", "filter": {"state": "FAILED"}}]}))
    res = {"dashboards": {"ops": {"display_name": "demo_model ops", "file_path": "dashboards/ops.yaml"}}}
    dev = B.render(_load(tmp_path, res))["dashboards"]["ops"]
    assert dev["object"] == "dev/.bundle/demo/dashboards/ops.json"
    body = json.loads(dev["content"])
    assert (body["title"], body["bundle"], body["target"], body["window"]) == (
        "[dev] demo_model ops", "demo", "dev", "30d")
    assert body["widgets"][0]["filter"] == {"state": "FAILED"}
    prod = B.render(_load(tmp_path, res, "prod"))["dashboards"]["ops"]
    assert prod["object"] == ".bundle/demo/dashboards/ops.json"
    assert json.loads(prod["content"])["title"] == "demo_model ops"


def test_a_dashboard_needs_its_file_and_a_valid_spec(tmp_path):
    with pytest.raises(ValueError, match="no file"):
        B.render(_load(tmp_path, {"dashboards": {"ops": {"display_name": "x", "file_path": "nope.yaml"}}}))
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump({"widgets": [
        {"title": "x", "type": "counter", "source": "runs", "by": "nope"}]}))
    with pytest.raises(ValueError, match="has no field"):
        B.render(_load(tmp_path, {"dashboards": {"ops": {"display_name": "x", "file_path": "bad.yaml"}}}))


def test_a_cluster_may_take_exact_cpu_and_memory(tmp_path):
    cluster = {"job_cluster_key": "roas-d90", "new_cluster": {
        "docker_image": {"url": "img"}, "cpu": 8, "memory": "24Gi"}}
    plan = B.render(_load(tmp_path, {"jobs": {"serve": _serve(
        [{"model": "m", "job_cluster_key": "roas-d90"}], clusters=(cluster,))}}))
    rj = plan["run_jobs"]["demo-serve-roas-d90-dev"]
    assert (rj["cpu"], rj["memory"]) == ("8", "24Gi")


@pytest.mark.parametrize("size, message", [
    ({"cpu": 8, "memory": "48Gi"}, "does not fit"),
    ({"cpu": 3, "memory": "4Gi"}, "does not fit"),
    ({"cpu": 4}, "go together"),
    ({"cpu": 4, "memory": "8Gi", "node_type_id": "e2-standard-4"}, "not both"),
])
def test_exact_sizes_fit_cloud_run(tmp_path, size, message):
    cluster = {"job_cluster_key": "c", "new_cluster": {"docker_image": {"url": "img"}, **size}}
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, {"jobs": {"serve": _serve([{"model": "m", "job_cluster_key": "c"}], clusters=(cluster,))}})


VERTEX = {"registered_model": "churn", "date_col": "install_date", "machine_type": "n1-highmem-8", "max_replica_count": 4,
          "input_table": "p.ml_feature_store.churn_features", "output_table": "p.ml_predictions.churn_predictions",
          "prediction_col": "predicted_churn",
          "entity_id_columns": ["user_id"], "instance_type": "array", "included_fields": ["f1", "f2"]}


def test_a_served_model_may_be_scored_by_a_vertex_batch_prediction_job(tmp_path):
    lb = _load(tmp_path, {"jobs": {"serve": _serve([{"model": "churn", "vertex_batch_prediction": VERTEX}], clusters=())}})
    [m] = B.job_definition(lb, "serve")["tasks"]["serve"]["argument"]["models"]
    assert "job" not in m and m["vertex"] == {
        "registered_model": "churn", "version": "@champion", "machine_type": "n1-highmem-8",
        "starting_replica_count": 1, "max_replica_count": 4, "date_col": "install_date",
        "input_table": "p.ml_feature_store.churn_features",
        "output_table": "p.ml_predictions.churn_predictions",
        "prediction_col": "predicted_churn", "entity_id_columns": ["user_id"], "instance_type": "array",
        "included_fields": ["f1", "f2"], "excluded_fields": [], "prediction_expr": "prediction"}
    assert B.render(lb)["run_jobs"] == {}


@pytest.mark.parametrize("change, message", [
    ({"alias": "champion", "version": "3"}, "alias or version"),
    ({"starting_replica_count": 5}, "below starting_replica_count"),
    ({"included_fields": []}, "needs included_fields"),
])
def test_a_vertex_batch_prediction_is_well_formed(tmp_path, change, message):
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, {"jobs": {"serve": _serve([{"model": "m", "vertex_batch_prediction": {**VERTEX, **change}}],
                                                  clusters=())}})
