"""The console's notebooks, on Colab Enterprise: the workspace notebooks live in (GCS), the
Colab runtimes they run on, and a relay that runs a notebook's cells on its user's runtime.

    gs://<bucket>/workspace/Users/<email>/...    a user's own notebooks (a notebook_task's
                       /Shared/...               /Workspace/... path names the same objects)

A Colab runtime belongs to one user and can't belong to a service account, so the relay runs
cells with the user's own Google access token (the console's "Allow notebook access"): it
calls the runtime's Jupyter server through Colab's proxy, which accepts the owner's bearer
token together with the tunnel cookie the proxy sets. The proxy refuses to be framed, so
the console draws the editor itself.
"""
from __future__ import annotations

import ast
import json
import os
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import requests

#: Where workspace notebooks live: bundle.WORKSPACE_PREFIX in the bundles' bucket.
WORKSPACE = os.environ.get("MLOPS_WORKSPACE", "gs://my-ml-bucket/workspace")
NOTEBOOK_TYPE = "application/x-ipynb+json"
EMPTY_NOTEBOOK = {"nbformat": 4, "nbformat_minor": 5,
                  "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}},
                  "cells": [{"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": ""}]}
_PATH = re.compile(r"^(Users/[^/]+|Shared)(/[^/]+)*/?$")


def _bucket_prefix() -> tuple[str, str]:
    bucket, _, prefix = WORKSPACE.removeprefix("gs://").partition("/")
    return bucket, prefix.rstrip("/") + "/"


def _clean(path: str) -> str:
    path = path.strip("/")
    if ".." in path.split("/") or not _PATH.match(path or "x"):
        raise ValueError(f"a workspace path is under Users/<email>/ or Shared/, not {path!r}")
    return path


def may_write(user: str, path: str, admin: bool) -> bool:
    """A user writes their own folder and Shared/; an admin writes anywhere."""
    return admin or path in ("Shared", f"Users/{user}") or path.startswith(("Shared/", f"Users/{user}/"))


def _guard(user: str, path: str, admin: bool) -> None:
    if not may_write(user, path, admin):
        raise PermissionError(f"{user} writes Users/{user}/ and Shared/, not {path}")


# --- sharing --------------------------------------------------------------------------------
#
# A notebook in Users/<owner>/ is everyone's to view and its owner's to edit, unless its owner
# (or an admin) shares it: general access "view" (everyone) or "private" (only the people
# added), and people added as "view" or "edit". The settings sit beside the workspace at
# .shares/<path>.json, a prefix no workspace path can name. Shared/ is everyone's to edit.

DEFAULT_SHARE = {"general": "view", "people": {}}
_EMAIL = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")


def _owner(path: str) -> str | None:
    return path.split("/")[1] if path.startswith("Users/") else None


def _share_object(base: str, path: str) -> str:
    return f"{base}.shares/{path}.json"


def read_share(gcp, path: str) -> dict:
    bucket, base = _bucket_prefix()
    got = gcp.read_object(bucket, _share_object(base, path))
    return {**DEFAULT_SHARE, **json.loads(got[0])} if got else {**DEFAULT_SHARE, "people": {}}


def access(gcp, user: str, path: str, admin: bool = False, share: dict | None = None) -> str:
    """What `user` may do with the notebook at `path`: "manage" (its owner, an admin),
    "edit", "view" or "none"."""
    owner = _owner(path)
    if admin or owner == user:
        return "manage"
    if owner is None:
        return "edit"
    share = share if share is not None else read_share(gcp, path)
    return share["people"].get(user.lower()) or ("view" if share["general"] == "view" else "none")


_LEVELS = ["none", "view", "edit", "manage"]


def _can(gcp, user: str, path: str, admin: bool, need: str) -> str:
    got = access(gcp, user, path, admin)
    if _LEVELS.index(got) < _LEVELS.index(need):
        raise PermissionError(f"{path} isn't shared with {user}" if got == "none"
                              else f"{user} may only {got} {path}: ask its owner to share it with you")
    return got


def get_share(gcp, user: str, path: str, admin: bool = False) -> dict:
    path = _clean(path)
    _can(gcp, user, path, admin, "manage")
    return {"path": path, "owner": _owner(path), **read_share(gcp, path)}


