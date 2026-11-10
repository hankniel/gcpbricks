"""Hash a .py file by its AST, so a comment-, docstring- or formatting-only change keeps the
hash: a content-addressed image tag or retrain key then moves only when logic does.
Docstrings are dropped, so code that reads one at run time (beyond help text) must not rely
on this."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path


def ast_hash(src: bytes) -> str | None:
    """sha256 of the normalized AST, or None if `src` does not parse."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


def file_digest(path: Path) -> str:
    """A .py file by its AST (bytes if it does not parse), anything else by its bytes."""
    data = path.read_bytes()
    return (ast_hash(data) if path.suffix == ".py" else None) or hashlib.sha256(data).hexdigest()


def tree_hash(files: list[Path], root: Path) -> str:
    """One hash over files, each keyed by its path relative to root, order-independent."""
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f"{f.relative_to(root)}\0{file_digest(f)}\n".encode())
    return h.hexdigest()
