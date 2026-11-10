"""The notebooks MCP server's tools over the console's fakes: editing by cell index, saving
over the editor's concurrent saves, and running cells into the notebook."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading

import pytest
from test_console_blog import FakeGcp as BlogGcp
from test_console_notebooks import ME, FakeGcp

from gcpbricks import notebooks_mcp as M
from gcpbricks.console import notebooks as NB

PATH = f"Users/{ME}/a.ipynb"


class FakeKernel:
    def __init__(self, kernels=()):
        self.ran, self.started, self.actions = [], 0, []
        self._kernels = list(kernels)

    def kernels(self):
        return self._kernels

    def start(self, name="python3"):
        self.started += 1
        self._kernels.append({"id": "k-new", "name": name})
        return self._kernels[-1]

    def interrupt(self, kid):
        self.actions.append(("interrupt", kid))

    def restart(self, kid):
        self.actions.append(("restart", kid))

    def execute(self, kid, code):
        self.ran.append((kid, code))
        if "boom" in code:
            yield {"output_type": "error", "ename": "NameError", "evalue": "boom",
                   "traceback": ["\x1b[31mNameError\x1b[0m: boom"]}
            yield {"output_type": "done", "execution_count": len(self.ran), "status": "error"}
            return
        yield {"output_type": "stream", "name": "stdout", "text": "a"}
        yield {"output_type": "stream", "name": "stdout", "text": "b\n"}
        yield {"output_type": "execute_result", "data": {"text/plain": "42", "image/png": "x"}, "metadata": {},
               "execution_count": len(self.ran)}
        yield {"output_type": "done", "execution_count": len(self.ran), "status": "ok"}


@pytest.fixture()
def env():
    gcp, kernel = FakeGcp(), FakeKernel([{"id": "k1", "name": "python3"}])
    seen = []

    def kernel_for(g, user, runtime_id, token):
        seen.append((user, runtime_id, token))
        return kernel
    nbs = M.Notebooks(gcp, ME, lambda: "tok", kernel_for)
    nbs.create(PATH, [{"source": "x = 1"}, {"source": "# Title", "cell_type": "markdown"}, {"source": "print(x)"}])
    return nbs, gcp, kernel, seen


def notebook(gcp):
    return NB.read_notebook(gcp, ME, PATH)["notebook"]


def test_cells_are_inserted_edited_moved_and_deleted_by_index(env):
    nbs, gcp, _, _ = env
    assert nbs.insert(PATH, "y = 2", index=1) == 1
    assert nbs.insert(PATH, "z = 3") == 4
    nbs.edit(PATH, 0, tags=["parameters"])
    nbs.edit(PATH, 2, cell_type="code")
    nbs.move(PATH, 4, 0)
    nbs.delete(PATH, 1)
    cells = notebook(gcp)["cells"]
    assert [(c["cell_type"], c["source"]) for c in cells] == [
        ("code", "z = 3"), ("code", "y = 2"), ("code", "# Title"), ("code", "print(x)")]
    assert cells[2]["outputs"] == [] and "tags" not in cells[2]["metadata"]
    nbs.edit(PATH, 0, tags=[])
    assert "tags" not in notebook(gcp)["cells"][0]["metadata"]
    with pytest.raises(IndexError, match="cells 0 to 3"):
        nbs.edit(PATH, 9, source="")


def test_an_edit_lands_on_a_save_the_editor_made_in_between(env, monkeypatch):
    nbs, gcp, _, _ = env
    real = NB.read_notebook

    def read_then_editor_saves(g, user, path):
        got = real(g, user, path)
        if not getattr(read_then_editor_saves, "done", False):
            read_then_editor_saves.done = True
            theirs = json.loads(json.dumps(got["notebook"]))
            theirs["cells"][0]["source"] = "x = 100"
            NB.save_notebook(g, ME, path, theirs, got["generation"])
        return got
    monkeypatch.setattr(NB, "read_notebook", read_then_editor_saves)
    nbs.edit(PATH, 2, source="print(x * 2)")
    monkeypatch.undo()
    assert [c["source"] for c in notebook(gcp)["cells"]][::2] == ["x = 100", "print(x * 2)"]


def test_cells_run_on_the_editors_kernel_and_their_outputs_are_saved(env):
    nbs, gcp, kernel, seen = env
    out = nbs.run_cells(PATH)
    assert seen == [(ME, "1", "tok")] and [k for k, _ in kernel.ran] == ["k1", "k1"]
    assert "--- cell 0 [run 1, ok]\nab\n42\n[image/png output, shown in the console]" in out
    cells = notebook(gcp)["cells"]
    assert cells[0]["outputs"][0] == {"output_type": "stream", "name": "stdout", "text": "ab\n"}
    assert cells[2]["execution_count"] == 2 and "outputs" not in cells[1]


def test_a_run_stops_at_the_first_error(env):
    nbs, gcp, kernel, _ = env
    nbs.edit(PATH, 0, source="boom")
    out = nbs.run_cells(PATH, [0, 2])
    assert "NameError: boom" in out and "\x1b" not in out and out.endswith("stopped at cell 0")
    assert len(kernel.ran) == 1 and notebook(gcp)["cells"][0]["outputs"][0]["ename"] == "NameError"


def test_code_runs_on_your_running_runtime_or_says_how_to_get_one(env):
    nbs, gcp, kernel, _ = env
    kernel._kernels = []
    assert nbs.run_code("1").startswith("[ok]\nab\n42") and kernel.started == 1
    gcp.runtimes[0]["runtimeState"] = "STOPPED"
    with pytest.raises(ValueError, match="no running Colab runtime"):
        nbs.run_code("1")
    assert [r["id"] for r in nbs.runtimes()] == ["1"]
    assert nbs.kernel_action("interrupt", "1") == "kernel k-new: interrupted"


def test_outputs_are_cut_to_the_limit():
    out = M.render_outputs([{"output_type": "stream", "name": "stdout", "text": "x" * 50}], limit=10)
    assert out == "xxxxxxxxxx\n... (40 more characters in the notebook)"


class SlowKernel(FakeKernel):
    """A cell that runs until the test lets it end, silent but for heartbeats."""

    def __init__(self, kernels=()):
        super().__init__(kernels)
        self.release, self.started_cell = threading.Event(), threading.Event()
        self.state = "idle"

    def status(self, kid):
        return {"execution_state": self.state, "last_activity": "t", "connections": 1}

    def execute(self, kid, code):
        self.ran.append((kid, code))
        self.state = "busy"
        self.started_cell.set()
        yield {"output_type": "stream", "name": "stdout", "text": "step 1\n"}
        while not self.release.wait(0.02):
            yield {"output_type": "heartbeat"}
        self.state = "idle"
        yield {"output_type": "done", "execution_count": len(self.ran), "status": "ok"}


@pytest.fixture()
def slow():
    gcp, kernel = FakeGcp(), SlowKernel([{"id": "k1", "name": "python3"}])
    nbs = M.Notebooks(gcp, ME, lambda: "tok", lambda *a: kernel)
    nbs.create(PATH, [{"source": "train()"}, {"source": "print(1)"}])
    yield nbs, gcp, kernel
    kernel.release.set()


def runs(gcp):
    from gcpbricks.console import runs as RUNS
    return RUNS.list_runs(gcp, PATH)


def test_a_slow_run_does_not_hold_any_other_call():
    """The server ran plain tool functions on its event loop: a long cell held every call."""
    pytest.importorskip("mcp")
    gcp, kernel = FakeGcp(), SlowKernel([{"id": "k1", "name": "python3"}])
    nbs = M.Notebooks(gcp, ME, lambda: "tok", lambda *a: kernel)
    srv = M.server(lambda: nbs)

    async def both():
        run = asyncio.create_task(srv.call_tool("run_code", {"code": "train()", "wait": 30}))
        await asyncio.to_thread(kernel.started_cell.wait, 5)
        listed = await asyncio.wait_for(srv.call_tool("list_runtimes", {}), 2)
        stopped = await asyncio.wait_for(srv.call_tool("kernel", {"action": "status"}), 2)
        assert not run.done()
        kernel.release.set()
        await asyncio.wait_for(run, 5)
        return listed, stopped
    listed, status = asyncio.run(both())
    assert '"id": "1"' in str(listed) and "busy" in str(status)


def test_a_run_longer_than_its_wait_is_named_and_followed(slow):
    nbs, gcp, kernel = slow
    out = nbs.run_cells(PATH, wait=0.3)
    assert "still running cell 0" in out and "step 1" in out and "run_status(" in out
    (record,) = runs(gcp)
    assert record["by"] == "claude" and [c["state"] for c in record["cells"]] == ["running", "queued"]
    assert record["cells"][0]["outputs"][0]["text"] == "step 1\n"
    job_id = out.split("run_status('")[1].split("'")[0]
    assert "running" in nbs.run_status()
    kernel.release.set()
    done = nbs.run_status(job_id, wait=5)
    assert "--- cell 0 [run 1, ok]" in done and "--- cell 1 [run 2, ok]" in done
    (record,) = runs(gcp)
    assert record["state"] == "done" and [c["state"] for c in record["cells"]] == ["ok", "ok"]
    assert notebook(gcp)["cells"][0]["outputs"][0]["text"] == "step 1\n"


def test_a_second_run_waits_for_the_first_on_the_kernel(slow):
    nbs, _, kernel = slow
    nbs.run_cells(PATH, [0], wait=0.1)
    second = nbs.run_code("1 + 1", wait=0.1)
    assert "waits for the kernel" in second and len(kernel.ran) == 1
    kernel.release.set()
    assert nbs.run_status(second.split("run_status('")[1].split("'")[0], wait=5).startswith("[ok]")


def test_a_lost_connection_says_what_happened_to_the_runtime(slow):
    nbs, gcp, kernel = slow

    def lost(kid, code):
        yield {"output_type": "stream", "name": "stdout", "text": "epoch 1\n"}
        gcp.runtimes[0].update(runtimeState="STOPPED", idleShutdownConfig={"idleTimeout": "1800s"})
        raise NB.KernelConnectionLost("closed (1005)")
    kernel.execute = lost
    out = nbs.run_cells(PATH, wait=5)
    assert "--- cell 0 [lost]" in out and "is STOPPED: Colab stops a runtime after 30 min" in out
    (record,) = runs(gcp)
    assert record["state"] == "failed" and "STOPPED" in record["error"]
    assert [c["state"] for c in record["cells"]] == ["failed", "cancelled"]


def test_liveness_names_each_case():
    from gcpbricks.console import runs as RUNS
    fresh, old = {"state": "running", "heartbeat": 100}, {"state": "running", "heartbeat": 0}
    busy, idle, dead = ({"execution_state": s} for s in ("busy", "idle", "dead"))
    assert RUNS.liveness(fresh, "RUNNING", busy, now=110) == "running"
    assert RUNS.liveness(old, "RUNNING", busy, now=110) == "runner_gone"
    assert RUNS.liveness(old, "RUNNING", idle, now=110) == "lost"
    assert RUNS.liveness(fresh, "RUNNING", dead, now=110) == "kernel_dead"
    assert RUNS.liveness(fresh, "RUNNING", None, now=110) == "kernel_gone"
    assert RUNS.liveness(fresh, "STOPPING", busy, now=110) == "runtime_stopped"
    assert RUNS.liveness(old, "RUNNING", {"execution_state": "unknown"}, now=110) == "stale"
    assert RUNS.liveness({**old, "state": "done"}, "STOPPED", None, now=110) == "done"


def test_cells_get_ids_that_stay_when_cells_move_or_change_type(env):
    nbs, gcp, _, _ = env
    ids = [c["id"] for c in notebook(gcp)["cells"]]
    assert len(set(ids)) == 3 and all(ids)
    nbs.move(PATH, 2, 0)
    nbs.edit(PATH, 1, cell_type="markdown")
    assert [c["id"] for c in notebook(gcp)["cells"]] == [ids[2], ids[0], ids[1]]


def test_a_process_the_kernel_starts_calls_google_as_the_user(tmp_path):
    """Before, only the kernel's own google.auth was given the token: a script it ran got 401."""
    pytest.importorskip("google.auth")
    image = tmp_path / "image"  # a sitecustomize the runtime's image already has keeps running
    image.mkdir()
    (image / "sitecustomize.py").write_text("import os\nos.environ['IMAGE_HOOK'] = 'ran'\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MLOPS_")}
    env.update(HOME=str(tmp_path), PYTHONPATH=str(image))
    # the kernel: a cell's credentials, then the environment a process it starts inherits
    kernel = subprocess.run([sys.executable, "-c", NB.credentials_code("tok-1") + "\nimport json, os\n"
                             "print(json.dumps(dict(os.environ)))"], env=env, check=True, capture_output=True, text=True)
    child = subprocess.run([sys.executable, "-c", ("import google.auth, os\n"
                                                   "print(google.auth.default()[0].token, os.environ.get('IMAGE_HOOK'))")],
                           env=json.loads(kernel.stdout), check=True, capture_output=True, text=True)
    assert child.stdout.split() == ["tok-1", "ran"]


