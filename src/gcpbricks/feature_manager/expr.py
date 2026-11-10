"""Parse, check and evaluate one `expr` string.

Python's own `ast` parses it, then only a whitelist of node types survives: arithmetic,
comparisons, `&`/`|`/`~`, names, numbers, and calls to ops.OPS. No attribute access,
subscripts, lambdas or comprehensions, so a spec cannot reach anything but those ops.
`vars` are folded in at parse time -- scalars become constants and each cols(...) call
is rewritten to its concrete column list -- so the stored tree reads only frame columns
and earlier features.
"""
from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .ops import COLS, MATRIX, OPS, ROW_MAP, ROW_REDUCE, SCALAR, VECTOR, align, broadcast_shape

_BINOPS = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply,
           ast.Div: np.divide, ast.Pow: np.power, ast.Mod: np.mod,
           ast.BitAnd: np.logical_and, ast.BitOr: np.logical_or}
_CMPOPS = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater,
           ast.GtE: np.greater_equal, ast.Eq: np.equal, ast.NotEq: np.not_equal}
_UNARYOPS = {ast.USub: np.negative, ast.UAdd: np.positive, ast.Invert: np.logical_not}
_FLOAT_NAMES = {"nan": float("nan"), "inf": float("inf")}


class ExprError(ValueError):
    pass


@dataclass(frozen=True)
class Expr:
    source: str
    tree: ast.Expression
    #: Every frame column or feature name the expression reads, in first-use order.
    names: tuple[str, ...]


def parse(source: str, constants: dict) -> Expr:
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"{source!r}: {e.msg}") from None
    tree = ast.fix_missing_locations(_Fold(source, constants).visit(tree))
    names: list[str] = []
    _check(tree.body, source, names)
    return Expr(source, tree, tuple(dict.fromkeys(names)))


def infer_shape(expr: Expr, shape_of: Callable[[str], str]) -> str:
    return _shape(expr.tree.body, expr.source, shape_of)


def evaluate(expr: Expr, lookup: Callable[[str], np.ndarray], n_rows: int) -> np.ndarray:
    # IEEE semantics on purpose (x/0 -> inf, log(0) -> -inf): the feature code being
    # replaced relied on them, and a spec states its own guards (maximum(), where()).
    with np.errstate(all="ignore"):
        value = _eval(expr.tree.body, lookup)
    if np.ndim(value) == 0:
        return np.full(n_rows, value, dtype=np.float64)
    return value


class _Fold(ast.NodeTransformer):
    def __init__(self, source: str, constants: dict):
        self.source, self.constants = source, constants

    def visit_Name(self, node: ast.Name):
        if node.id in _FLOAT_NAMES:
            return ast.copy_location(ast.Constant(_FLOAT_NAMES[node.id]), node)
        if node.id not in self.constants:
            return node
        value = self.constants[node.id]
        if isinstance(value, list):
            raise ExprError(f"{self.source!r}: list var {node.id!r} can only be used as "
                            f"cols(..., <key>={node.id})")
        return ast.copy_location(ast.Constant(value), node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "cols":
            return ast.copy_location(self._expand_cols(node), node)
        for kw in node.keywords:  # a list var is a valid keyword value, e.g. x=days
            if isinstance(kw.value, ast.Name) and isinstance(self.constants.get(kw.value.id), list):
                kw.value = ast.List([ast.Constant(v) for v in self.constants[kw.value.id]], ast.Load())
        return self.generic_visit(node)

    def _expand_cols(self, node: ast.Call) -> ast.Call:
        if (len(node.args) != 1 or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str) or len(node.keywords) != 1):
            raise ExprError(f"{self.source!r}: cols() takes one pattern string and one "
                            f"keyword, e.g. cols('val_day_{{d}}', d=days)")
        pattern, kw = node.args[0].value, node.keywords[0]
        values = self._sequence(kw.value)
        if "{" + kw.arg + "}" not in pattern:
            raise ExprError(f"{self.source!r}: cols pattern {pattern!r} never uses {{{kw.arg}}}")
        columns = [pattern.format(**{kw.arg: v}) for v in values]
        return ast.Call(func=ast.Name("cols", ast.Load()),
                        args=[ast.Name(c, ast.Load()) for c in columns], keywords=[])

    def _sequence(self, node: ast.AST) -> list:
        if isinstance(node, ast.Name) and isinstance(self.constants.get(node.id), list):
            return list(self.constants[node.id])
        if isinstance(node, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) for e in node.elts):
            return [e.value for e in node.elts]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "range" and not node.keywords
                and all(isinstance(a, ast.Constant) and isinstance(a.value, int) for a in node.args)):
            return list(range(*(a.value for a in node.args)))
        raise ExprError(f"{self.source!r}: cols() values must be a list var, a literal list, "
                        f"or range(a, b)")


