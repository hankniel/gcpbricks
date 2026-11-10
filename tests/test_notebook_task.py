"""notebook_task: its schema, what deploy uploads and what its runner is sent."""
from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from gcpbricks import bundle as B

BASE = {
    "bundle": {"name": "demo", "mlops_version": "0.5.0"},
    "gcp": {"project": "p", "bucket": "b"},
    "run_as": {"service_principal_name": "s"},
    "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}},
}
NOTEBOOK = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []}


def _load(tmp_path, task: dict, target="dev") -> B.Loaded:
    (tmp_path / "nb").mkdir(exist_ok=True)
    (tmp_path / "nb" / "report.ipynb").write_text(json.dumps(NOTEBOOK))
    path = tmp_path / "mlops.yaml"
    path.write_text(yaml.safe_dump({**BASE, "resources": {"jobs": {"daily": {"tasks": [{"task_key": "report", **task}]}}}}))
    return B.load(path, target)


def test_a_bundle_notebook_is_uploaded_and_run_on_the_default_template(tmp_path):
    lb = _load(tmp_path, {"notebook_task": {"notebook_path": "nb/report.ipynb", "base_parameters": {"day": "{{job.start_time.iso_date}}"}}})
    plan = B.render(lb)
    assert plan["task_workflows"]["notebook"] == "demo-notebook-dev"
    assert plan["notebooks"]["daily-report"]["object"] == "dev/.bundle/demo/notebooks/daily-report.ipynb"
    assert json.loads(plan["notebooks"]["daily-report"]["content"]) == NOTEBOOK
    task = B.job_definition(lb, "daily")["tasks"]["report"]
    assert (task["workflow"], task["options"], task["options_into"]) == (
        "demo-notebook-dev", {"day": "{{job.start_time.iso_date}}"}, "parameters")
    assert task["argument"]["runtime_template"] == "Default"
    assert task["argument"]["object"] == "dev/.bundle/demo/notebooks/daily-report.ipynb"


def test_a_workspace_notebook_is_read_where_the_console_keeps_it(tmp_path):
    lb = _load(tmp_path, {"notebook_task": {"notebook_path": "/Workspace/Shared/report.ipynb", "runtime_template": "DS v2"}})
    arg = B.job_definition(lb, "daily")["tasks"]["report"]["argument"]
    assert (arg["object"], arg["runtime_template"]) == ("workspace/Shared/report.ipynb", "DS v2")
    assert B.render(lb)["notebooks"] == {}


@pytest.mark.parametrize("task", [
    {"notebook_task": {"notebook_path": "nb/report.py"}},
    {"notebook_task": {"notebook_path": "/etc/report.ipynb"}},
    {"notebook_task": {"notebook_path": "nb/report.ipynb", "source": "WORKSPACE"}},
    {"notebook_task": {"notebook_path": "nb/report.ipynb", "base_parameters": {"not a name": "1"}}},
    {"notebook_task": {"notebook_path": "nb/report.ipynb"}, "job_cluster_key": "main"},
])
def test_a_notebook_task_colab_cannot_run_is_refused(tmp_path, task):
    with pytest.raises((ValidationError, ValueError)):
        _load(tmp_path, task)


def test_a_missing_bundle_notebook_fails_the_render(tmp_path):
    lb = _load(tmp_path, {"notebook_task": {"notebook_path": "nb/gone.ipynb"}})
    with pytest.raises(ValueError, match="no notebook"):
        B.render(lb)