def test_the_server_lists_its_tools_without_signing_in():
    pytest.importorskip("mcp")
    made = []
    names = {t.name for t in asyncio.run(M.server(lambda: made.append(1)).list_tools())}
    assert {"read_notebook", "edit_cell", "run_cells", "run_code", "start_runtime",
            "create_blog_post", "update_blog_post"} <= names and not made


def test_a_post_is_published_as_you_and_edited_field_by_field():
    blog = M.Blog(BlogGcp(), ME)
    slug = blog.create("Hướng dẫn: Feature Manager", "# Hi\n\nbody", ["guide"], "one line")
    assert slug == "huong-dan-feature-manager"
    blog.update(slug, body="# Hi\n\nnew body")
    (post,) = blog.list()
    assert (post["author"], post["tags"], post["summary"]) == (ME, ["guide"], "one line")
    assert blog.read(slug).endswith("---\n# Hi\n\nnew body")


def test_someone_elses_post_is_not_yours_to_edit():
    gcp = BlogGcp()
    slug = M.Blog(gcp, "you@x.com").create("Theirs", "body")
    with pytest.raises(PermissionError):
        M.Blog(gcp, ME).update(slug, body="mine now")


def test_an_edit_made_in_between_fails_the_save(monkeypatch):
    gcp = BlogGcp()
    blog = M.Blog(gcp, ME)
    slug = blog.create("Post", "body")
    read = M.BL.read_post

    def read_then_editor_saves(*a, **k):
        got = read(*a, **k)
        M.BL.save_post(gcp, ME, slug, "Post", [], "", "the editor's", got["generation"])
        return got
    monkeypatch.setattr(M.BL, "read_post", read_then_editor_saves)
    with pytest.raises(ValueError, match="changed since"):
        blog.update(slug, body="mine")


def test_an_image_is_uploaded_and_given_as_markdown(tmp_path):
    gcp = BlogGcp()
    blog = M.Blog(gcp, ME)
    slug = blog.create("Post", "body")
    (tmp_path / "chart.png").write_bytes(b"png")
    md = blog.upload_image(slug, str(tmp_path / "chart.png"))
    assert md.startswith(f"![](/api/blog/assets/{slug}/") and md.endswith(".png)")
    with pytest.raises(ValueError):
        (tmp_path / "x.svg").write_bytes(b"<svg/>")
        blog.upload_image(slug, str(tmp_path / "x.svg"))
