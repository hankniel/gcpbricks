"""The console's notebooks over fakes: the workspace's rules, Colab compute, the cell relay
and the notebook-access cookie."""
from __future__ import annotations

import json
import os
import time

import pytest
import requests

from gcpbricks.console import notebooks as NB

P = "projects/p/locations/r"
ME = "me@x.com"


class FakeGcp:
    project, region, parent = "p", "r", P

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.calls, self.audits = [], []
        self.runtimes = [{"name": f"{P}/notebookRuntimes/1", "displayName": "me-1", "runtimeUser": ME,
                          "runtimeState": "RUNNING", "proxyUri": "abc.proxy.example"},
                         {"name": f"{P}/notebookRuntimes/2", "displayName": "you-1", "runtimeUser": "you@x.com",
                          "runtimeState": "STOPPED", "proxyUri": "def.proxy.example"}]

    def list_dir(self, bucket, prefix):
        names = [n for n in self.objects if n.startswith(prefix)]
        subs = sorted({prefix + n[len(prefix):].split("/")[0] + "/" for n in names if "/" in n[len(prefix):]})
        return subs, [{"name": n, "updated": "t", "size": "1", "generation": self.objects[n][1]}
                      for n in names if "/" not in n[len(prefix):]]

    def read_object(self, bucket, obj):
        return self.objects.get(obj)

    def object_generation(self, bucket, obj):
        return self.objects[obj][1] if obj in self.objects else None

    def write_object(self, bucket, obj, data, content_type, if_generation=None):
        current = self.objects.get(obj, (b"", "0"))[1]
        if if_generation is not None and if_generation != current:
            r = requests.Response()
            r.status_code = 412
            raise requests.HTTPError(response=r)
        gen = str(int(current) + 1)
        self.objects[obj] = (data, gen)
        return gen

    def delete_object(self, bucket, obj):
        self.objects.pop(obj, None)

    def ai_list(self, path, key, **params):
        return self.runtimes if path == "notebookRuntimes" else [
            {"name": f"{P}/notebookRuntimeTemplates/9", "displayName": "Default", "isDefault": True,
             "machineSpec": {"machineType": "e2-standard-4"}, "idleShutdownConfig": {"idleTimeout": "10800s"}}]

    def ai_get(self, path, **params):
        return next(r for r in self.runtimes if r["name"].endswith("/" + path.rsplit("/", 1)[-1]))

    def ai_post(self, path, body):
        self.calls.append(("post", path, body))
        return {"name": "op"}

    def ai_delete(self, path):
        self.calls.append(("delete", path))
        return {}

    def audit(self, payload):
        self.audits.append(payload)


@pytest.fixture()
def gcp():
    return FakeGcp()


def test_a_user_writes_their_own_folder_and_shared_only(gcp):
    NB.create_notebook(gcp, ME, "Users/me@x.com/a")
    NB.create_notebook(gcp, ME, "Shared/team.ipynb")
    with pytest.raises(PermissionError):
        NB.create_notebook(gcp, ME, "Users/you@x.com/b.ipynb")
    NB.create_notebook(gcp, ME, "Users/you@x.com/b.ipynb", admin=True)
    for bad in ["../etc/x.ipynb", "Elsewhere/x.ipynb", "Users/me@x.com/../you@x.com/x.ipynb"]:
        with pytest.raises(ValueError):
            NB.create_notebook(gcp, ME, bad)
    assert "workspace/Users/me@x.com/a.ipynb" in gcp.objects


def test_the_root_lists_shared_and_users_and_your_folder_comes_first(gcp):
    assert [f["path"] for f in NB.list_folder(gcp, ME)["folders"]] == ["Shared", "Users"]
    NB.create_notebook(gcp, "zed@x.com", "Users/zed@x.com/z.ipynb")
    users = NB.list_folder(gcp, ME, "Users")["folders"]
    assert [f["name"] for f in users] == [ME, "zed@x.com"]
    NB.create_notebook(gcp, ME, "Users/me@x.com/a.ipynb")
    mine = NB.list_folder(gcp, ME, "Users/me@x.com")
    assert [n["name"] for n in mine["notebooks"]] == ["a.ipynb"]


