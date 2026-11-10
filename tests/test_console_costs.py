"""The Costs page over fakes: list prices matched from the catalog (the bundled snapshot of it),
notebook runtimes' running time rebuilt from the audit log, and the report's days and sources."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import requests

from gcpbricks.console import costs as C
from gcpbricks.console import prices as PR

SNAP = json.loads(PR.SNAPSHOT.read_text())
NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
SHAPES = {"e2-standard-4": (4, 16384), "n1-highmem-8": (8, 53248), "g2-standard-8": (8, 32768)}


def prices():
    return PR.Prices(lambda sid: SNAP["skus"].get(sid, []), "us-central1")


def shape(mt):
    cpus, mb = SHAPES[mt]
    return PR.machine_shape({"guestCpus": cpus, "memoryMb": mb}, mt)


def test_a_machine_is_priced_from_its_family_skus_usage_and_management_fee():
    p = prices()
    colab = p.colab_hour(shape("n1-highmem-8"), "T4", 1)
    assert colab["missing"] == 0 and len(colab["skus"]) == 6
    assert colab["usd_per_hour"] == pytest.approx(1.1525, abs=1e-3)
    # The E2 management fee's description starts with a space in the catalog.
    assert p.colab_hour(shape("e2-standard-4"), None, 0)["usd_per_hour"] == pytest.approx(0.1876, abs=1e-3)
    assert p.vertex_hour("training", shape("g2-standard-8"), "L4", 1)["usd_per_hour"] == pytest.approx(1.1097, abs=1e-3)
    assert p.colab_disk_month("pd-balanced", 100)["usd_per_month"] == pytest.approx(14.0)
    assert p.cloud_run("job")["usd_per_vcpu_s"] == 0.000018 and p.bigquery_tib("us")["usd"] == 6.25


def test_a_gpu_the_catalog_lacks_leaves_the_machine_unpriced_rather_than_cheaper():
    got = prices().colab_hour(shape("n1-highmem-8"), "Z9", 1)
    assert got["usd_per_hour"] is None and got["missing"] == 2


def entry(t, method, rid, last=True, code=None, template=None):
    return {"timestamp": t, "operation": {"id": f"projects/1/locations/r/notebookRuntimes/{rid}/operations/9",
                                          **({"last": True} if last else {"first": True})},
            "protoPayload": {"methodName": f"google.cloud.aiplatform.v1.NotebookService.{method}",
                             "resourceName": "projects/p/locations/r",
                             **({"status": {"code": code}} if code else {}),
                             **({"request": {"notebookRuntimeTemplate": f"x/notebookRuntimeTemplates/{template}"}} if template else {})}}


def test_runtime_events_count_completed_operations_only():
    events, templates = C.runtime_events([
        entry("2026-10-28T10:00:00Z", "AssignNotebookRuntime", "7", last=False, template="T"),
        entry("2026-10-28T10:01:00Z", "AssignNotebookRuntime", "7"),
        entry("2026-10-28T11:00:00Z", "StartNotebookRuntime", "8", code=9),  # refused: already running
        entry("2026-10-28T12:00:00Z", "StopNotebookRuntime", "7", last=False),
        entry("2026-10-28T12:01:00Z", "StopNotebookRuntime", "7")])
    assert templates == {"7": "T"}
    assert [(t.hour, t.minute, k) for t, k in events["7"]] == [(10, 1, "up"), (12, 1, "down")] and "8" not in events


def at(h):
    return datetime(2026, 10, 28, h, tzinfo=UTC)


def test_an_unlogged_idle_shutdown_is_a_range_and_the_runtime_says_the_last_one():
    events = [(at(1), "up"), (at(2), "down"), (at(4), "up"), (at(9), "up")]
    iv = C.runtime_intervals(events, timedelta(minutes=30), "STOPPED", at(11), NOW)
    assert [(i["start"].hour, i["end"], i["upper"].hour) for i in iv] == [
        (1, at(2), 2), (4, at(4) + timedelta(minutes=30), 9), (9, at(11), 11)]
    running = C.runtime_intervals([(at(9), "up")], timedelta(minutes=30), "RUNNING", at(9), NOW)
    assert running == [{"start": at(9), "end": NOW, "upper": NOW}]


def test_spread_splits_an_interval_across_utc_days():
    from collections import defaultdict
    days = defaultdict(float)
    total = C._spread(days, at(22), at(22) + timedelta(hours=4), 1.0, at(0))
    assert total == pytest.approx(4) and dict(days) == {"2026-10-28": pytest.approx(2), "2026-10-29": pytest.approx(2)}


class FakeGcp:
    project, region = "p", "us-central1"

    def catalog_skus(self, sid):
        r = requests.Response()
        r.status_code, r._content = 403, b"Cloud Billing API has not been used in project p before"
        raise requests.HTTPError(response=r)

    def machine_type(self, zone, mt):
        cpus, mb = SHAPES[mt]
        return {"guestCpus": cpus, "memoryMb": mb}

    def bq_query(self, body):
        r = requests.Response()
        r.status_code = 403
        raise requests.HTTPError(response=r)

    def run_jobs(self):
        return [{"name": "x/jobs/demo-train", "labels": {"mlops_bundle": "demo", "mlops_job": "train"}},
                {"name": "x/jobs/mlops-notify"}, {"name": "x/jobs/legacy-predict"}]

    def run_services(self):
        service = {"updateTime": "2026-10-29T00:00:00Z",
                   "template": {"containers": [{"resources": {"limits": {"cpu": "1", "memory": "512Mi"}, "cpuIdle": True}}],
                                "scaling": {"minInstanceCount": 1}}}
        return [{"name": "x/services/console", **service}, {"name": "x/services/other-team", **service}]

    def time_series(self, flt, start, end, aligner, period="60s"):
        v = 3600.0 if "cpu/" in flt else 7200.0
        label, names = (("job_name", ("demo-train", "mlops-notify", "legacy-predict")) if "cloud_run_job" in flt
                        else ("service_name", ("other-team",)))
        return [{"resource": {"labels": {label: n}},
                 "points": [{"interval": {"endTime": "2026-10-28T10:00:00Z"}, "value": {"doubleValue": v}}]} for n in names]

    def ai_list(self, path, key, **params):
        if path == "customJobs":
            return [{"displayName": "train", "state": "JOB_STATE_SUCCEEDED", "startTime": "2026-10-28T10:00:00Z",
                     "endTime": "2026-10-28T12:00:00Z", "labels": {"mlops_bundle": "demo", "mlops_job": "retrain",
                                                                    "vertex-ai-pipelines-run-billing-id": "1"},
                     "jobSpec": {"workerPoolSpecs": [{"machineSpec": {"machineType": "g2-standard-8",
                                                                      "acceleratorType": "NVIDIA_L4", "acceleratorCount": 1},
                                                      "replicaCount": "1"}]}},
                    {"displayName": "train-legacy", "state": "JOB_STATE_SUCCEEDED", "startTime": "2026-10-28T10:00:00Z",
                     "endTime": "2026-10-28T12:00:00Z", "labels": {"vertex-ai-pipelines-run-billing-id": "2"},
                     "jobSpec": {"workerPoolSpecs": [{"machineSpec": {"machineType": "g2-standard-8"}, "replicaCount": "1"}]}}]
        if path == "notebookRuntimes":
            return [{"name": "x/notebookRuntimes/7", "displayName": "me-1", "runtimeUser": "me@x.com", "runtimeState": "RUNNING",
                     "createTime": "2026-10-29T09:00:00Z", "updateTime": "2026-10-29T09:00:00Z",
                     "machineSpec": {"machineType": "e2-standard-4"},
                     "dataPersistentDiskSpec": {"diskType": "pd-standard", "diskSizeGb": "100"},
                     "idleShutdownConfig": {"idleTimeout": "1800s"}}]
        return []

    def log_entries_all(self, flt, limit=5000):
        return [entry("2026-10-29T09:00:00Z", "AssignNotebookRuntime", "7")]


def test_the_report_adds_up_each_source_by_day_and_names_what_it_could_not_read():
    r = C.report(FakeGcp(), days=2, now=NOW, console="console")
    assert "as of" in r["price_source"] and "isn't enabled" in r["price_source"]
    assert set(r["errors"]) == {"bigquery"} and "missing role" in r["errors"]["bigquery"]
    assert [d["day"] for d in r["days"]] == ["2026-10-28", "2026-10-29"]
    by = {(i["source"], i["name"]): i for i in r["items"]}
    assert by[("cloud_run", "demo/train")]["usd"] == pytest.approx(3600 * 0.000018 + 7200 * 0.000002)
    assert ("cloud_run", "mlops-notify") in by
    assert by[("vertex", "demo/retrain")]["usd"] == pytest.approx(2 * 1.1097, abs=0.01)
    nb = by[("notebooks", "me-1")]
    assert nb["usd"] == pytest.approx(3 * 0.1876 + 100 * 5.6 / 100 * 3 / 730, abs=0.01)  # 3 h running, 3 h of disk
    running = {r["name"]: r for r in r["running"]}
    assert running["me-1"]["usd_per_hour"] == pytest.approx(0.1876, abs=1e-3) and "console" in running
    assert not {"legacy-predict", "other-team", "train-legacy"} & {n for _, n in by}
    assert r["totals"]["all"] == pytest.approx(sum(r["totals"][s] for s in ("cloud_run", "vertex", "notebooks")), abs=0.02)


class ModelGcp:
    """A model version's artifacts in a fake bucket."""

    def __init__(self, files):
        self.files = files

    def ai_get(self, path, **params):
        return {"artifactUri": "gs://b/models/run/artifacts/model"}

    def read_object(self, bucket, obj):
        name = obj.rsplit("/", 1)[1]
        return (self.files[name].encode(), "1") if name in self.files else None


