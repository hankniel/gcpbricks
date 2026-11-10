"""Repo files a set of entrypoint modules can import, transitively, read from the source with
`ast` -- nothing is imported or executed. ALL imports count, lazy ones included, so the set
over-approximates: a file in it may not run, a file outside it never does (dynamic imports
aside, which the caller adds as entrypoints).

Names resolve against `search_dirs` in order, the way sys.path would.
"""
from __future__ import annotations

import ast
from pathlib import Path


def module_path(name: str, search_dirs: list[Path]) -> Path | None:
    """The file backing module `name` in the first search dir that has it, or None
    (third-party or stdlib)."""
    rel = name.replace(".", "/")
    for base in search_dirs:
        for path in (base / f"{rel}.py", base / rel / "__init__.py"):
            if path.is_file():
                return path
    return None


def all_imports(path: Path) -> set[str]:
    """Every module this file imports, at any nesting depth."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            # Both the module and each `module.name`: `from pkg import sub` is
            # ImportFrom(module="pkg"), and recording only that stops at pkg/__init__.py.
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def closure(entrypoints: list[str], search_dirs: list[Path], *, ignore: set = frozenset(),
            root: Path | None = None) -> set[Path]:
    """Files reachable from `entrypoints` through imports that resolve in `search_dirs`.

    `ignore` drops single (importer, module) EDGES, the importer as a path relative to
    `root`: a guarded fallback in one file cannot then excuse a hard import of the same
    module in another."""
    def resolve(name: str) -> Path | None:
        return module_path(name, search_dirs)

    seen = set(entrypoints)
    queue = list(entrypoints)
    files: set[Path] = set()
    while queue:
        path = resolve(queue.pop(0))
        if path is None:
            continue
        files.add(path)
        importer = str(path.relative_to(root)) if root else str(path)
        for imported in all_imports(path):
            if any(importer == f and imported.startswith(m) for f, m in ignore):
                continue
            key = imported if resolve(imported) else imported.split(".")[0]
            if key not in seen and resolve(key):
                seen.add(key)
                queue.append(key)
    return files
