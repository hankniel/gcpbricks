"""Quality monitors created in the console (Databricks' Create monitor): its own bundle,
`console-monitors`, per target, deployed by the console as any bundle deploys.

The bundle's settings (project, region, bucket, mode) and the account everything runs as are
those of the bundle MLOPS_MONITORS_SETTINGS_FROM names, on that target, read from its
deployment record. Unset, the console creates no monitors (a bundle's own still show). The monitors themselves live in
console-monitors' own deployment record (each monitor's `config`), so a create, update or delete
reads them there, changes one, and deploys the whole bundle again.

The deploy runs `mlops bundle deploy` in a child process as whoever acts, as the console's
other writes do: a user with their own GCP access token (their roles decide, and audit logs
name them), an admin as the console's own account. The refresh jobs run as that bundle's
run_as account either way."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from gcpbricks.console import bundles as BN

BUNDLE = "console-monitors"
#: Whose settings and account console-monitors takes, per target (a deployed bundle's name).
SOURCE = os.environ.get("MLOPS_MONITORS_SETTINGS_FROM", "")
#: How long a deploy may take: terraform applying a few Workflows and Cloud Run jobs.
TIMEOUT = 900


def _settings_from(record: dict) -> dict:
    return {k: record.get(k) for k in ("project", "region", "bucket", "run_as", "mode")}


def _settings(gcp, target: str) -> dict:
    if not SOURCE:
        raise ValueError("console monitors are off: set MLOPS_MONITORS_SETTINGS_FROM to the bundle whose "
                         "settings and run_as account they deploy with")
    d = BN.deployment(gcp, SOURCE, target)
    if not d or not d.get("run_as"):
        raise ValueError(f"{SOURCE} has no deployment record on {target}: console-monitors takes its settings from it")
    return _settings_from(d)


def targets(gcp) -> list[str]:
    """The targets a console monitor can be created on: those SOURCE is deployed to."""
    if not SOURCE:
        return []
    return sorted(d["target"] for d in BN.deployments(gcp) if d["bundle"] == SOURCE and d.get("run_as"))


def monitors(gcp, target: str) -> dict[str, dict]:
    """console-monitors' monitors on the target, as declared (each one's recorded config)."""
    d = BN.deployment(gcp, BUNDLE, target) or {}
    out = {}
    for k, m in ((d.get("resources") or {}).get("quality_monitors") or {}).items():
        if not m.get("config"):
            raise ValueError(f"console-monitors' {k} on {target} has no recorded config: redeploy it first")
        out[k] = m["config"]
    return out


def bundle_yaml(settings: dict, target: str, quality_monitors: dict[str, dict]) -> str:
    """console-monitors' mlops.yaml: one target, SOURCE's settings and account."""
    doc = {"bundle": {"name": BUNDLE, "mlops_version": "latest"},
           "gcp": {"project": settings["project"], "region": settings["region"], "bucket": settings["bucket"]},
           "run_as": {"service_principal_name": settings["run_as"]},
           "targets": {target: {"default": True, **({"mode": settings["mode"]} if settings.get("mode") else {})}},
           "resources": {"quality_monitors": quality_monitors}}
    return yaml.safe_dump(doc, sort_keys=False, width=110)


def _deploy(gcp, user: str, target: str, quality_monitors: dict[str, dict], token: str | None = None) -> dict:
    from gcpbricks.console import service as S

    settings = _settings(gcp, target)
    root = Path(tempfile.mkdtemp(prefix="console-monitors-"))
    (root / "mlops.yaml").write_text(bundle_yaml(settings, target, quality_monitors))
    # deploy lists the bundle's files with git.
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=30)
    version = "console-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    env = {**os.environ, "GITLAB_USER_EMAIL": user}
    env.pop("MLOPS_DEPLOY_TOKEN", None)
    if token:
        env["MLOPS_DEPLOY_TOKEN"] = token
    done = subprocess.run([sys.executable, "-m", "gcpbricks.console.monitor_deploy", "-t", target,
                           "--version", version], cwd=root, env=env, capture_output=True, text=True,
                          timeout=TIMEOUT, check=False)
    log = (done.stdout + done.stderr).strip()
    S._audit(gcp, user, "deploy_monitors", BUNDLE, target=target, monitors=sorted(quality_monitors),
             version=version, ok=done.returncode == 0)
    if done.returncode:
        raise ValueError(f"deploying console-monitors to {target} failed:\n{log[-3000:]}")
    return {"bundle": BUNDLE, "target": target, "version": version, "log": log[-3000:]}


def upsert(gcp, user: str, target: str, key: str, spec: dict, token: str | None = None) -> dict:
    """Creates or updates monitor `key` in console-monitors on the target."""
    from gcpbricks.console.quality import monitor_yaml

    monitor_yaml(key, spec)  # validates, as deploy would
    taken = [d["bundle"] for d in BN.deployments(gcp) if d["target"] == target and d["bundle"] != BUNDLE
             and key in ((d.get("resources") or {}).get("quality_monitors") or {})]
    current = monitors(gcp, target)
    on_table = [k for k, m in current.items() if m.get("table_name") == spec.get("table_name") and k != key]
    if on_table:
        raise ValueError(f"{spec.get('table_name')} already has console monitor {on_table[0]} on {target}")
    out = _deploy(gcp, user, target, {**current, key: spec}, token)
    return {**out, **({"note": f"bundle(s) {taken} also declare a monitor {key}"} if taken else {})}


def remove(gcp, user: str, target: str, key: str, token: str | None = None) -> dict:
    """Deletes monitor `key` from console-monitors: its refresh job is retired (no schedule),
    as any job a bundle stops declaring; its metric tables stay."""
    current = monitors(gcp, target)
    if key not in current:
        raise KeyError(f"no console monitor {key!r} on {target}")
    return _deploy(gcp, user, target, {k: v for k, v in current.items() if k != key}, token)


def _as_token(token: str) -> None:
    """Every Google client in this process, and terraform, act with the user's access token."""
    import google.auth
    from google.oauth2.credentials import Credentials

    creds = Credentials(token)
    _, project = google.auth.default()
    google.auth.default = lambda *a, **k: (creds, project)
    os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = token


if __name__ == "__main__":
    from gcpbricks import cli

    if os.environ.get("MLOPS_DEPLOY_TOKEN"):
        _as_token(os.environ.pop("MLOPS_DEPLOY_TOKEN"))
    sys.exit(cli.main(["bundle", "deploy", *sys.argv[1:]]))
