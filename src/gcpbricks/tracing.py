"""One Cloud Trace id per run, on every line its containers print, so a run's logs are one
Cloud Logging query (`trace="projects/P/traces/ID"`) however deep the task that printed them.

workflows/job.yaml derives the trace from the run id (a repair keeps it) and hands it, with the
task's key, to each container as TRACE_ENV / TASK_ENV, and to the batch_serve runner, which
hands them on to its models' jobs. mlops-task (task_runner) then prints each line of its own
and of the command it runs as Cloud Run structured logging: the line as `message`, with the
trace and an `mlops_task` label. Nothing else changes: a line's severity stays unset, as for
plain text, and a line that already is a JSON object keeps its own fields.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading

TRACE_ENV = "MLOPS_TRACE"
TASK_ENV = "MLOPS_TASK"
TASK_LABEL = "mlops_task"
TRACE_FIELD = "logging.googleapis.com/trace"
LABELS_FIELD = "logging.googleapis.com/labels"


def trace_name(project: str, run_id: str) -> str:
    """A run's trace, as workflows/job.yaml derives it: a Workflow execution id is a UUID, so
    its 32 hex digits are a valid trace id."""
    return f"projects/{project}/traces/{run_id.replace('-', '').lower()}"


def entry(line: str, trace: str, task: str = "") -> str:
    """`line` as one Cloud Run structured log line carrying the trace."""
    try:
        payload = json.loads(line)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {"message": line}
    payload.setdefault(TRACE_FIELD, trace)
    if task:
        payload[LABELS_FIELD] = {**(payload.get(LABELS_FIELD) or {}), TASK_LABEL: task}
    return json.dumps(payload, ensure_ascii=False)


class TracedStream(io.TextIOBase):
    """A text stream that writes each complete line to `out` as entry(line)."""

    def __init__(self, out, trace: str, task: str = ""):
        self.out, self.trace, self.task, self.buf = out, trace, task, ""
        self.lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        with self.lock:
            self.buf += s
            *lines, self.buf = self.buf.split("\n")
            for line in lines:
                self.out.write(entry(line, self.trace, self.task) + "\n")
            if lines:
                self.out.flush()
        return len(s)

    def flush(self) -> None:
        with self.lock:
            if self.buf:
                self.out.write(entry(self.buf, self.trace, self.task) + "\n")
                self.buf = ""
            self.out.flush()


def from_env() -> tuple[str, str] | None:
    """(trace, task) this container runs under, or None outside a traced run."""
    trace = os.environ.get(TRACE_ENV)
    return (trace, os.environ.get(TASK_ENV, "")) if trace else None


def install() -> bool:
    """Trace this process's own stdout and stderr; False (untouched) outside a traced run."""
    ctx = from_env()
    if not ctx:
        return False
    sys.stdout = TracedStream(sys.__stdout__, *ctx)
    sys.stderr = TracedStream(sys.__stderr__, *ctx)
    return True


def traced_run(argv, cwd=None, env=None, check=False) -> subprocess.CompletedProcess:
    """subprocess.run, with the child's stdout and stderr written line by line to this
    process's sys.stdout and sys.stderr (traced by install())."""
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace", bufsize=1)

    def pump(src, dst):
        for line in src:
            dst.write(line)
        dst.flush()

    threads = [threading.Thread(target=pump, args=(p.stdout, sys.stdout), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, sys.stderr), daemon=True)]
    for t in threads:
        t.start()
    code = p.wait()
    for t in threads:
        t.join()
    if check and code:
        raise subprocess.CalledProcessError(code, argv)
    return subprocess.CompletedProcess(argv, code)