def set_share(gcp, user: str, path: str, general: str, people: dict[str, str], admin: bool = False) -> dict:
    """Its owner or an admin shares a notebook in a Users/ folder (Shared/ is everyone's already)."""
    bucket, base = _bucket_prefix()
    path = _clean(path)
    owner = _owner(path)
    if owner is None or not path.endswith(".ipynb"):
        raise ValueError(f"only a notebook in a Users/ folder is shared, not {path}")
    _can(gcp, user, path, admin, "manage")
    if gcp.object_generation(bucket, base + path) is None:
        raise KeyError(f"no notebook {path}")
    if general not in ("view", "private"):
        raise ValueError(f"general access is view or private, not {general!r}")
    people = {e.strip().lower(): lvl for e, lvl in people.items() if e.strip().lower() != owner}
    for email, level in people.items():
        if not _EMAIL.match(email):
            raise ValueError(f"{email!r} isn't an email address")
        if level not in ("view", "edit"):
            raise ValueError(f"{email} may view or edit, not {level!r}")
    share = {"general": general, "people": dict(sorted(people.items()))}
    gcp.write_object(bucket, _share_object(base, path), json.dumps(share).encode(), "application/json")
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "share_notebook", "path": path, **share})
    return {"path": path, "owner": owner, **share}


# --- the workspace --------------------------------------------------------------------------

def list_folder(gcp, user: str, path: str = "", admin: bool = False) -> dict:
    """A folder's sub-folders and the notebooks `user` may view. The root lists Users/ (yours
    first) and Shared/."""
    bucket, base = _bucket_prefix()
    if not path.strip("/"):
        return {"path": "", "folders": [{"name": "Shared", "path": "Shared"}, {"name": "Users", "path": "Users"}],
                "notebooks": [], "home": f"Users/{user}"}
    path = path.strip("/")
    if path != "Users":
        path = _clean(path)
    prefixes, items = gcp.list_dir(bucket, f"{base}{path}/")
    folders = [{"name": p.rstrip("/").rsplit("/", 1)[-1], "path": p[len(base):].rstrip("/")} for p in prefixes]
    if path == "Users" and not any(f["name"] == user for f in folders):
        folders.append({"name": user, "path": f"Users/{user}"})
    folders.sort(key=lambda f: (f["name"] != user, f["name"]))
    notebooks = [{"name": i["name"].rsplit("/", 1)[-1], "path": i["name"][len(base):], "updated": i.get("updated"),
                  "size": int(i.get("size", 0)), "generation": i.get("generation")}
                 for i in items if i["name"].endswith(".ipynb")]
    owner = _owner(path)
    if owner and owner != user and not admin:
        # Only shared notebooks have settings, so one listing finds the few to read.
        _, shared = gcp.list_dir(bucket, f"{base}.shares/{path}/")
        settings = [s["name"].removeprefix(f"{base}.shares/").removesuffix(".json") for s in shared]
        shares = {p: read_share(gcp, p) for p in settings}
        notebooks = [n for n in notebooks if access(gcp, user, n["path"], share=shares.get(n["path"], DEFAULT_SHARE)) != "none"]
    return {"path": path, "folders": folders, "notebooks": sorted(notebooks, key=lambda n: n["name"]),
            "home": f"Users/{user}"}


def read_notebook(gcp, user: str, path: str, admin: bool = False) -> dict:
    """A notebook and what `user` may do with it (`access`)."""
    bucket, base = _bucket_prefix()
    path = _clean(path)
    can = _can(gcp, user, path, admin, "view")
    got = gcp.read_object(bucket, base + path)
    if got is None:
        raise KeyError(f"no notebook {path}")
    data, generation = got
    return {"path": path, "generation": generation, "notebook": json.loads(data), "access": can}


def notebook_generation(gcp, user: str, path: str, admin: bool = False) -> dict:
    """Only the generation: what an open editor polls to see a save made elsewhere (another
    tab, Claude through mlops-notebooks-mcp)."""
    bucket, base = _bucket_prefix()
    path = _clean(path)
    _can(gcp, user, path, admin, "view")
    return {"path": path, "generation": gcp.object_generation(bucket, base + path)}


def new_cell_id() -> str:
    return uuid.uuid4().hex[:12]