def test_sharing_a_notebook_sets_who_views_and_edits_it(gcp):
    YOU, THEM = "you@x.com", "them@x.com"
    mine = "Users/me@x.com/a.ipynb"
    made = NB.create_notebook(gcp, ME, mine)
    NB.create_notebook(gcp, ME, "Users/me@x.com/b.ipynb")
    # By default everyone views it and only its owner edits it.
    assert NB.read_notebook(gcp, YOU, mine)["access"] == "view"
    assert NB.read_notebook(gcp, ME, mine)["access"] == "manage"
    with pytest.raises(PermissionError, match="ask its owner"):
        NB.save_notebook(gcp, YOU, mine, json.loads(gcp.objects[f"workspace/{mine}"][0]), made["generation"])
    with pytest.raises(PermissionError):
        NB.set_share(gcp, YOU, mine, "view", {YOU: "edit"})

    NB.set_share(gcp, ME, mine, "private", {" You@X.com ": "edit", ME: "view"})
    assert NB.get_share(gcp, ME, mine) == {"path": mine, "owner": ME, "general": "private", "people": {YOU: "edit"}}
    got = NB.read_notebook(gcp, YOU, mine)
    assert got["access"] == "edit"
    NB.save_notebook(gcp, YOU, mine, got["notebook"], got["generation"])
    with pytest.raises(PermissionError, match="isn't shared"):
        NB.read_notebook(gcp, THEM, mine)
    with pytest.raises(PermissionError):
        NB.notebook_generation(gcp, THEM, mine)
    # An editor doesn't share or delete it; an admin does anything.
    with pytest.raises(PermissionError):
        NB.get_share(gcp, YOU, mine)
    with pytest.raises(PermissionError):
        NB.delete_notebook(gcp, YOU, mine)
    assert NB.read_notebook(gcp, THEM, mine, admin=True)["access"] == "manage"

    assert [n["name"] for n in NB.list_folder(gcp, THEM, "Users/me@x.com")["notebooks"]] == ["b.ipynb"]
    assert [n["name"] for n in NB.list_folder(gcp, YOU, "Users/me@x.com")["notebooks"]] == ["a.ipynb", "b.ipynb"]
    for bad in [("public", {}), ("view", {"nobody": "view"}), ("view", {YOU: "manage"})]:
        with pytest.raises(ValueError):
            NB.set_share(gcp, ME, mine, *bad)
    with pytest.raises(ValueError, match="Users/ folder"):
        NB.set_share(gcp, ME, "Shared/x.ipynb", "view", {})
    assert gcp.audits[-1]["action"] == "share_notebook"

    # Deleting it drops its settings, so a notebook made later at that path starts unshared.
    NB.delete_notebook(gcp, ME, mine)
    assert not any(k.startswith("workspace/.shares/") for k in gcp.objects)


def test_a_save_over_someone_elses_newer_save_is_refused(gcp):
    made = NB.create_notebook(gcp, ME, "Shared/a.ipynb")
    read = NB.read_notebook(gcp, ME, "Shared/a.ipynb")
    assert read["generation"] == made["generation"] and read["notebook"]["cells"][0]["cell_type"] == "code"
    theirs = NB.save_notebook(gcp, "you@x.com", "Shared/a.ipynb", read["notebook"], read["generation"])
    assert NB.notebook_generation(gcp, ME, "Shared/a.ipynb")["generation"] == theirs["generation"] != read["generation"]
    with pytest.raises(ValueError, match="changed since you opened it"):
        NB.save_notebook(gcp, ME, "Shared/a.ipynb", read["notebook"], read["generation"])