def test_a_version_page_names_what_it_read_what_it_fit_on_and_what_differs():
    from gcpbricks.console import ml as M
    rec = {"read": ["spend", "installs", "country"], "model_inputs": {"default": ["spend_x2", "installs"]},
           "computed": ["spend_x2"], "sources": {"read": "feature_manager fetch_columns"}}
    got = M.version_features(ModelGcp({"features.json": json.dumps(rec)}), "1", "3")
    assert got["logged"] and got["read_not_fit"] == ["spend", "country"] and got["fit_not_read"] == ["spend_x2"]


def test_an_older_version_without_features_json_falls_back_to_its_signature_and_spec():
    from gcpbricks.console import ml as M
    mlmodel = "signature:\n  inputs: '[{\"type\": \"double\", \"name\": \"a\", \"required\": true}]'\n"
    spec = json.dumps({"name": "s", "features": ["fs:a", "fs:b"], "spine_inputs": []})
    got = M.version_features(ModelGcp({"MLmodel": mlmodel, "feature_spec.json": spec}), "1", "3")
    assert not got["logged"] and got["read"] == ["a", "b"] and got["model_inputs"] == {"model": ["a"]}
    assert got["signature"] == [{"name": "a", "type": "double"}] and got["read_not_fit"] == ["b"]
    nothing = M.version_features(ModelGcp({}), "1", "3")
    assert nothing["read"] is None and nothing["model_inputs"] == {}


