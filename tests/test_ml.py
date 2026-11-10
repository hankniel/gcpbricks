"""experiments, registered_models and model_serving_endpoints: the schema, the plan, and
apply() against a fake Vertex."""
from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from gcpbricks import bundle as B
from gcpbricks import ml

BASE = {
    "bundle": {"name": "demo", "mlops_version": "0.4.0"},
    "gcp": {"project": "p", "bucket": "b"},
    "run_as": {"service_principal_name": "s"},
    "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}},
}
RESOURCES = {
    "experiments": {"roas": {"name": "roas-d30"}},
    "registered_models": {"roas": {"name": "roas_d30", "comment": "ROAS at day 30"}},
    "model_serving_endpoints": {"roas": {
        "name": "roas-online",
        "config": {"served_entities": [
            {"name": "main", "entity_name": "${resources.registered_models.roas.name}", "entity_version": "3"},
            {"name": "canary", "entity_name": "${resources.registered_models.roas.name}",
             "entity_version": "4", "workload_size": "Medium", "workload_type": "GPU_SMALL"}],
            "traffic_config": {"routes": [
                {"served_entity_name": "main", "traffic_percentage": 90},
                {"served_entity_name": "canary", "traffic_percentage": 10}]}},
        "tags": [{"key": "team", "value": "ua"}]}},
    "jobs": {"train": {"tasks": [{"task_key": "t", "dbt_task": {
        "schema": "s", "commands": ["dbt build --vars experiment=${resources.experiments.roas.name}"]}}]}},
}


def _load(tmp_path, resources=RESOURCES, target="dev") -> B.Loaded:
    path = tmp_path / "mlops.yaml"
    path.write_text(yaml.safe_dump({**BASE, "resources": resources}))
    return B.load(path, target)


def test_names_follow_the_target_as_demo_model_names_them(tmp_path):
    dev, prod = B.ml_plan(_load(tmp_path)), B.ml_plan(_load(tmp_path, target="prod"))
    assert dev["experiments"] == ["roas-d30-dev"] and prod["experiments"] == ["roas-d30"]
    assert dev["registered_models"][0]["display_name"] == "roas_d30_dev"
    assert prod["registered_models"][0]["display_name"] == "roas_d30"
    assert dev["model_serving_endpoints"][0]["display_name"] == "roas-online-dev"


def test_resource_references_resolve_to_the_targets_names(tmp_path):
    lb = _load(tmp_path)
    assert lb.bundle.resources.jobs["train"].tasks[0].dbt_task.commands == [
        "dbt build --vars experiment=roas-d30-dev"]
    assert lb.bundle.resources.model_serving_endpoints["roas"].config.served_entities[0].entity_name == "roas_d30_dev"


def test_served_entities_size_vertex_by_the_workload_table(tmp_path):
    e = B.ml_plan(_load(tmp_path))["model_serving_endpoints"][0]
    small, canary = e["deployments"]
    assert (small["name"], small["machine_type"], small["accelerator_count"]) == ("main", "n1-standard-4", 0)
    assert (small["min_replica_count"], small["max_replica_count"]) == (1, 1)
    assert (canary["machine_type"], canary["accelerator_type"], canary["max_replica_count"]) == (
        "n1-standard-8", "NVIDIA_TESLA_T4", 4)
    assert e["traffic"] == {"main": 90, "canary": 10}
    assert e["labels"] == {"mlops_bundle": "demo", "mlops_target": "dev", "team": "ua"}


@pytest.mark.parametrize("endpoint", [
    {"name": "e", "config": {"served_entities": [
        {"entity_name": "m", "entity_version": "1", "scale_to_zero_enabled": True}]}},
    {"name": "e", "config": {"served_entities": [
        {"entity_name": "m", "entity_version": "1", "workload_type": "MULTIGPU_MEDIUM"}]}},
    {"name": "e", "config": {"served_entities": [
        {"entity_name": "m", "entity_version": "1"}, {"entity_name": "m", "entity_version": "2"}]}},
    {"name": "e", "config": {"served_entities": [{"entity_name": "m", "entity_version": "1"}],
                             "traffic_config": {"routes": [
                                 {"served_entity_name": "m-1", "traffic_percentage": 50}]}}},
    {"name": "e", "config": {"served_entities": [
        {"entity_name": "m", "entity_version": "1", "environment_vars": {"A": "1"}}]}},
    {"name": "e", "ai_gateway": {}, "config": {"served_entities": [
        {"entity_name": "m", "entity_version": "1"}]}},
])
def test_an_endpoint_vertex_cannot_serve_is_refused(tmp_path, endpoint):
    with pytest.raises((ValidationError, ValueError)):
        _load(tmp_path, {"model_serving_endpoints": {"e": endpoint}})


