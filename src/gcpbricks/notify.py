"""A failed job run's email, as Databricks sends one: the run's status, details, error and
tasks, and a View run button into the console. Run by the project's mlops-notify Cloud Run job
(deploy/console/dev, on the console's image), which workflows/job.yaml starts when a run fails
(unless the bundle's gcp.mail is false):

    python -m gcpbricks.notify <workflow> <execution id> <job> <target> '["a@x.com"]'

env: MLOPS_PROJECT, MLOPS_REGION (where the run is), MLOPS_CONSOLE_URL ("" links to the
Workflows console instead), and SMTP_HOST (default smtp.gmail.com), SMTP_PORT (587), SMTP_USER,
SMTP_PASSWORD (a Gmail app password), SMTP_FROM (default SMTP_USER): this repo's GitLab CI
variables, as dbt-features's notify uses.
"""
from __future__ import annotations

import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

#: The console's zone: every time it shows is GMT+7.
ZONE, ZONE_LABEL = ZoneInfo("Asia/Ho_Chi_Minh"), "GMT+7"
STATE = {"SUCCESS": ("Succeeded", "#277c43"), "SUCCEEDED": ("Succeeded", "#277c43"), "FAILED": ("Failed", "#c82d4c"),
         "UPSTREAM_FAILED": ("Upstream failed", "#be501e"), "EXCLUDED": ("Not run", "#5f7281"),
         "REUSED": ("Reused", "#5f7281"), "CANCELLED": ("Cancelled", "#5f7281"), "RUNNING": ("Running", "#2272b4")}


def _parse(iso: str) -> datetime:
    """GCP's RFC 3339 time, nanoseconds and all (fromisoformat takes six digits at most)."""
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", iso))


def _when(iso: str | None) -> str:
    if not iso:
        return "-"
    t = _parse(iso).astimezone(ZONE)
    return t.strftime("%b %d, %Y, %I:%M %p ") + ZONE_LABEL