def test_runtimes_are_their_owners_or_an_admins(gcp):
    assert [r["user"] for r in NB.runtimes(gcp)] == [ME, "you@x.com"]
    with pytest.raises(PermissionError):
        NB.set_runtime(gcp, ME, "2", "start")
    NB.set_runtime(gcp, ME, "2", "start", admin=True)
    NB.set_runtime(gcp, ME, "1", "stop")
    assert ("post", f"{P}/notebookRuntimes/1:stop", {}) in gcp.calls
    NB.create_runtime(gcp, ME, "9")
    _, path, body = gcp.calls[-1]
    assert path == f"{P}/notebookRuntimes:assign" and body["notebookRuntime"]["runtimeUser"] == ME
    assert body["notebookRuntimeTemplate"] == f"{P}/notebookRuntimeTemplates/9"
    assert body["notebookRuntime"]["displayName"].startswith("me-")
    NB.create_runtime(gcp, ME, "9", " my-gpu ")
    assert gcp.calls[-1][2]["notebookRuntime"]["displayName"] == "my-gpu"


def test_cells_run_only_on_your_own_running_runtime(gcp):
    with pytest.raises(PermissionError):
        NB.kernel_for(gcp, ME, "2", "t")
    gcp.runtimes[0]["runtimeState"] = "STOPPED"
    with pytest.raises(ValueError, match="start it first"):
        NB.kernel_for(gcp, ME, "1", "t")