@pytest.mark.parametrize("resources", [
    {"registered_models": {"m": {"catalog_name": "other-project"}}},
    {"registered_models": {"m": {"schema_name": "default"}}},
    {"experiments": {"x": {"name": "/Users/me/x"}}},
    {"experiments": {"x": {"name": "x", "artifact_location": "gs://b/x"}}},
])
def test_ml_settings_without_a_vertex_meaning_are_refused(tmp_path, resources):
    with pytest.raises((ValidationError, ValueError)):
        _load(tmp_path, resources)


# --- apply --------------------------------------------------------------------------------

class FakeVertex:
    def __init__(self, models=(), endpoints=None, aliases=None):
        self.models = {m: f"projects/p/models/{i}" for i, m in enumerate(models)}
        self.endpoints = endpoints or {}       # display name -> {"id", "deployed": [...], ...}
        self.aliases = aliases or {}           # model resource -> {version: [alias]}
        self.calls = []
        self.experiments = set()
        self._n = 0

    def ensure_experiment(self, name):
        self.experiments.add(name)

    def find_model(self, display_name):
        return self.models.get(display_name)

    def update_model(self, model, description):
        self.calls.append(("update_model", model, description))

    def version_aliases(self, model):
        return self.aliases.get(model, {})

    def move_alias(self, model, alias, version):
        self.calls.append(("move_alias", alias, version))

    def find_endpoint(self, display_name):
        return self.endpoints.get(display_name, {}).get("id")

    def create_endpoint(self, display_name, description, labels):
        self.endpoints[display_name] = {"id": f"ep-{display_name}", "deployed": [], "labels": labels}
        self.calls.append(("create_endpoint", display_name))
        return f"ep-{display_name}"

    def update_endpoint(self, endpoint, description, labels):
        self.calls.append(("update_endpoint", endpoint))

    def _ep(self, endpoint):
        return next(e for e in self.endpoints.values() if e["id"] == endpoint)

    def deployed(self, endpoint):
        return list(self._ep(endpoint)["deployed"])

    def deploy(self, endpoint, model, version, name, spec, traffic_percentage):
        self._n += 1
        dm = {"id": f"d{self._n}", "display_name": name, "model": model, "version": version, **spec}
        self._ep(endpoint)["deployed"].append(dm)
        self.calls.append(("deploy", name, version, traffic_percentage))
        return dm["id"]

    def set_traffic(self, endpoint, split):
        self._ep(endpoint)["traffic"] = split
        self.calls.append(("set_traffic", split))

    def undeploy(self, endpoint, deployed_id):
        ep = self._ep(endpoint)
        ep["deployed"] = [d for d in ep["deployed"] if d["id"] != deployed_id]
        self.calls.append(("undeploy", deployed_id))

    def bundle_endpoints(self, labels):
        return [n for n, e in self.endpoints.items() if e.get("labels") == labels]

    def set_retired(self, kind, name, since):
        self.calls.append(("set_retired", kind, name, since))
        return kind != "model" or name in self.models


def _quiet(*_):
    pass


def _plan(tmp_path):
    return B.ml_plan(_load(tmp_path))


def test_apply_creates_the_endpoint_and_splits_traffic(tmp_path):
    vx = FakeVertex(models=["roas_d30_dev"])
    ml.apply(_plan(tmp_path), vx, {"mlops_bundle": "demo", "mlops_target": "dev"}, log=_quiet)
    assert vx.experiments == {"roas-d30-dev"}
    deploys = [c for c in vx.calls if c[0] == "deploy"]
    assert deploys == [("deploy", "main", "3", 100), ("deploy", "canary", "4", 0)]
    assert vx.endpoints["roas-online-dev"]["traffic"] == {"d1": 90, "d2": 10}


def test_a_second_apply_changes_nothing_deployed(tmp_path):
    vx = FakeVertex(models=["roas_d30_dev"])
    ml.apply(_plan(tmp_path), vx, {}, log=_quiet)
    vx.calls.clear()
    ml.apply(_plan(tmp_path), vx, {}, log=_quiet)
    assert not [c for c in vx.calls if c[0] in ("deploy", "undeploy")]


