"""Console-created quality monitors: the console-monitors bundle they deploy as, on
demo-model's settings and account, and create / update / delete over its recorded monitors."""
from __future__ import annotations

import pytest

from gcpbricks import bundle as B
from gcpbricks.console import bundles as BN
from gcpbricks.console import monitor_deploy as MD

SOURCE = {"bundle": "demo-model", "target": "dev", "mode": "development", "project": "p", "region": "us-central1",
          "bucket": "b", "run_as": "sa@p.iam.gserviceaccount.com", "resources": {}}
SPEC = {"table_name": "p.ds.preds", "output_schema_name": "p.mon",
        "inference_log": {"problem_type": "PROBLEM_TYPE_REGRESSION", "prediction_col": "pred", "timestamp_col": "ts",
                          "model_id_col": "model_id", "granularities": ["1 day"]}}
OTHER = {**SPEC, "table_name": "p.ds.other"}


@pytest.fixture
def records(monkeypatch):
    deps = [SOURCE, {"bundle": "console-monitors", "target": "dev",
                     "resources": {"quality_monitors": {"other": {"config": OTHER}}}}]
    monkeypatch.setattr(BN, "deployments", lambda gcp: deps)
    monkeypatch.setattr(BN, "deployment", lambda gcp, b, t: next(
        (d for d in deps if (d["bundle"], d["target"]) == (b, t)), None))
    deployed = []
    monkeypatch.setattr(MD, "_deploy", lambda gcp, user, target, ms, token=None: deployed.append((target, ms))
                        or {"target": target})
    return deployed


def test_the_bundle_takes_demo_model_settings_and_deploys_each_monitors_refresh_job(tmp_path):
    (tmp_path / "mlops.yaml").write_text(MD.bundle_yaml(MD._settings_from(SOURCE), "dev", {"forecast": SPEC}))
    lb = B.load(tmp_path / "mlops.yaml", "dev")
    assert lb.bundle.bundle.name == "console-monitors" and lb.mode == "development"
    assert lb.bundle.run_as.service_principal_name == "sa@p.iam.gserviceaccount.com"
    assert (lb.bundle.gcp.project, lb.bundle.gcp.bucket) == ("p", "b")
    assert "forecast-monitor" in B.render(lb)["jobs"]


def test_targets_are_those_demo_model_is_deployed_to(records):
    assert MD.targets(None) == ["dev"]


def test_create_adds_to_the_recorded_monitors(records):
    MD.upsert(None, "me", "dev", "forecast", SPEC)
    assert records == [("dev", {"other": OTHER, "forecast": SPEC})]


def test_update_replaces_one_and_a_table_takes_one_console_monitor(records):
    MD.upsert(None, "me", "dev", "other", {**OTHER, "slicing_exprs": ["geo"]})
    assert records[-1][1]["other"]["slicing_exprs"] == ["geo"]
    with pytest.raises(ValueError, match="already has console monitor other"):
        MD.upsert(None, "me", "dev", "again", OTHER)


def test_an_invalid_monitor_is_refused_before_any_deploy(records):
    with pytest.raises(ValueError):
        MD.upsert(None, "me", "dev", "forecast", {"table_name": "p.ds.preds", "output_schema_name": "p.mon"})
    assert records == []


def test_delete_deploys_the_rest(records):
    MD.remove(None, "me", "dev", "other")
    assert records == [("dev", {})]
    with pytest.raises(KeyError):
        MD.remove(None, "me", "dev", "nope")


def test_no_demo_model_record_means_no_console_monitors(monkeypatch):
    monkeypatch.setattr(BN, "deployment", lambda gcp, b, t: None)
    with pytest.raises(ValueError, match="takes its settings"):
        MD._settings(None, "prod")


def test_a_deploy_acts_with_the_users_token_when_given(monkeypatch, records):
    seen = []
    monkeypatch.setattr(MD, "_deploy", lambda gcp, user, target, ms, token=None: seen.append(token) or {})
    MD.upsert(None, "me", "dev", "forecast", SPEC, "user-token")
    MD.remove(None, "me", "dev", "other")
    assert seen == ["user-token", None]