class FakeWs:
    def __init__(self, replies):
        self.raw, self.replies, self.sent = replies, [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send(self, text):
        self.sent.append(json.loads(text))
        mid = self.sent[-1]["header"]["msg_id"]
        self.replies += [json.dumps({**r, "parent_header": {"msg_id": mid}}) for r in self.raw]

    def recv(self, timeout=None):
        return self.replies.pop(0)


def test_a_cells_messages_become_its_outputs(monkeypatch):
    replies = [{"msg_type": "status", "content": {"execution_state": "busy"}},
               {"msg_type": "execute_input", "content": {"execution_count": 3}},
               {"msg_type": "stream", "content": {"name": "stdout", "text": "hi\n"}},
               {"msg_type": "execute_result", "content": {"data": {"text/plain": "42"}, "execution_count": 3}},
               {"msg_type": "execute_reply", "content": {"status": "ok", "execution_count": 3}},
               {"msg_type": "status", "content": {"execution_state": "idle"}}]
    ws = FakeWs(replies)
    seen = {}

    def connect(url, additional_headers, origin, open_timeout, max_size):
        seen.update(url=url, headers=additional_headers, origin=origin)
        return ws
    from websockets.sync import client
    monkeypatch.setattr(client, "connect", connect)
    k = NB.Kernel("abc.proxy.example", "tok")
    monkeypatch.setattr(k, "kernels", list)
    k.s.cookies.set("S", "tunnel")
    out = list(k.execute("k1", "print('hi'); 6*7"))
    assert [o["output_type"] for o in out] == ["stream", "execute_result", "done"]
    assert out[-1] == {"output_type": "done", "execution_count": 3, "status": "ok"}
    assert seen["url"].startswith("wss://abc.proxy.example/api/kernels/k1/channels")
    assert seen["headers"] == {"Authorization": "Bearer tok", "Cookie": "S=tunnel"}
    first, cell = ws.sent
    assert first["content"]["code"] == NB.credentials_code("tok")
    assert first["content"]["silent"] and not first["content"]["store_history"]
    assert not first["content"]["stop_on_error"]
    assert cell["content"]["code"] == "print('hi'); 6*7"
    assert not cell["content"]["silent"] and cell["content"]["store_history"]


def _kernel_with(monkeypatch, replies):
    ws = FakeWs(replies)
    from websockets.sync import client
    monkeypatch.setattr(client, "connect", lambda *a, **k: ws)
    k = NB.Kernel("abc.proxy.example", "tok")
    monkeypatch.setattr(k, "kernels", list)
    return k, ws


def test_completions_come_from_the_kernel_without_running_anything(monkeypatch):
    k, ws = _kernel_with(monkeypatch, [
        {"msg_type": "status", "content": {"execution_state": "busy"}},
        {"msg_type": "complete_reply", "content": {
            "matches": ["DataFrame", "DateOffset"], "cursor_start": 3, "cursor_end": 6,
            "metadata": {"_jupyter_types_experimental": [{"text": "DataFrame", "type": "class"}]}}}])
    got = k.complete("k1", "pd.Dat", 6)
    assert got == {"matches": ["DataFrame", "DateOffset"], "cursor_start": 3, "cursor_end": 6, "types": {"DataFrame": "class"}}
    [sent] = ws.sent
    assert sent["header"]["msg_type"] == "complete_request"
    assert sent["content"] == {"code": "pd.Dat", "cursor_pos": 6}


def test_inspect_returns_the_docstring_text(monkeypatch):
    k, ws = _kernel_with(monkeypatch, [{"msg_type": "inspect_reply", "content": {
        "found": True, "data": {"text/plain": "Signature: len(obj, /)"}}}])
    assert k.inspect("k1", "len(", 3) == {"found": True, "text": "Signature: len(obj, /)"}
    assert ws.sent[0]["content"]["detail_level"] == 0


def test_variables_are_read_as_a_silent_user_expression(monkeypatch):
    listed = json.dumps([{"name": "df", "type": "DataFrame", "shape": "(3, 2)", "value": "<df>"}])
    k, ws = _kernel_with(monkeypatch, [{"msg_type": "execute_reply", "content": {
        "status": "ok", "user_expressions": {"vars": {"status": "ok", "data": {"text/plain": repr(listed)}}}}}])
    assert k.variables("k1") == [{"name": "df", "type": "DataFrame", "shape": "(3, 2)", "value": "<df>"}]
    [sent] = ws.sent
    assert sent["content"]["silent"] and not sent["content"]["store_history"] and sent["content"]["code"] == ""
    assert sent["content"]["user_expressions"] == {"vars": NB.VARIABLES}


def test_the_variables_expression_lists_data_not_modules_or_functions():
    import json as json_module
    ns = {"json": json_module, "x": 3, "_hidden": 1, "f": lambda: 1, "In": [], "rows": [1, 2]}
    got = json.loads(eval(NB.VARIABLES, ns))
    assert [(v["name"], v["type"], v["value"]) for v in got] == [("x", "int", "3"), ("rows", "list", "[1, 2]")]


def test_a_kernel_calls_google_as_the_user(monkeypatch, tmp_path):
    import google.auth

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setattr(google.auth, "default", google.auth.default)
    monkeypatch.delenv("CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", raising=False)
    exec(NB.credentials_code("ya29.user"), {})  # noqa: S102 - what the kernel runs
    creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    assert (creds.token, project) == ("ya29.user", "proj")
    path = os.environ["CLOUDSDK_AUTH_ACCESS_TOKEN_FILE"]
    with open(path) as f:
        assert f.read() == "ya29.user"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_a_runtime_refusing_the_token_asks_for_access_again():
    class Session(requests.Session):
        def request(self, method, url, **kw):
            r = requests.Response()
            r.status_code = 302
            return r
    with pytest.raises(PermissionError, match="allow it again"):
        NB.Kernel("abc.proxy.example", "tok", Session()).kernels()


def test_the_access_cookie_serves_only_the_user_it_was_granted_for():
    from gcpbricks.console import app as A

    class Req:
        def __init__(self, value):
            self.cookies = {A.TOKEN_COOKIE: value}
    later = int(time.time()) + 3000
    assert A._granted(Req(f"{ME}|{later}|tok"), ME) == "tok"
    assert A._granted(Req(f"{ME}|{later}|tok"), "you@x.com") is None
    assert A._granted(Req(f"{ME}|{int(time.time())}|tok"), ME) is None


def test_only_admins_change_templates_and_a_create_sends_every_setting(gcp):
    spec = {"name": "GPU T4", "machine": "n1-standard-8", "accelerator": "NVIDIA_TESLA_T4", "accelerator_count": 1,
            "disk_gb": 200, "idle_minutes": 60, "internet": True, "image": "py311",
            "env": {"TARGET": "dev"}, "startup_script": "pip install gcpbricks", "startup_behavior": "RUN_ONCE"}
    with pytest.raises(PermissionError):
        NB.create_template(gcp, ME, spec)
    NB.create_template(gcp, ME, spec, admin=True)
    _, path, body = gcp.calls[-1]
    assert path == f"{P}/notebookRuntimeTemplates"
    assert body["machineSpec"] == {"machineType": "n1-standard-8", "acceleratorType": "NVIDIA_TESLA_T4", "acceleratorCount": 1}
    assert body["dataPersistentDiskSpec"]["diskSizeGb"] == "200" and body["idleShutdownConfig"] == {"idleTimeout": "3600s"}
    assert body["softwareConfig"]["env"] == [{"name": "TARGET", "value": "dev"}]
    assert body["softwareConfig"]["postStartupScriptConfig"] == {"postStartupScript": "pip install gcpbricks",
                                                                  "postStartupScriptBehavior": "RUN_ONCE"}
    with pytest.raises(ValueError, match="10 to 1440"):
        NB.create_template(gcp, ME, {**spec, "idle_minutes": 5}, admin=True)


def test_an_edit_changes_only_what_colab_lets_change(gcp):
    patched = []
    gcp.ai_patch = lambda path, body, mask: patched.append((path, body, mask)) or {}
    NB.update_template(gcp, ME, "9", {"name": "Default v2", "env": {"A": "1"}, "etag": "e1"}, admin=True)
    path, body, mask = patched[-1]
    assert path == "notebookRuntimeTemplates/9" and body["etag"] == "e1" and body["displayName"] == "Default v2"
    assert mask == "display_name,software_config.env"
    with pytest.raises(ValueError, match="duplicate it"):
        NB.update_template(gcp, ME, "9", {"machine": "e2-standard-8"}, admin=True)
    with pytest.raises(PermissionError):
        NB.update_template(gcp, ME, "9", {"name": "x"})


# --- liveness: heartbeats, lost connections, run records -------------------------------------

class SilentWs(FakeWs):
    """Replies with a pause (the kernel busy and quiet) or a dropped connection where the
    script says so."""

    def __init__(self, script):
        super().__init__([])
        self.script = script

    def send(self, text):
        self.sent.append(json.loads(text))
        if self.sent[-1]["content"].get("silent"):  # the credentials ahead of the cell
            return
        mid = self.sent[-1]["header"]["msg_id"]
        self.replies += [r if isinstance(r, str) else json.dumps({**r, "parent_header": {"msg_id": mid}})
                         for r in self.script]

    def recv(self, timeout=None):
        r = self.replies.pop(0)
        if r == "pause":
            raise TimeoutError
        if r == "drop":
            from websockets.exceptions import ConnectionClosedError
            raise ConnectionClosedError(None, None)
        return r


def _silent_kernel(monkeypatch, script):
    ws = SilentWs(script)
    from websockets.sync import client
    monkeypatch.setattr(client, "connect", lambda *a, **k: ws)
    k = NB.Kernel("abc.proxy.example", "tok")
    monkeypatch.setattr(k, "kernels", list)
    return k


def test_a_silent_cell_beats_instead_of_timing_out(monkeypatch):
    k = _silent_kernel(monkeypatch, ["pause", "pause",
                                     {"msg_type": "execute_reply", "content": {"status": "ok", "execution_count": 1}},
                                     {"msg_type": "status", "content": {"execution_state": "idle"}}])
    assert [o["output_type"] for o in k.execute("k1", "train()")] == ["heartbeat", "heartbeat", "done"]


def test_a_dropped_connection_is_named_not_left_to_the_socket(monkeypatch):
    k = _silent_kernel(monkeypatch, [{"msg_type": "stream", "content": {"name": "stdout", "text": "epoch 1\n"}}, "drop"])
    got = k.execute("k1", "train()")
    assert next(got)["text"] == "epoch 1\n"
    with pytest.raises(NB.KernelConnectionLost, match="closed mid-run"):
        next(got)


def test_a_runtime_says_its_idle_shutdown():
    assert NB._runtime({"name": "x/1", "idleShutdownConfig": {"idleTimeout": "1800s"}})["idle"] == "30 min"
    assert NB._runtime({"name": "x/1", "idleShutdownConfig": {"idleTimeout": "14400s"}})["idle"] == "4 h"
    assert NB._runtime({"name": "x/1", "idleShutdownConfig": {"idleShutdownDisabled": True}})["idle"] is None


class StatusKernel:
    def __init__(self, state="idle"):
        self.state = state

    def status(self, kid):
        return {"execution_state": self.state, "last_activity": "t", "connections": 1}

    def execute(self, kid, code):
        yield {"output_type": "stream", "name": "stdout", "text": "hi\n"}
        yield {"output_type": "heartbeat"}
        yield {"output_type": "done", "execution_count": 4, "status": "ok"}


@pytest.fixture()
def api(gcp, monkeypatch):
    from fastapi.testclient import TestClient

    from gcpbricks.console import app as A
    monkeypatch.delenv("MLOPS_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("MLOPS_CONSOLE_DEV", raising=False)
    monkeypatch.setattr(A, "gcp", lambda: gcp)
    kernel = StatusKernel()
    monkeypatch.setattr(NB, "kernel_for", lambda *a: kernel)
    A.app.dependency_overrides[A.user] = lambda: ME
    client = TestClient(A.app)
    client.cookies.set(A.TOKEN_COOKIE, f"{ME}|{int(time.time()) + 3600}|tok")
    NB.create_notebook(gcp, ME, f"Users/{ME}/a.ipynb")
    yield client, kernel
    A.app.dependency_overrides.clear()


def test_a_cell_run_from_the_editor_leaves_a_record_another_page_reads(api):
    client, _ = api
    path = f"Users/{ME}/a.ipynb"
    r = client.post("/api/notebooks/runtimes/1/kernels/k1/execute",
                    json={"code": "print('hi')", "path": path, "cell_id": "c1", "index": 0})
    assert [json.loads(x)["output_type"] for x in r.text.splitlines()] == ["stream", "heartbeat", "done"]
    (run,) = client.get("/api/workspace/notebook/runs", params={"path": path}).json()["runs"]
    assert (run["by"], run["live"], run["cells"][0]["state"], run["cells"][0]["execution_count"]) == ("editor", "done", "ok", 4)
    assert run["cells"][0]["outputs"] == [{"output_type": "stream", "name": "stdout", "text": "hi\n"}]


def test_a_run_whose_page_went_away_shows_as_still_on_the_kernel(api, gcp):
    from gcpbricks.console import runs as RUNS
    client, kernel = api
    path = f"Users/{ME}/a.ipynb"
    RUNS.RunRecord(gcp, ME, path, "editor", "1", "k1", [(0, "c1")], clock=lambda: 0).start_cell(0)
    kernel.state = "busy"
    (run,) = client.get("/api/workspace/notebook/runs", params={"path": path}).json()["runs"]
    assert run["live"] == "runner_gone" and "outputs from now on are not captured" in run["why"]
    kernel.state = "idle"
    assert client.get("/api/workspace/notebook/runs", params={"path": path}).json()["runs"][0]["live"] == "lost"
    gcp.runtimes[0].update(runtimeState="STOPPED", idleShutdownConfig={"idleTimeout": "1800s"})
    (run,) = client.get("/api/workspace/notebook/runs", params={"path": path}).json()["runs"]
    assert run["live"] == "runtime_stopped" and "after 30 min with no kernel activity" in run["why"]
