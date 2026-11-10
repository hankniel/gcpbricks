"""A failed run's email (gcpbricks.notify): what it says, whom the bundle sends it to,
and that Cloud Monitoring stops emailing the same failure."""
from __future__ import annotations

from pathlib import Path

import yaml

from gcpbricks import bundle as B
from gcpbricks import notify as N

JOB_YAML = yaml.safe_load((Path(__file__).resolve().parent.parent / "terraform" / "modules" / "bundle"
                           / "workflows" / "job.yaml").read_text())
RUN = {"id": "e1", "state": "FAILED", "repair": False, "start": "2026-10-16T03:11:00Z", "end": "2026-10-16T03:16:29Z",
       "params": {"from_date": "2026-10-01"}, "error_message": "ua_features: task(s) failed: [\"ua_features\"]",
       "task_runs": {
           "ua_features": {"key": "ua_features", "status": "FAILED", "start": "2026-10-16T03:11:02Z",
                           "end": "2026-10-16T03:16:29Z", "error": "dbt build: 1 error <model x>"},
           "after": {"key": "after", "status": "UPSTREAM_FAILED", "start": None, "end": None, "error": ""}}}
BASE = {"bundle": {"name": "demo", "mlops_version": "0.4.0"},
        "gcp": {"project": "p", "bucket": "b"},
        "run_as": {"service_principal_name": "sa@p.iam.gserviceaccount.com"},
        "targets": {"dev": {"default": True}}}
JOB = {"email_notifications": {"on_failure": ["a@x.com", "b@x.com"]},
       "tasks": [{"task_key": "t", "condition_task": {"op": "EQUAL_TO", "left": "1", "right": "1"}}]}


def _loaded(gcp=None, job=JOB) -> B.Loaded:
    raw = {**BASE, "gcp": {**BASE["gcp"], **(gcp or {})}, "resources": {"jobs": {"nightly": job}}}
    return B.Loaded(bundle=B.Bundle.model_validate(raw), target="dev", root=Path("."))


def test_the_email_says_what_failed_and_links_to_the_run():
    subject, text, body = N.render(RUN, job="nightly", target="dev", url="https://console.x/jobs/w/runs/e1")
    assert subject == "[gcpbricks] Job run failed: nightly (dev)"
    assert "nightly run failed" in body and 'href="https://console.x/jobs/w/runs/e1"' in body and ">View run<" in body
    assert "1 of 2 tasks failed" in body and "Upstream failed" in body and "5m 29s" in body
    assert "&lt;model x&gt;" in body and "<model x>" not in body  # escaped
    assert "Oct 16, 2026, 10:11 AM GMT+7" in body and "from_date" in body
    assert "View run: https://console.x/jobs/w/runs/e1" in text
    _, text, _ = N.render({**RUN, "id": "r2", "run_id": "e1", "repair_id": 2}, job="nightly", target="dev", url="u")
    assert "Job run ID: e1" in text and "Launched: Repair 2" in text


def test_without_a_console_the_run_links_to_the_workflows_console():
    assert N.run_url("", "p", "r", "w", "e1").startswith("https://console.cloud.google.com/workflows/workflow/r/w/execution/e1")


def test_by_default_the_platform_sends_the_failure_mail():
    lb = _loaded()
    plan = B.render(lb)
    assert B.job_definition(lb, "nightly")["notify"] == {"run_job": "mlops-notify", "to": ["a@x.com", "b@x.com"]}
    alerts = {a["key"]: a for a in plan["alerts"]}
    assert alerts["nightly:on_failure"]["emails"] == []  # Monitoring no longer mails the failure
    assert alerts["nightly:on_skipped"]["emails"] == ["a@x.com", "b@x.com"]


def test_with_mail_off_cloud_monitoring_mails_as_before():
    lb = _loaded({"mail": False})
    plan = B.render(lb)
    assert B.job_definition(lb, "nightly")["notify"] == {}
    alerts = {a["key"]: a for a in plan["alerts"]}
    assert alerts["nightly:on_failure"]["emails"] == ["a@x.com", "b@x.com"] and "nightly:on_skipped" not in alerts


def test_a_job_that_names_no_one_mails_the_default_receiver():
    lb = _loaded(job={"tasks": JOB["tasks"]})
    assert B.job_definition(lb, "nightly")["notify"]["to"] == B.DEFAULT_FAILURE_EMAILS == ["admin@example.com"]
    assert "nightly:on_skipped" not in {a["key"] for a in B.render(lb)["alerts"]}


def test_a_failed_run_starts_the_mail_without_waiting_or_failing_for_it():
    steps = {k: v for step in JOB_YAML["main"]["steps"] for k, v in step.items()}
    names = list(steps)
    assert names.index("log_end") < names.index("mail_failure") < names.index("fail_if_any_failed")
    start = steps["mail_failure"]["switch"][0]["steps"][0]["start_mail"]
    assert start["try"]["args"]["connector_params"] == {"skip_polling": True} and "except" in start
    assert "gcpbricks.notify" in start["try"]["args"]["body"]["overrides"]["containerOverrides"][0]["args"]


def test_the_mail_goes_out_over_the_ci_variables_smtp(monkeypatch):
    sent = {}

    class Smtp:
        def __init__(self, host, port, timeout):
            sent["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["to"], sent["subject"] = msg["To"], msg["Subject"]

    monkeypatch.setattr(N.smtplib, "SMTP", Smtp)
    monkeypatch.setenv("SMTP_USER", "bot@x.com")
    monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    N.send(["a@x.com", "b@x.com"], "s", "t", "<p>b</p>")
    assert sent == {"server": ("smtp.gmail.com", 587), "tls": True, "login": ("bot@x.com", "abcdefghijklmnop"),
                    "to": "a@x.com, b@x.com", "subject": "s"}
