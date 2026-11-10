"""A vertex_pipeline_task's pipeline in the console: its compiled definition and a run's steps."""
from __future__ import annotations

import pytest

from gcpbricks.console import pipeline_view as PV
from gcpbricks.console import service as S

SOURCE = "\nimport kfp\n\ndef prepare_op(model: str) -> str:\n    return model\n"
SPEC = {
    "pipelineInfo": {"name": "train"}, "sdkVersion": "kfp-2.16.1",
    "root": {"inputDefinitions": {"parameters": {"model": {"parameterType": "STRING"},
                                                 "force": {"parameterType": "STRING", "defaultValue": ""}}},
             "dag": {"tasks": {
                 "prepare-op": {"componentRef": {"name": "comp-prepare-op"}, "taskInfo": {"name": "prepare-op"},
                                "cachingOptions": {"enableCache": True},
                                "inputs": {"parameters": {"model": {"componentInputParameter": "model"},
                                                          "sweep": {"runtimeValue": {"constant": ""}}}}},
                 "condition-1": {"componentRef": {"name": "comp-condition-1"}, "dependentTasks": ["prepare-op"],
                                 "triggerPolicy": {"condition": "inputs.parameter_values['pipelinechannel--prepare-op-go'] == true"},
                                 "inputs": {"parameters": {
                                     "pipelinechannel--prepare-op-uri": {"taskOutputParameter": {"producerTask": "prepare-op", "outputParameterKey": "uri"}},
                                     "pipelinechannel--model": {"componentInputParameter": "model"}}}}}}},
    "components": {
        "comp-prepare-op": {"executorLabel": "exec-prepare-op",
                            "inputDefinitions": {"parameters": {"model": {"parameterType": "STRING"},
                                                                "sweep": {"parameterType": "STRING", "isOptional": True}}},
                            "outputDefinitions": {"parameters": {"uri": {"parameterType": "STRING"}},
                                                  "artifacts": {"report": {"artifactType": {"schemaTitle": "system.Markdown"}}}}},
        "comp-condition-1": {"dag": {"tasks": {
            "train-op": {"componentRef": {"name": "comp-train-op"}, "taskInfo": {"name": "train-op"},
                         "inputs": {"parameters": {"data": {"componentInputParameter": "pipelinechannel--prepare-op-uri"},
                                                   "model": {"componentInputParameter": "pipelinechannel--model"}}}}}}},
        "comp-train-op": {"executorLabel": "exec-train-op",
                          "inputDefinitions": {"parameters": {"data": {"parameterType": "STRING"}, "model": {"parameterType": "STRING"}}}}},
    "deploymentSpec": {"executors": {
        "exec-prepare-op": {"container": {"image": "img:1", "command": ["sh", "-c", "pip install", "sh", "-ec", "run", SOURCE],
                                          "args": ["--executor_input", "{{$}}", "--function_to_execute", "prepare_op"],
                                          "resources": {"resourceCpuLimit": "8", "resourceMemoryLimit": "32G"}}},
        "exec-train-op": {"container": {"image": "img:1", "command": ["python", "-m", "train"]}}}},
}


def test_a_steps_component_says_what_feeds_it_and_what_it_runs():
    c = PV.component(SPEC, "prepare-op")
    assert {i["name"]: i["from"] for i in c["inputs"]} == {"model": "pipeline parameter model", "sweep": "= ''"}
    assert [o["name"] for o in c["outputs"]] == ["uri", "report"] and c["cache"]
    assert c["runs"]["image"] == "img:1" and c["runs"]["function"] == "prepare_op"
    assert c["runs"]["source"] == SOURCE and c["runs"]["resources"]["cpu"] == "8"


def test_a_step_under_a_condition_reads_through_it_to_its_real_source():
    c = PV.component(SPEC, "train-op")
    assert {i["name"]: i["from"] for i in c["inputs"]} == {"data": "prepare-op.uri", "model": "pipeline parameter model"}
    cond = PV.component(SPEC, "condition-1")
    assert cond["runs"]["kind"] == "dag" and cond["condition"] == "go == true"