def ensure_cell_ids(notebook: dict) -> bool:
    """nbformat 4.5's cell ids, given to the cells without one: a run record names its cells by
    id, which stays put when cells are inserted or moved. True when one was added."""
    seen, added = set(), False
    for c in notebook.get("cells", []):
        if not c.get("id") or c["id"] in seen:
            c["id"], added = new_cell_id(), True
        seen.add(c["id"])
    if added and (notebook.get("nbformat"), notebook.get("nbformat_minor", 0)) < (4, 5):
        notebook["nbformat"], notebook["nbformat_minor"] = 4, 5
    return added


def save_notebook(gcp, user: str, path: str, notebook: dict, generation: str | None, admin: bool = False) -> dict:
    """Saves over the generation the editor read ("0" for a new one), so a save never
    overwrites someone else's newer one; that conflict raises ValueError. A cell without an
    id gets one."""
    bucket, base = _bucket_prefix()
    path = _clean(path)
    if not path.endswith(".ipynb"):
        raise ValueError(f"a notebook's path ends in .ipynb, not {path}")
    _can(gcp, user, path, admin, "edit")
    ensure_cell_ids(notebook)
    try:
        new = gcp.write_object(bucket, base + path, json.dumps(notebook, indent=1).encode(), NOTEBOOK_TYPE,
                               generation)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 412:
            raise ValueError(f"{path} changed since you opened it: reload it before saving") from None
        raise
    return {"path": path, "generation": new}


def create_notebook(gcp, user: str, path: str, admin: bool = False) -> dict:
    return save_notebook(gcp, user, path if path.endswith(".ipynb") else f"{path}.ipynb",
                         json.loads(json.dumps(EMPTY_NOTEBOOK)), "0", admin)


def create_folder(gcp, user: str, path: str, admin: bool = False) -> dict:
    """A folder is a prefix: it holds a placeholder until something is saved in it."""
    bucket, base = _bucket_prefix()
    path = _clean(path)
    _guard(user, path, admin)
    gcp.write_object(bucket, f"{base}{path}/.folder", b"", "text/plain", "0")
    return {"path": path}


def delete_notebook(gcp, user: str, path: str, admin: bool = False) -> dict:
    bucket, base = _bucket_prefix()
    path = _clean(path)
    _guard(user, path, admin)
    gcp.delete_object(bucket, base + path)
    gcp.delete_object(bucket, _share_object(base, path))
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "delete_notebook", "path": path})
    return {"deleted": path}


# --- compute: Colab runtime templates and runtimes ------------------------------------------