class TrainedGcp(ModelGcp):
    """A version whose training ran as a Vertex AI pipeline step, priced from the snapshot."""
    region, parent = "us-central1", "projects/p/locations/us-central1"

    def __init__(self, files):
        super().__init__(files)
        self.jobs = [{"displayName": "train", "startTime": "2026-10-28T01:00:00Z", "endTime": "2026-10-28T03:00:00Z",
                      "labels": {"vertex-ai-pipelines-run-billing-id": "9"},
                      "jobSpec": {"workerPoolSpecs": [{"machineSpec": {"machineType": "g2-standard-8",
                                                                       "acceleratorType": "NVIDIA_L4", "acceleratorCount": 1}}]}},
                     {"displayName": "prepare", "startTime": "2026-10-28T00:00:00Z", "endTime": "2026-10-28T01:00:00Z",
                      "labels": {"vertex-ai-pipelines-run-billing-id": "9"},
                      "jobSpec": {"workerPoolSpecs": [{"machineSpec": {"machineType": "e2-standard-4"}}]}}]

    def ai_get(self, path, **params):
        return self.jobs[0] if path.startswith("customJobs/") else super().ai_get(path)

    def ai_list(self, path, key, **params):
        return self.jobs

    catalog_skus = FakeGcp.catalog_skus
    machine_type = FakeGcp.machine_type


def test_a_version_page_prices_its_training_step_and_its_pipeline_run():
    from gcpbricks.console import ml as M
    summary = {"execution": "123", "data": {"splits": {"train": {"rows": 10}}}}
    got = M.version_training(TrainedGcp({"training_summary.json": json.dumps(summary)}), "1", "3")
    c = got["cost"]
    assert c["kind"] == "vertex" and c["seconds"] == 7200 and "g2-standard-8 + 1 x L4" in c["machine"]
    assert c["usd"] == pytest.approx(2 * 1.1097, abs=0.01)
    assert c["pipeline"]["steps"] == 2 and c["pipeline"]["usd"] == pytest.approx(2 * 1.1097 + 0.1742, abs=0.01)
    none = M.version_training(TrainedGcp({"training_summary.json": json.dumps({**summary, "execution": None})}), "1", "3")
    assert none["summary"] and none["cost"] is None
    assert M.version_training(TrainedGcp({}), "1", "3")["summary"] is None


def test_bigquery_reads_the_bundles_region_and_prices_it_at_the_us_rate():
    class Bq:
        sql = ""

        def bq_query(self, body):
            Bq.sql = body["query"]
            return {"rows": [{"f": [{"v": "2026-10-29"}, {"v": "demo-train-ab1cd"}, {"v": False}, {"v": "3"}, {"v": str(2 ** 40)}]}]}

        def run_jobs(self):
            return [{"name": "x/jobs/demo-train", "labels": {"mlops_bundle": "demo", "mlops_job": "train"}}]

    out = C.bigquery(Bq(), prices(), NOW - timedelta(days=1), NOW)
    assert "`region-us-central1`.INFORMATION_SCHEMA" in Bq.sql
    assert out["by_day"]["2026-10-29"] == pytest.approx(6.25) and "US multi-region" in out["notes"][0]