def test_a_new_version_deploys_before_the_old_one_leaves(tmp_path):
    vx = FakeVertex(models=["roas_d30_dev"])
    ml.apply(_plan(tmp_path), vx, {}, log=_quiet)
    vx.calls.clear()
    moved = dict(RESOURCES)
    moved["model_serving_endpoints"] = {"roas": {"name": "roas-online", "config": {"served_entities": [
        {"entity_name": "roas_d30_dev", "entity_version": "4"}]}}}
    ml.apply(B.ml_plan(_load(tmp_path, moved)), vx, {}, log=_quiet)
    kinds = [c[0] for c in vx.calls if c[0] in ("deploy", "set_traffic", "undeploy")]
    assert kinds == ["deploy", "set_traffic", "undeploy", "undeploy"]
    assert [d["display_name"] for d in vx.endpoints["roas-online-dev"]["deployed"]] == ["roas_d30_dev-4"]


def test_an_endpoint_on_an_unregistered_model_fails(tmp_path):
    with pytest.raises(RuntimeError, match="not registered"):
        ml.apply(_plan(tmp_path), FakeVertex(), {}, log=_quiet)


def test_aliases_move_only_when_they_point_elsewhere(tmp_path):
    res = dict(RESOURCES, model_serving_endpoints={})
    res["registered_models"] = {"roas": {"name": "roas_d30", "aliases": [
        {"alias_name": "champion", "version_num": 3}, {"alias_name": "stable", "version_num": 2}]}}
    vx = FakeVertex(models=["roas_d30_dev"], aliases={"projects/p/models/0": {"3": ["champion"]}})
    ml.apply(B.ml_plan(_load(tmp_path, res)), vx, {}, log=_quiet)
    assert [c for c in vx.calls if c[0] == "move_alias"] == [("move_alias", "stable", "2")]


def test_what_the_bundle_no_longer_declares_is_retired_not_deleted(tmp_path):
    labels = {"mlops_bundle": "demo", "mlops_target": "dev"}
    old = {"id": "ep-old", "labels": labels, "deployed": [
        {"id": "a", "display_name": "a"}, {"id": "b", "display_name": "b"}]}
    vx = FakeVertex(models=["roas_d30_dev", "churn_dev"], endpoints={"old-dev": old})
    previous = {"experiments": {"roas": "roas-d30-dev", "churn": "churn-dev"},
                "registered_models": {"roas": "roas_d30_dev", "churn": "churn_dev"},
                "model_serving_endpoints": {"roas": "roas-online-dev"}}
    retired = ml.apply(_plan(tmp_path), vx, labels, previous, "2026-10-19", log=_quiet)
    assert retired == ["experiment churn-dev", "model churn_dev", "endpoint old-dev"]
    assert "old-dev" in vx.endpoints and vx.endpoints["old-dev"]["deployed"] == []
    # Traffic moves onto one model before the others leave it.
    steps = [c for c in vx.calls if c[0] in ("set_traffic", "undeploy") and c != ("set_traffic", {"d1": 90, "d2": 10})]
    assert steps == [("set_traffic", {"a": 100, "b": 0}), ("undeploy", "b"), ("undeploy", "a")]


def test_declaring_a_retired_resource_again_lifts_its_label(tmp_path):
    vx = FakeVertex(models=["roas_d30_dev"])
    ml.apply(_plan(tmp_path), vx, {}, log=_quiet)
    lifted = {(c[1], c[2]) for c in vx.calls if c[0] == "set_retired" and c[3] is None}
    assert lifted == {("experiment", "roas-d30-dev"), ("model", "roas_d30_dev")}


def test_retiring_keeps_the_other_labels_and_the_first_date():
    sdk = object.__new__(ml.VertexSdk)
    sdk.project, sdk.location = "p", "r"
    store = {"labels": {"mlflow_run_id": "x"}}
    patched = []

    def rest(method, resource, **kw):
        r = type("R", (), {"status_code": 200, "raise_for_status": lambda self: None,
                           "json": lambda self: dict(store)})()
        if method == "PATCH":
            store["labels"] = kw["json"]["labels"]
            patched.append(kw["json"]["labels"])
        return r
    sdk._rest = rest
    assert sdk.set_retired("experiment", "x", "2026-10-19")
    assert sdk.set_retired("experiment", "x", "2026-11-01")
    assert store["labels"] == {"mlflow_run_id": "x", "mlops_retired": "2026-10-19"} and len(patched) == 1
    sdk.set_retired("experiment", "x", None)
    assert store["labels"] == {"mlflow_run_id": "x"}