def _id(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _template(t: dict) -> dict:
    m, sw = t.get("machineSpec") or {}, t.get("softwareConfig") or {}
    script = sw.get("postStartupScriptConfig") or {}
    idle = t.get("idleShutdownConfig") or {}
    return {"id": _id(t["name"]), "name": t.get("displayName", _id(t["name"])),
            "description": t.get("description", ""), "machine": m.get("machineType", ""),
            "accelerator": m.get("acceleratorType", "") if m.get("acceleratorCount") else "",
            "accelerator_count": m.get("acceleratorCount") or 0,
            "disk_gb": (t.get("dataPersistentDiskSpec") or {}).get("diskSizeGb"),
            "disk_type": (t.get("dataPersistentDiskSpec") or {}).get("diskType", ""),
            "idle_shutdown": None if idle.get("idleShutdownDisabled") else idle.get("idleTimeout"),
            "internet": bool((t.get("networkSpec") or {}).get("enableInternetAccess", True)),
            "image": (sw.get("colabImage") or {}).get("releaseName", ""),
            "env": {e["name"]: e.get("value", "") for e in sw.get("env", [])},
            "startup_script": script.get("postStartupScript", ""), "startup_script_url": script.get("postStartupScriptUrl", ""),
            "startup_behavior": script.get("postStartupScriptBehavior", ""),
            "default": bool(t.get("isDefault")) or t.get("notebookRuntimeType") == "ONE_CLICK",
            "labels": t.get("labels") or {}, "created": t.get("createTime"), "updated": t.get("updateTime"),
            "etag": t.get("etag", "")}


def templates(gcp) -> list[dict]:
    out = [_template(t) for t in gcp.ai_list("notebookRuntimeTemplates", "notebookRuntimeTemplates")]
    return sorted(out, key=lambda t: (not t["default"], t["name"]))


#: A template's settings Colab lets change after it's made; the rest (machine, disk, network,
#: idle shutdown) is fixed, so changing those means a new template.
_MUTABLE = {"name": "display_name", "env": "software_config.env", "image": "software_config.colab_image.release_name",
            "startup_script": "software_config.post_startup_script_config.post_startup_script",
            "startup_script_url": "software_config.post_startup_script_config.post_startup_script_url",
            "startup_behavior": "software_config.post_startup_script_config.post_startup_script_behavior"}


def _software(spec: dict) -> dict:
    sw: dict = {}
    if spec.get("env") is not None:
        sw["env"] = [{"name": k, "value": str(v)} for k, v in spec["env"].items()]
    if spec.get("image"):
        sw["colabImage"] = {"releaseName": spec["image"]}
    script = {k2: spec[k] for k, k2 in (("startup_script", "postStartupScript"), ("startup_script_url", "postStartupScriptUrl"),
                                        ("startup_behavior", "postStartupScriptBehavior")) if spec.get(k)}
    if script:
        sw["postStartupScriptConfig"] = script
    return sw


def _admin_only(admin: bool) -> None:
    if not admin:
        raise PermissionError("templates are shared and set what every runtime costs: only the console's admins change them")


def create_template(gcp, user: str, spec: dict, admin: bool = False) -> dict:
    """A runtime template from the console's form: its machine (and GPU), data disk, internet
    access, idle shutdown (minutes; 0: never), Colab image, env and post-startup script."""
    _admin_only(admin)
    if not spec.get("name") or not spec.get("machine"):
        raise ValueError("a template needs a name and a machine type")
    machine = {"machineType": spec["machine"]}
    if spec.get("accelerator") and int(spec.get("accelerator_count") or 0) > 0:
        machine |= {"acceleratorType": spec["accelerator"], "acceleratorCount": int(spec["accelerator_count"])}
    idle = int(spec.get("idle_minutes") if spec.get("idle_minutes") is not None else 180)
    if idle and not 10 <= idle <= 1440:
        raise ValueError("idle shutdown is 10 to 1440 minutes, or 0 for never")
    body = {"displayName": spec["name"], "description": spec.get("description", ""), "machineSpec": machine,
            "dataPersistentDiskSpec": {"diskType": spec.get("disk_type") or "pd-standard",
                                       "diskSizeGb": str(int(spec.get("disk_gb") or 100))},
            "networkSpec": {"enableInternetAccess": bool(spec.get("internet", True))},
            "idleShutdownConfig": ({"idleTimeout": f"{idle * 60}s"} if idle else {"idleShutdownDisabled": True}),
            "labels": {"mlops_created_by": re.sub(r"[^a-z0-9_-]", "_", user.lower())[:63]}}
    if sw := _software(spec):
        body["softwareConfig"] = sw
    op = gcp.ai_post(f"{gcp.parent}/notebookRuntimeTemplates", body)
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "create_template", "template": spec["name"]})
    return {"name": spec["name"], "operation": op.get("name", "")}


def update_template(gcp, user: str, template_id: str, spec: dict, admin: bool = False) -> dict:
    """Changes what Colab lets change: the name, env, Colab image and post-startup script."""
    _admin_only(admin)
    fixed = sorted(set(spec) - set(_MUTABLE) - {"etag"})
    if fixed:
        raise ValueError(f"a template's {fixed} are fixed once it's made: duplicate it with the new values instead")
    if not spec.keys() - {"etag"}:
        raise ValueError("nothing to change")
    body: dict = {"etag": spec["etag"]} if spec.get("etag") else {}
    if "name" in spec:
        body["displayName"] = spec["name"]
    if sw := _software({k: v for k, v in spec.items() if k != "name"}):
        body["softwareConfig"] = sw
    mask = ",".join(_MUTABLE[k] for k in spec if k in _MUTABLE)
    gcp.ai_patch(f"notebookRuntimeTemplates/{template_id}", body, mask)
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "update_template", "template": template_id,
               "fields": sorted(set(spec) - {"etag"})})
    return {"template": template_id, "updated": sorted(set(spec) - {"etag"})}


def delete_template(gcp, user: str, template_id: str, admin: bool = False) -> dict:
    """Deletes a template; runtimes made from it keep running."""
    _admin_only(admin)
    gcp.ai_delete(f"notebookRuntimeTemplates/{template_id}")
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "delete_template", "template": template_id})
    return {"deleted": template_id}


