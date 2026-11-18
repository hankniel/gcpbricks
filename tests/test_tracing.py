"""tracing: every line a traced container prints carries the run's trace and its task."""
from __future__ import annotations

import io
import json
import re
import signal
import sys
from pathlib import Path

from gcpbricks import task_runner, tracing

TRACE = "projects/p/traces/faa56bf851eb4e9cb5bd1ce041c8540b"
JOB_YAML = (Path(__file__).parents[1] / "terraform/modules/bundle/workflows/job.yaml").read_text()


def test_a_runs_trace_is_its_workflow_execution_id_as_job_yaml_derives_it():
    assert tracing.trace_name("p", "faa56bf8-51eb-4e9c-b5bd-1ce041c8540b") == TRACE
    expr = '"projects/" + ctx.project + "/traces/" + text.replace_all(ctx.run_id, "-", "")'
    assert JOB_YAML.count(expr) == 2  # each container's env, and a traced runner's argument
    assert set(re.findall(r"name: (MLOPS_TRACE|MLOPS_TASK)", JOB_YAML)) == {tracing.TRACE_ENV, tracing.TASK_ENV}


def test_a_plain_line_becomes_its_message_with_the_trace_and_task_and_no_severity():
    got = json.loads(tracing.entry("chunk 2026-10-19: predict", TRACE, "ua_forecast_mh"))
    assert got == {"message": "chunk 2026-10-19: predict", tracing.TRACE_FIELD: TRACE,
                   tracing.LABELS_FIELD: {"mlops_task": "ua_forecast_mh"}}


def test_a_json_line_keeps_its_own_fields_and_labels():
    line = json.dumps({"severity": "ERROR", "event": "x", tracing.LABELS_FIELD: {"model": "m"}})
    got = json.loads(tracing.entry(line, TRACE, "t"))
    assert got["severity"] == "ERROR" and got["event"] == "x" and got[tracing.TRACE_FIELD] == TRACE
    assert got[tracing.LABELS_FIELD] == {"model": "m", "mlops_task": "t"}
    assert json.loads(tracing.entry("[1, 2]", TRACE)) == {"message": "[1, 2]", tracing.TRACE_FIELD: TRACE}


def test_a_traced_stream_writes_whole_lines_and_the_rest_on_flush():
    out = io.StringIO()
    s = tracing.TracedStream(out, TRACE, "t")
    s.write("a\nb")
    assert [json.loads(x)["message"] for x in out.getvalue().splitlines()] == ["a"]
    s.flush()
    assert [json.loads(x)["message"] for x in out.getvalue().splitlines()] == ["a", "b"]


def test_outside_a_traced_run_nothing_changes(monkeypatch):
    monkeypatch.delenv(tracing.TRACE_ENV, raising=False)
    assert tracing.from_env() is None and tracing.install() is False
    monkeypatch.setenv(tracing.TRACE_ENV, "")  # a runner whose job.yaml sent none
    assert tracing.from_env() is None


def test_a_traced_child_prints_through_the_traced_streams_with_its_exit_code(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", tracing.TracedStream(out, TRACE, "t"))
    monkeypatch.setattr(sys, "stderr", tracing.TracedStream(err, TRACE, "t"))
    code = "import sys; print('to out'); print('to err', file=sys.stderr); sys.exit(3)"
    r = tracing.traced_run([sys.executable, "-c", code], check=False)
    assert r.returncode == 3
    assert [json.loads(x)["message"] for x in out.getvalue().splitlines()] == ["to out"]
    assert [json.loads(x)["message"] for x in err.getvalue().splitlines()] == ["to err"]


def test_an_out_of_memory_kill_is_still_named_and_traced(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", tracing.TracedStream(out, TRACE, "t"))
    monkeypatch.setattr(sys, "stderr", tracing.TracedStream(io.StringIO(), TRACE, "t"))
    code = "import os, signal; print('fetched', flush=True); os.kill(os.getpid(), signal.SIGKILL)"
    r = tracing.traced_run([sys.executable, "-c", code])
    assert task_runner._exit_code(r.returncode, "serve.py") == 128 + signal.SIGKILL
    sys.stdout.flush()
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0]["message"] == "fetched" and "killed by SIGKILL" in lines[-1]["message"]
    assert all(x[tracing.TRACE_FIELD] == TRACE for x in lines)


def test_mlops_task_runs_a_python_file_traced_under_a_traced_run(tmp_path, monkeypatch, capfd):
    root = tmp_path / "files"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "job.py").write_text("print('scored 10 rows')\n")
    monkeypatch.setattr(task_runner.files, "fetch",
                        lambda uri, work, local_root=None, version=None: _copy(root, work) or "v1")
    monkeypatch.setenv(tracing.TRACE_ENV, TRACE)
    monkeypatch.setenv(tracing.TASK_ENV, "ua_forecast_mh")
    try:
        assert task_runner.main(["python", "--files", "gs://b/files", "pkg/job.py"]) == 0
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    lines = [json.loads(x) for x in capfd.readouterr().out.splitlines()]
    assert [x["message"] for x in lines] == ["mlops-task: bundle files v1", "scored 10 rows"]
    assert {x[tracing.LABELS_FIELD]["mlops_task"] for x in lines} == {"ua_forecast_mh"}


def _copy(src: Path, dst: Path) -> None:
    import shutil

    shutil.copytree(src, dst, dirs_exist_ok=True)
