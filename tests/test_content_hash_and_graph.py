from __future__ import annotations

from gcpbricks.content_hash import ast_hash, tree_hash
from gcpbricks.import_graph import closure

BASE = b"def f(a):\n    return a + 1\n"


def test_cosmetic_edits_keep_the_hash_and_logic_moves_it():
    assert ast_hash(b'def f(a):\n    """Doc."""\n    # note\n    return (a+1)\n') == ast_hash(BASE)
    assert ast_hash(b"def f(a):\n    return a + 2\n") != ast_hash(BASE)
    assert ast_hash(b"def broken(:\n") is None


def test_tree_hash_is_order_independent_and_path_keyed(tmp_path):
    (tmp_path / "a.py").write_bytes(BASE)
    (tmp_path / "b.py").write_bytes(BASE)
    files = [tmp_path / "a.py", tmp_path / "b.py"]
    before = tree_hash(files, tmp_path)
    assert before == tree_hash(files[::-1], tmp_path)
    (tmp_path / "b.py").rename(tmp_path / "c.py")
    assert tree_hash([tmp_path / "a.py", tmp_path / "c.py"], tmp_path) != before


def test_closure_follows_lazy_imports_and_stops_at_third_party(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("import numpy\ndef f():\n    from pkg import b\n")
    (tmp_path / "pkg" / "b.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "unused.py").write_text("x = 2\n")
    reached = {p.name for p in closure(["pkg.a"], [tmp_path])}
    assert reached == {"a.py", "b.py", "__init__.py"}


def test_an_ignored_edge_is_dropped_only_from_its_importer(tmp_path):
    (tmp_path / "a.py").write_text("import b\nimport c\n")
    (tmp_path / "c.py").write_text("import b\n")
    (tmp_path / "b.py").write_text("")
    reached = {p.name for p in closure(["a"], [tmp_path], ignore={("a.py", "b")}, root=tmp_path)}
    assert reached == {"a.py", "b.py", "c.py"}  # c.py still imports b
    (tmp_path / "c.py").write_text("")
    reached = {p.name for p in closure(["a"], [tmp_path], ignore={("a.py", "b")}, root=tmp_path)}
    assert reached == {"a.py", "c.py"}