def _duration(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "-"
    s = max(0, int((_parse(end) - _parse(start)).total_seconds()))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {s % 3600 // 60}m"


def run_url(console_url: str, project: str, region: str, workflow: str, execution_id: str) -> str:
    if console_url:
        return f"{console_url.rstrip('/')}/jobs/{workflow}/runs/{execution_id}"
    return (f"https://console.cloud.google.com/workflows/workflow/{region}/{workflow}/execution/{execution_id}"
            f"?project={project}")


#: Log lines of the failure's root an email quotes.
WHY_LINES = 15


def _code_text(code: dict) -> str:
    if not code.get("commit"):
        return ""
    return (f"{code.get('branch') or '?'}@{code['commit'][:8]}" + (" (with uncommitted changes)" if code.get("dirty") else "")
            + (f", deployed {_when(code.get('deployed_at'))}" if code.get("deployed_at") else ""))


def render(run: dict, *, job: str, target: str, url: str, why: dict | None = None) -> tuple[str, str, str]:
    """(subject, plain text, HTML) of a failed run's email. `run` is the console's run_detail,
    `why` its failure.why(): where the failure really came from, with its last log lines."""
    e = html.escape
    tasks = sorted(run["task_runs"].values(), key=lambda t: (t.get("start") or "~", t["key"]))
    failed = [t for t in tasks if t.get("status") == "FAILED"]
    error = run.get("error_message") or ""
    subject = f"[gcpbricks] Job run failed: {job} ({target})"
    repair_id = run.get("repair_id") or 0
    rows = [("Job", job), ("Target", target), ("Job run ID", run.get("run_id") or run["id"]),
            ("Launched", f"Repair {repair_id}" if repair_id else "Repair run" if run.get("repair") else "Run"), ("Started", _when(run.get("start"))),
            ("Ended", _when(run.get("end"))), ("Duration", _duration(run.get("start"), run.get("end")))]
    params = run.get("params") or {}
    lead = (f"{len(failed)} of {len(tasks)} task{'s' if len(tasks) != 1 else ''} failed"
            if tasks else "The run failed before its tasks ran")

    text = "\n".join([f"Job run failed: {job} ({target})", lead + ".", "",
                      *[f"{k}: {v}" for k, v in rows], *[f"Parameter {k}: {v}" for k, v in params.items()],
                      *(["", f"Error: {error}"] if error else []), "", "Tasks:",
                      *[f"  {STATE.get(t.get('status') or '', (t.get('status') or '-', ''))[0]:<16} {t['key']}"
                        f"  {_duration(t.get('start'), t.get('end'))}" + (f"  {t['error']}" if t.get("error") else "")
                        for t in tasks],
                      *(["", "Why it failed: " + " > ".join(p["label"] for p in why["path"]),
                         *([f"  {why['error']}"] if why.get("error") else []),
                         *[f"  | {line['text']}" for line in why["logs"][-WHY_LINES:]],
                         *([f"  Code: {_code_text(why['code'])}"] if _code_text(why.get("code") or {}) else [])]
                        if why and why.get("path") else []),
                      "", f"View run: {url}"])

    def badge(status: str | None) -> str:
        name, color = STATE.get(status or "", (status or "-", "#5f7281"))
        return (f'<span style="color:{color};font-weight:600;white-space:nowrap">'
                f'<span style="font-size:10px;vertical-align:1px">&#9679;</span>&nbsp;{e(name)}</span>')

    detail = "".join(
        f'<tr><td style="padding:6px 0;color:#5f7281;width:140px;vertical-align:top">{e(k)}</td>'
        f'<td style="padding:6px 0;color:#11171c;word-break:break-word">{e(str(v))}</td></tr>' for k, v in rows)
    detail += "".join(
        f'<tr><td style="padding:6px 0;color:#5f7281;vertical-align:top">{e(k)}</td><td style="padding:6px 0;'
        f'font-family:Menlo,Consolas,monospace;font-size:12px;color:#11171c;word-break:break-word">{e(v)}</td></tr>'
        for k, v in params.items())
    task_rows = "".join(
        f'<tr><td style="padding:8px 12px;border-top:1px solid #e8ecf0;vertical-align:top">{badge(t.get("status"))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e8ecf0;vertical-align:top;color:#11171c;font-weight:600">{e(t["key"])}'
        + (f'<div style="margin-top:4px;font-weight:400;color:#c82d4c;font-family:Menlo,Consolas,monospace;font-size:12px;'
           f'white-space:pre-wrap;word-break:break-word">{e(t["error"][:600])}</div>' if t.get("error") else "")
        + f'</td><td style="padding:8px 12px;border-top:1px solid #e8ecf0;vertical-align:top;color:#5f7281;text-align:right;'
          f'white-space:nowrap">{_duration(t.get("start"), t.get("end"))}</td></tr>' for t in tasks)
    error_box = (f'<div style="margin:0 0 20px;padding:12px 16px;background:#fff5f7;border:1px solid #f8d4dc;border-radius:4px;'
                 f'color:#11171c;font-family:Menlo,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word">'
                 f'{e(error[:2000])}</div>' if error else "")
    why_box = ""
    if why and why.get("path"):
        lines = "\n".join(line["text"] for line in why["logs"][-WHY_LINES:])
        code = _code_text(why.get("code") or {})
        why_box = (
            '<div style="font-weight:600;color:#11171c;margin-bottom:4px">Why it failed</div>'
            f'<div style="color:#5f7281;font-size:12px;margin-bottom:8px">{e(" > ".join(p["label"] for p in why["path"]))}</div>'
            + (f'<div style="color:#c82d4c;font-family:Menlo,Consolas,monospace;font-size:12px;white-space:pre-wrap;'
               f'word-break:break-word;margin-bottom:8px">{e(why["error"][:1500])}</div>' if why.get("error") else "")
            + (f'<div style="margin:0 0 8px;padding:10px 12px;background:#11171c;color:#e8ecf0;border-radius:4px;'
               f'font-family:Menlo,Consolas,monospace;font-size:11px;line-height:16px;white-space:pre-wrap;'
               f'word-break:break-word">{e(lines[-4000:])}</div>' if lines else "")
            + (f'<div style="color:#5f7281;font-size:12px;margin-bottom:20px">Code: {e(code)}</div>' if code
               else '<div style="margin-bottom:20px"></div>'))
    body = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f6f7f9">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f7f9;padding:24px 0">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;
 border:1px solid #e8ecf0;border-radius:8px;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:20px">
<tr><td style="padding:20px 32px;border-bottom:1px solid #e8ecf0">
 <span style="display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;background:#2272b4;color:#fff;
  border-radius:4px;font-weight:700;font-size:13px">b</span>
 <span style="margin-left:8px;font-weight:600;color:#11171c;vertical-align:middle">gcpbricks</span>
</td></tr>
<tr><td style="padding:28px 32px 8px">
 <div style="margin-bottom:8px">{badge("FAILED")}</div>
 <div style="font-size:22px;line-height:28px;font-weight:600;color:#11171c;word-break:break-word">{e(job)} run failed</div>
 <div style="margin-top:6px;color:#5f7281">{e(lead)} on {e(target)}.</div>
 <div style="margin:20px 0 24px"><a href="{e(url)}" style="display:inline-block;background:#2272b4;color:#ffffff;
  text-decoration:none;font-weight:600;padding:8px 16px;border-radius:4px">View run</a></div>
 {error_box}
 {why_box}
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px">{detail}</table>
 <div style="font-weight:600;color:#11171c;margin-bottom:8px">Tasks</div>
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e8ecf0;border-radius:4px;
  border-collapse:separate;margin-bottom:24px">
  <tr style="background:#f6f7f9"><td style="padding:8px 12px;color:#5f7281;font-size:12px">Status</td>
  <td style="padding:8px 12px;color:#5f7281;font-size:12px">Task</td>
  <td style="padding:8px 12px;color:#5f7281;font-size:12px;text-align:right">Duration</td></tr>
  {task_rows or '<tr><td colspan="3" style="padding:8px 12px;color:#5f7281;border-top:1px solid #e8ecf0">No task ran.</td></tr>'}
 </table>
</td></tr>
<tr><td style="padding:16px 32px;border-top:1px solid #e8ecf0;color:#5f7281;font-size:12px;line-height:18px">
 You're receiving this because you're notified of {e(job)}'s failed runs. Times are {ZONE_LABEL}.
</td></tr>
</table></td></tr></table></body></html>"""
    return subject, text, body


def send(to: list[str], subject: str, text: str, body: str) -> None:
    user = os.environ["SMTP_USER"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("SMTP_FROM") or user, ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    host, port = os.environ.get("SMTP_HOST") or "smtp.gmail.com", int(os.environ.get("SMTP_PORT") or 587)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        # A Gmail app password reads in groups of four; spaces pasted with it don't belong.
        s.login(user, os.environ["SMTP_PASSWORD"].replace(" ", ""))
        s.send_message(msg)


def main(argv: list[str] | None = None) -> int:
    workflow, execution_id, job, target, to = (argv or sys.argv[1:])[:5]
    from gcpbricks.console import gcp as G
    from gcpbricks.console import service as S

    project, region = os.environ["MLOPS_PROJECT"], os.environ["MLOPS_REGION"]
    run = S.run_detail(G.Gcp(project, region), workflow, execution_id)
    url = run_url(os.environ.get("MLOPS_CONSOLE_URL", ""), project, region, workflow, execution_id)
    if run.get("repair_id") and os.environ.get("MLOPS_CONSOLE_URL"):
        url = run_url(os.environ["MLOPS_CONSOLE_URL"], project, region, workflow, run["run_id"]) + f"?repair_id={run['repair_id']}"
    try:
        from gcpbricks.console import failure as F
        why = F.why(G.Gcp(project, region), workflow, execution_id)
    except Exception as e:  # noqa: BLE001 - the email goes out without its root cause rather than not at all
        print(f"couldn't trace the failure: {e}")
        why = None
    subject, text, body = render(run, job=job, target=target, url=url, why=why)
    send(json.loads(to), subject, text, body)
    print(f"sent {subject!r} to {to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