class FakeGcp:
    def __init__(self):
        self.objects = {
            ("b", "jobs/train.json"): {"tasks": {"train": {"type": "workflow", "argument": {
                "pipeline": "train", "spec_prefix": "pipes/train", "bucket": "b"}}}},
            ("b", "pipes/train/current.json"): {"version": "v1", "image": "img:1",
                                                "train": {"spec": "gs://b/pipes/train/v1/train_pipeline.json",
                                                          "params": "gs://b/pipes/train/v1/train_params.json"}},
            ("b", "pipes/train/v1/train_pipeline.json"): SPEC,
            ("b", "pipes/train/v1/train_params.json"): {"parameter_values": {"model": "roas_d90"}}}
        self.job = {"name": "projects/p/locations/r/pipelineJobs/run-1", "pipelineSpec": SPEC, "jobDetail": {"taskDetails": [
            {"taskName": "prepare-op", "state": "SUCCEEDED", "startTime": "2026-01-01T00:00:00Z", "endTime": "2026-01-01T00:05:00Z",
             "execution": {"state": "CACHED", "metadata": {"input:model": "roas_d90", "output:uri": "gs://b/data"}},
             "outputs": {"report": {"artifacts": [{"name": "a/1", "uri": "gs://b/verdict.md", "schemaTitle": "system.Markdown"}]}},
             "executorDetail": {"containerDetail": {"mainJob": "projects/p/locations/r/customJobs/42"}}}]}}
        self.log_filters = []

    def read_json(self, bucket, name):
        return self.objects[(bucket, name)]

    def read_object(self, bucket, name):
        return (b"# Gate passed", "1") if (bucket, name) == ("b", "verdict.md") else None

    def ai_get(self, name, **_):
        return self.job

    def log_entries(self, flt, n=500, newest_first=False):
        self.log_filters.append(flt)
        return [{"timestamp": "2026-01-01T00:01:00Z", "severity": "INFO", "resource": {"labels": {"task_name": "workerpool0-0"}},
                 "textPayload": "fitting"}]


@pytest.fixture()
def gcp(monkeypatch):
    g = FakeGcp()
    monkeypatch.setattr(S, "_find", lambda gcp, wf: {"job": "retrain", "bucket": "b", "definition": "jobs/train.json"})
    monkeypatch.setattr(S, "task_kind", lambda spec: "vertex_pipeline_task")
    monkeypatch.setattr(S, "pipeline_run", lambda *a: {"run": {"name": g.job["name"]}})
    monkeypatch.setattr(PV.ST, "custom_job", lambda gcp, name, step: {"name": name, "machine": {}, "usage": {}})
    return g


def test_the_definition_is_the_published_pipeline_with_the_values_the_task_passes(gcp):
    d = PV.definition(gcp, "retrain", "train")
    assert d["version"] == "v1" and d["name"] == "train"
    assert {p["name"]: (p["value"], p["default"]) for p in d["parameters"]} == {"model": ("roas_d90", None), "force": (None, "")}
    assert set(d["steps"]) == {"prepare-op", "condition-1", "train-op"}
    assert all(n["state"] is None for n in d["nodes"])


def test_a_run_step_has_its_values_artifacts_cache_logs_and_compute(gcp):
    s = PV.step(gcp, "retrain", "e1", "train", "prepare-op")
    assert s["inputs"] == {"model": "roas_d90"} and s["outputs"] == {"uri": "gs://b/data"} and s["cached"]
    assert s["output_artifacts"][0]["name"] == "report" and s["output_artifacts"][0]["text"]
    assert s["logs"][0]["text"] == "fitting" and 'job_id="42"' in gcp.log_filters[0] and "timestamp>=" in gcp.log_filters[0]
    assert s["compute"]["name"].endswith("customJobs/42") and s["component"]["key"] == "prepare-op"
    with pytest.raises(KeyError):
        PV.step(gcp, "retrain", "e1", "train", "no-such-step")


def test_a_step_vertex_hasnt_started_yet_reads_as_queued_with_its_component(gcp):
    s = PV.step(gcp, "retrain", "e1", "train", "train-op")
    assert s["state"] == "QUEUED" and s["start"] is None and s["logs"] == [] and s["compute"] is None
    assert s["component"]["key"] == "train-op"


def test_only_a_text_artifact_the_step_wrote_is_read(gcp):
    assert PV.artifact_text(gcp, "retrain", "e1", "train", "prepare-op", "report")["text"] == "# Gate passed"
    with pytest.raises(KeyError):
        PV.artifact_text(gcp, "retrain", "e1", "train", "prepare-op", "../secrets")