def _check(node: ast.AST, source: str, names: list[str]) -> None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ExprError(f"{source!r}: only numeric constants are allowed, got {node.value!r}")
    elif isinstance(node, ast.Name):
        if node.id in OPS:
            raise ExprError(f"{source!r}: {node.id!r} is an op, not a value")
        names.append(node.id)
    elif isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        _check(node.left, source, names)
        _check(node.right, source, names)
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        _check(node.operand, source, names)
    elif isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMPOPS:
        _check(node.left, source, names)
        _check(node.comparators[0], source, names)
    elif isinstance(node, ast.Call):
        _check_call(node, source, names)
    else:
        raise ExprError(f"{source!r}: {type(node).__name__} is not allowed in an expr")


def _check_call(node: ast.Call, source: str, names: list[str]) -> None:
    if not isinstance(node.func, ast.Name) or node.func.id not in OPS:
        shown = node.func.id if isinstance(node.func, ast.Name) else ast.unparse(node.func)
        raise ExprError(f"{source!r}: unknown op {shown!r}; allowed: {sorted(OPS)}")
    op = OPS[node.func.id]
    if op.kind != COLS:
        lo, hi = op.n_args
        if not lo <= len(node.args) <= hi:
            raise ExprError(f"{source!r}: {node.func.id}() takes {lo}"
                            f"{'' if lo == hi else f'-{hi}'} positional argument(s)")
        for kw in node.keywords:
            if kw.arg not in op.keywords:
                raise ExprError(f"{source!r}: {node.func.id}() has no keyword {kw.arg!r}")
            _const(kw.value, source)
    for arg in node.args:
        _check(arg, source, names)


def _const(node: ast.AST, source: str):
    """A keyword argument's value: a number, or a list of them."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_const(node.operand, source)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(_const(e, source) for e in node.elts)
    raise ExprError(f"{source!r}: keyword arguments must be numbers or lists of numbers")


def _shape(node: ast.AST, source: str, shape_of: Callable[[str], str]) -> str:
    if isinstance(node, ast.Constant):
        return SCALAR
    if isinstance(node, ast.Name):
        return shape_of(node.id)
    if isinstance(node, ast.BinOp):
        return broadcast_shape(_shape(node.left, source, shape_of), _shape(node.right, source, shape_of))
    if isinstance(node, ast.UnaryOp):
        return _shape(node.operand, source, shape_of)
    if isinstance(node, ast.Compare):
        return broadcast_shape(_shape(node.left, source, shape_of),
                               _shape(node.comparators[0], source, shape_of))
    op = OPS[node.func.id]
    arg_shapes = [_shape(a, source, shape_of) for a in node.args]
    if op.kind == COLS:
        if any(s != VECTOR for s in arg_shapes):
            raise ExprError(f"{source!r}: cols() can only stack per-row columns")
        return MATRIX
    if op.kind in (ROW_REDUCE, ROW_MAP):
        if arg_shapes[0] != MATRIX:
            raise ExprError(f"{source!r}: {node.func.id}() works on a row of a matrix; "
                            f"its argument is a {arg_shapes[0]} (build one with cols())")
        return VECTOR if op.kind == ROW_REDUCE else MATRIX
    return broadcast_shape(*arg_shapes)


def _eval(node: ast.AST, lookup: Callable[[str], np.ndarray]):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return lookup(node.id)
    if isinstance(node, ast.BinOp):
        return _BINOPS[type(node.op)](*align(_eval(node.left, lookup), _eval(node.right, lookup)))
    if isinstance(node, ast.UnaryOp):
        return _UNARYOPS[type(node.op)](_eval(node.operand, lookup))
    if isinstance(node, ast.Compare):
        return _CMPOPS[type(node.ops[0])](*align(_eval(node.left, lookup),
                                                 _eval(node.comparators[0], lookup)))
    op = OPS[node.func.id]
    args = [_eval(a, lookup) for a in node.args]
    kwargs = {kw.arg: _const(kw.value, "") for kw in node.keywords}
    return op.impl(*args, **kwargs)