def _idle(cfg: dict | None) -> str | None:
    """A runtime's idle shutdown as words ("30 min"), None when it never idles out."""
    cfg = cfg or {}
    if cfg.get("idleShutdownDisabled") or not cfg.get("idleTimeout"):
        return None
    minutes = int(float(cfg["idleTimeout"].rstrip("s"))) // 60
    return f"{minutes // 60} h" if minutes % 60 == 0 and minutes >= 60 else f"{minutes} min"


def _runtime(r: dict) -> dict:
    return {"id": _id(r["name"]), "name": r.get("displayName", _id(r["name"])), "user": r.get("runtimeUser", ""),
            "state": r.get("runtimeState", ""), "health": r.get("healthState", ""), "idle": _idle(r.get("idleShutdownConfig")),
            "template": (r.get("notebookRuntimeTemplateRef") or {}).get("notebookRuntimeTemplate", "").rsplit("/", 1)[-1],
            "machine": ((r.get("machineSpec") or {}).get("machineType", "")), "proxy": r.get("proxyUri", ""),
            "created": r.get("createTime"), "updated": r.get("updateTime"),
            "expires": r.get("expirationTime")}


def runtimes(gcp) -> list[dict]:
    """Every user's runtimes, running ones first: who has what up, and since when."""
    rs = [_runtime(r) for r in gcp.ai_list("notebookRuntimes", "notebookRuntimes")]
    return sorted(rs, key=lambda r: (r["state"] != "RUNNING", r["user"], r["name"]))


def runtime(gcp, runtime_id: str) -> dict:
    return _runtime(gcp.ai_get(f"notebookRuntimes/{runtime_id}"))


def _owned(gcp, user: str, runtime_id: str, admin: bool) -> dict:
    r = runtime(gcp, runtime_id)
    if not admin and r["user"] != user:
        raise PermissionError(f"{r['name']} is {r['user']}'s runtime")
    return r


def create_runtime(gcp, user: str, template_id: str, name: str = "") -> dict:
    """A runtime for `user` from a template, named `name` (default <user>-<UTC time>); Colab starts it."""
    name = name.strip() or f"{user.split('@')[0]}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    if len(name) > 128:
        raise ValueError("a runtime's name is at most 128 characters")
    op = gcp.ai_post(f"{gcp.parent}/notebookRuntimes:assign", {
        "notebookRuntimeTemplate": f"{gcp.parent}/notebookRuntimeTemplates/{template_id}",
        "notebookRuntime": {"displayName": name, "runtimeUser": user}})
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "create_runtime", "template": template_id,
               "runtime": name})
    return {"name": name, "operation": op.get("name", "")}


def set_runtime(gcp, user: str, runtime_id: str, action: str, admin: bool = False) -> dict:
    """start, stop or delete a runtime: its owner's or an admin's."""
    r = _owned(gcp, user, runtime_id, admin)
    if action == "delete":
        gcp.ai_delete(f"notebookRuntimes/{runtime_id}")
    elif action in ("start", "stop"):
        gcp.ai_post(f"{gcp.parent}/notebookRuntimes/{runtime_id}:{action}", {})
    else:
        raise ValueError(f"a runtime starts, stops or is deleted, not {action!r}")
    gcp.audit({"event": "mlops_console_action", "user": user, "action": f"{action}_runtime", "runtime": r["name"]})
    return {"runtime": r["name"], "action": action}


# --- running cells: the user's runtime's Jupyter server, through Colab's proxy ---------------

def credentials_code(token: str) -> str:
    """Code that makes the kernel's Google calls run as the user, with `token`.

    A Colab runtime's kernel has no Google identity of its own here: an EUC runtime's agent
    is refused ("not preauthorized to use managed EUC with offline-access tokens") and an
    EUC-off one has no service account the kernel can use. So every cell is preceded by the
    access token the console already holds for the user: client libraries get it from
    google.auth.default(), and gcloud/bq from CLOUDSDK_AUTH_ACCESS_TOKEN_FILE. It lives about
    an hour and the next cell renews it; a client built earlier keeps the token it was
    built with.

    Python processes the kernel starts (`!python job.py`, subprocess, setsid nohup) inherit
    MLOPS_ACCESS_TOKEN_FILE and a PYTHONPATH that leads with ~/.config/mlops/py, whose
    sitecustomize gives their google.auth.default() the same token, read again from the file
    every few minutes: without it they fall back to the runtime's metadata server, which has
    no identity (HTTP 401). Only the kernel's descendants carry that PYTHONPATH."""
    return (
        "def _mlops_credentials(token):\n"
        "    import os\n"
        "    path = os.path.expanduser('~/.config/mlops/access_token')\n"
        "    os.makedirs(os.path.dirname(path), exist_ok=True)\n"
        "    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:\n"
        "        f.write(token)\n"
        "    os.environ['CLOUDSDK_AUTH_ACCESS_TOKEN_FILE'] = path\n"
        "    os.environ['MLOPS_ACCESS_TOKEN_FILE'] = path\n"
        "    hooks = os.path.expanduser('~/.config/mlops/py')\n"
        "    hook = os.path.join(hooks, 'sitecustomize.py')\n"
        f"    if not os.path.exists(hook) or open(hook).read() != {CHILD_HOOK!r}:\n"
        "        os.makedirs(hooks, exist_ok=True)\n"
        f"        open(hook, 'w').write({CHILD_HOOK!r})\n"
        "    paths = [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p and p != hooks]\n"
        "    os.environ['PYTHONPATH'] = os.pathsep.join([hooks, *paths])\n"
        "    try:\n"
        "        import google.auth\n"
        "        import google.oauth2.credentials\n"
        "    except ImportError:\n"
        "        return\n"
        "    creds = google.oauth2.credentials.Credentials(token)\n"
        "    google.auth.default = lambda *a, **k: (creds, os.environ.get('GOOGLE_CLOUD_PROJECT'))\n"
        f"_mlops_credentials({token!r})\n"
        "del _mlops_credentials\n")


#: The sitecustomize a Python process the kernel starts imports first (its PYTHONPATH leads
#: with it); it then runs the sitecustomize it shadows, if the image has one.
CHILD_HOOK = '''"""Written by the mlops console: a Python process the notebook's kernel started calls Google
as the notebook's user, as the kernel does (gcpbricks.console.notebooks.credentials_code)."""
import importlib.util
import os
import sys

_PATH = os.environ.get("MLOPS_ACCESS_TOKEN_FILE")
_HERE = os.path.dirname(os.path.abspath(__file__))


def _install():
    try:
        import datetime

        import google.auth
        import google.oauth2.credentials
    except ImportError:
        return

    def fresh(*_):
        with open(_PATH) as f:
            return f.read().strip(), datetime.datetime.utcnow() + datetime.timedelta(minutes=5)
    token, expiry = fresh()
    creds = google.oauth2.credentials.Credentials(token, expiry=expiry, refresh_handler=fresh)
    google.auth.default = lambda *a, **k: (creds, os.environ.get("GOOGLE_CLOUD_PROJECT"))


def _next():
    """The sitecustomize this one shadows, run as it would have been."""
    for d in sys.path:
        p = os.path.join(d or ".", "sitecustomize.py")
        if os.path.abspath(d or ".") != _HERE and os.path.isfile(p):
            spec = importlib.util.spec_from_file_location("_shadowed_sitecustomize", p)
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
            return


if _PATH and os.path.exists(_PATH):
    _install()
_next()
'''


class KernelConnectionLost(ConnectionError):
    """The WebSocket to a running cell closed before its reply: what happened to the cell is
    the runtime's state and the kernel's status to tell (runs.explain_loss)."""


class Kernel:
    """The Jupyter server on a runtime, as its owner: REST for kernels, a WebSocket per
    execution. Colab's proxy takes the owner's bearer token with its tunnel cookie."""

    def __init__(self, proxy: str, token: str, session: requests.Session | None = None):
        self.base = f"https://{proxy}"
        self.proxy = proxy
        self.s = session or requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.token = token

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        r = self.s.request(method, f"{self.base}{path}", allow_redirects=False, timeout=60, **kw)
        if r.status_code in (301, 302, 401, 403):
            raise PermissionError("the runtime refused your notebook access: allow it again, or this isn't your runtime")
        r.raise_for_status()
        return r

    def kernels(self) -> list[dict]:
        return self._call("GET", "/api/kernels").json()

    def status(self, kernel_id: str) -> dict | None:
        """The kernel's own word on itself: {execution_state (idle, busy, starting, dead),
        last_activity, connections}; None when it is not on the runtime."""
        r = self.s.get(f"{self.base}/api/kernels/{kernel_id}", allow_redirects=False, timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code in (301, 302, 401, 403):
            raise PermissionError("the runtime refused your notebook access: allow it again, or this isn't your runtime")
        r.raise_for_status()
        k = r.json()
        return {"execution_state": k.get("execution_state"), "last_activity": k.get("last_activity"),
                "connections": k.get("connections")}

    def kernelspecs(self) -> dict:
        return self._call("GET", "/api/kernelspecs").json()

    def start(self, name: str = "python3") -> dict:
        return self._call("POST", "/api/kernels", json={"name": name}).json()

    def interrupt(self, kernel_id: str) -> None:
        self._call("POST", f"/api/kernels/{kernel_id}/interrupt")

    def restart(self, kernel_id: str) -> dict:
        return self._call("POST", f"/api/kernels/{kernel_id}/restart").json()

    def shutdown(self, kernel_id: str) -> None:
        self._call("DELETE", f"/api/kernels/{kernel_id}")

    def execute(self, kernel_id: str, code: str, heartbeat: float = 10) -> Iterator[dict]:
        """Runs `code`, yielding its outputs as nbformat output dicts as they come, then
        {"output_type": "done", "execution_count", "status"}. A cell may be silent for as long
        as it runs: every `heartbeat` seconds without a message yields {"output_type":
        "heartbeat"}, so a runner keeps its run record fresh and a stream stays open. A lost
        connection (the runtime stopping, the proxy dropping it) raises KernelConnectionLost."""
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        self.kernels()  # the proxy sets its tunnel cookie on a REST call first
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.s.cookies)
        msg_id, session = uuid.uuid4().hex, uuid.uuid4().hex
        url = f"wss://{self.proxy}/api/kernels/{kernel_id}/channels?session_id={session}"
        def request(mid: str, source: str, silent: bool) -> str:
            return json.dumps({
                "header": {"msg_id": mid, "msg_type": "execute_request", "username": "", "session": session,
                           "version": "5.3", "date": ""},
                "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
                "content": {"code": source, "silent": silent, "store_history": not silent, "user_expressions": {},
                            "allow_stdin": False, "stop_on_error": not silent}})

        with connect(url, additional_headers={"Authorization": f"Bearer {self.token}", "Cookie": cookie},
                     origin=self.base, open_timeout=60, max_size=None) as ws:
            # Silent, so it leaves no output or history; never stop_on_error, so a failure
            # there can't abort the cell queued behind it.
            ws.send(request(uuid.uuid4().hex, credentials_code(self.token), silent=True))
            ws.send(request(msg_id, code, silent=False))
            count, status, idle, replied = None, "ok", False, False
            while not (idle and replied):
                try:
                    m = json.loads(ws.recv(timeout=heartbeat))
                except TimeoutError:
                    yield {"output_type": "heartbeat"}
                    continue
                except ConnectionClosed as e:
                    raise KernelConnectionLost(
                        f"the connection to the kernel closed mid-run ({e.rcvd.code if e.rcvd else 'no close frame'})"
                    ) from None
                if (m.get("parent_header") or {}).get("msg_id") != msg_id:
                    continue
                kind, c = m.get("msg_type"), m.get("content") or {}
                if kind == "stream":
                    yield {"output_type": "stream", "name": c.get("name", "stdout"), "text": c.get("text", "")}
                elif kind in ("display_data", "update_display_data"):
                    yield {"output_type": "display_data", "data": c.get("data", {}), "metadata": c.get("metadata", {})}
                elif kind == "execute_result":
                    count = c.get("execution_count")
                    yield {"output_type": "execute_result", "data": c.get("data", {}), "metadata": c.get("metadata", {}),
                           "execution_count": count}
                elif kind == "error":
                    yield {"output_type": "error", "ename": c.get("ename", ""), "evalue": c.get("evalue", ""),
                           "traceback": c.get("traceback", [])}
                elif kind == "clear_output":
                    yield {"output_type": "clear_output"}
                elif kind == "execute_input":
                    count = c.get("execution_count", count)
                elif kind == "execute_reply":
                    replied, status = True, c.get("status", "ok")
                    count = c.get("execution_count", count)
                elif kind == "status" and c.get("execution_state") == "idle":
                    idle = True
            yield {"output_type": "done", "execution_count": count, "status": status}


    def _shell(self, kernel_id: str, msg_type: str, content: dict, timeout: float = 30) -> dict:
        """One shell request, its reply's content; nothing runs as a cell, so no credentials go first."""
        from websockets.sync.client import connect

        self.kernels()
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.s.cookies)
        msg_id, session = uuid.uuid4().hex, uuid.uuid4().hex
        url = f"wss://{self.proxy}/api/kernels/{kernel_id}/channels?session_id={session}"
        with connect(url, additional_headers={"Authorization": f"Bearer {self.token}", "Cookie": cookie},
                     origin=self.base, open_timeout=60, max_size=None) as ws:
            ws.send(json.dumps({
                "header": {"msg_id": msg_id, "msg_type": msg_type, "username": "", "session": session,
                           "version": "5.3", "date": ""},
                "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [], "content": content}))
            reply = msg_type.replace("_request", "_reply")
            while True:
                m = json.loads(ws.recv(timeout=timeout))
                if (m.get("parent_header") or {}).get("msg_id") == msg_id and m.get("msg_type") == reply:
                    return m.get("content") or {}

    def complete(self, kernel_id: str, code: str, cursor_pos: int) -> dict:
        """The kernel's completions at `cursor_pos`: {"matches", "cursor_start", "cursor_end"}."""
        c = self._shell(kernel_id, "complete_request", {"code": code, "cursor_pos": cursor_pos})
        return {"matches": c.get("matches", []), "cursor_start": c.get("cursor_start", cursor_pos),
                "cursor_end": c.get("cursor_end", cursor_pos),
                "types": {m["text"]: m.get("type", "") for m in (c.get("metadata") or {}).get("_jupyter_types_experimental", [])
                          if isinstance(m, dict) and "text" in m}}

    def inspect(self, kernel_id: str, code: str, cursor_pos: int) -> dict:
        """What the kernel knows of the name at `cursor_pos`, as Shift+Tab shows it: {"found", "text"}."""
        c = self._shell(kernel_id, "inspect_request", {"code": code, "cursor_pos": cursor_pos, "detail_level": 0})
        return {"found": bool(c.get("found")), "text": (c.get("data") or {}).get("text/plain", "")}

    def variables(self, kernel_id: str) -> list[dict]:
        """The user's globals, read as a silent user expression: no output, no history, no execution count."""
        c = self._shell(kernel_id, "execute_request", {
            "code": "", "silent": True, "store_history": False, "user_expressions": {"vars": VARIABLES},
            "allow_stdin": False, "stop_on_error": False})
        got = (c.get("user_expressions") or {}).get("vars") or {}
        if got.get("status") != "ok":
            raise ValueError(f"the kernel couldn't list its variables: {got.get('ename', '')} {got.get('evalue', '')}".strip())
        return json.loads(ast.literal_eval(got["data"]["text/plain"]))


# One expression, so it evaluates in the user's namespace without running as a cell. reprlib keeps
# each value short and survives a failing __repr__; modules, classes and functions aren't variables.
VARIABLES = (
    "__import__('json').dumps([{'name': n, 'type': type(v).__name__,"
    " 'shape': '' if callable(getattr(v, 'shape', None)) else str(getattr(v, 'shape', '')),"
    " 'value': __import__('reprlib').repr(v)}"
    " for n, v in list(globals().items())"
    " if not n.startswith('_') and n not in ('In', 'Out', 'exit', 'quit', 'get_ipython')"
    " and not callable(v) and type(v).__name__ != 'module'])")


def kernel_for(gcp, user: str, runtime_id: str, token: str) -> Kernel:
    """The relay to `user`'s own runtime; refuses another user's (its proxy would too)."""
    r = runtime(gcp, runtime_id)
    if r["user"] != user:
        raise PermissionError(f"{r['name']} is {r['user']}'s runtime: cells run on your own")
    if r["state"] != "RUNNING" or not r["proxy"]:
        raise ValueError(f"{r['name']} is {r['state'] or 'not ready'}: start it first")
    return Kernel(r["proxy"], token)
