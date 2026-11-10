"""The base operations an `expr` may call, with the shape rule each one follows.

Every op here works within a row: an elementwise op maps each cell on its own, a row
reduction collapses one row's matrix into that row's scalar, and a row map turns a row
into another row (residuals, differences, a slice). Nothing reduces ACROSS
rows, so an expression's value for a segment never depends on which other segments share
the batch -- that is what lets serving score in chunks. A statistic across rows is a
`fit:` entry instead (see steps.py), computed once at fit time and frozen.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SCALAR, VECTOR, MATRIX = "scalar", "vector", "matrix"
_RANK = {SCALAR: 0, VECTOR: 1, MATRIX: 2}

#: row_reduce: matrix -> one value per row. row_map: matrix -> matrix, each row on its own.
ELEMENTWISE, ROW_REDUCE, ROW_MAP, COLS = "elementwise", "row_reduce", "row_map", "cols"


@dataclass(frozen=True)
class Op:
    impl: Callable
    kind: str
    n_args: tuple[int, int]
    keywords: tuple[str, ...] = ()


def align(*arrays):
    """numpy broadcasts a (N,) against a (N, k) along the LAST axis; a per-row vector has
    to line up with a matrix's rows instead."""
    if not any(np.ndim(a) == 2 for a in arrays):
        return arrays
    return tuple(a[:, None] if np.ndim(a) == 1 else a for a in arrays)


def broadcast_shape(*shapes: str) -> str:
    return max(shapes, key=_RANK.__getitem__) if shapes else SCALAR


def _where(cond, a, b):
    return np.where(*align(cond, a, b))


def _clip(x, lo=None, hi=None):
    if lo is not None:
        x = np.maximum(x, lo)
    if hi is not None:
        x = np.minimum(x, hi)
    return x


def _binary(fn):
    return lambda a, b: fn(*align(a, b))


def _fillna(x, value):
    return np.where(np.isnan(x), value, x)


# Row-wise curve fits against x: by default each column's position 0..k-1, or the days
# the columns stand for (e.g. x=[1, 3, 7, 10, 14]).

def _x(m, x):
    if x is None:
        return np.arange(m.shape[1], dtype=float)
    x = np.asarray(x, dtype=float)
    if x.shape != (m.shape[1],):
        raise ValueError(f"feature_manager: x has {len(x)} values for a {m.shape[1]}-column matrix")
    return x


def _row_slope(m, x=None, eps=0.0):
    """Least-squares slope of each row against x. eps joins the denominator, where the
    feature being stated guarded it that way."""
    xc = _x(m, x) - _x(m, x).mean()
    return (m - m.mean(axis=1, keepdims=True)) @ xc / (xc @ xc + eps)


def _row_detrend(m, x=None):
    """Residuals of each row's least-squares line against x."""
    x = _x(m, x)
    b = _row_slope(m, x)
    a = m.mean(axis=1) - b * x.mean()
    return m - (a[:, None] + b[:, None] * x)


def _poly_coef(m, deg, x):
    a = np.vander(_x(m, x), int(deg) + 1, increasing=True)
    return m @ a @ np.linalg.inv(a.T @ a), a


def _row_polyfit(m, deg=1, k=1, x=None):
    """Coefficient k (of x**k) of each row's degree-`deg` least-squares polynomial."""
    return _poly_coef(m, deg, x)[0][:, int(k)]


def _row_polyfit_r2(m, deg=1, x=None, eps=1e-12):
    """R^2 of each row's degree-`deg` least-squares polynomial; 0 for a flat row."""
    c, a = _poly_coef(m, deg, x)
    ss_res = np.square(m - c @ a.T).sum(axis=1)
    ss_tot = np.square(m - m.mean(axis=1, keepdims=True)).sum(axis=1)
    return np.where(ss_tot > eps, 1.0 - ss_res / np.maximum(ss_tot, eps), 0.0)


def _row_argmax(m, last=0):
    """Column position of each row's max -- the first one, or with last=1 the last one
    (on a boolean matrix: the first/last True)."""
    if last:
        return (m.shape[1] - 1 - np.argmax(m[:, ::-1], axis=1)).astype(np.float64)
    return np.argmax(m, axis=1).astype(np.float64)


def _row_gini(m, eps=1e-12):
    """Gini of each row's values; 0 for a row summing below eps."""
    v = np.sort(m, axis=1)
    n = m.shape[1]
    s = v.sum(axis=1)
    g = 2 * (v * np.arange(1, n + 1)).sum(axis=1) / (n * s) - (n + 1) / n
    return np.where(s < eps, 0.0, g)


def _col_slice(m, start=0, stop=None):
    return m[:, int(start):None if stop is None else int(stop)]


OPS: dict[str, Op] = {
    "log": Op(np.log, ELEMENTWISE, (1, 1)),
    "log1p": Op(np.log1p, ELEMENTWISE, (1, 1)),
    "exp": Op(np.exp, ELEMENTWISE, (1, 1)),
    "expm1": Op(np.expm1, ELEMENTWISE, (1, 1)),
    "sqrt": Op(np.sqrt, ELEMENTWISE, (1, 1)),
    "abs": Op(np.abs, ELEMENTWISE, (1, 1)),
    "tanh": Op(np.tanh, ELEMENTWISE, (1, 1)),
    "isnan": Op(np.isnan, ELEMENTWISE, (1, 1)),
    "fillna": Op(_fillna, ELEMENTWISE, (2, 2)),
    "where": Op(_where, ELEMENTWISE, (3, 3)),
    "maximum": Op(_binary(np.maximum), ELEMENTWISE, (2, 2)),
    "minimum": Op(_binary(np.minimum), ELEMENTWISE, (2, 2)),
    "clip": Op(_clip, ELEMENTWISE, (1, 1), ("lo", "hi")),
    # Rounds to float32; arithmetic on the result stays float32, for features first
    # computed at that precision.
    "float32": Op(lambda x: np.asarray(x, dtype=np.float32), ELEMENTWISE, (1, 1)),
    # Back to float64: where the feature being stated promoted a float32 value by mixing
    # in a float64 one (a numpy float64 constant does; a plain number does not).
    "float64": Op(lambda x: np.asarray(x, dtype=np.float64), ELEMENTWISE, (1, 1)),
    "row_sum": Op(lambda m: np.sum(m, axis=1), ROW_REDUCE, (1, 1)),
    "row_mean": Op(lambda m: np.mean(m, axis=1), ROW_REDUCE, (1, 1)),
    "row_max": Op(lambda m: np.max(m, axis=1), ROW_REDUCE, (1, 1)),
    "row_min": Op(lambda m: np.min(m, axis=1), ROW_REDUCE, (1, 1)),
    "row_std": Op(lambda m, ddof=0: np.std(m, axis=1, ddof=ddof), ROW_REDUCE, (1, 1), ("ddof",)),
    "row_slope": Op(_row_slope, ROW_REDUCE, (1, 1), ("x", "eps")),
    "row_polyfit": Op(_row_polyfit, ROW_REDUCE, (1, 1), ("deg", "k", "x")),
    "row_polyfit_r2": Op(_row_polyfit_r2, ROW_REDUCE, (1, 1), ("deg", "x", "eps")),
    "row_gini": Op(_row_gini, ROW_REDUCE, (1, 1), ("eps",)),
    "row_argmax": Op(_row_argmax, ROW_REDUCE, (1, 1), ("last",)),
    "row_detrend": Op(_row_detrend, ROW_MAP, (1, 1), ("x",)),
    "row_diff": Op(lambda m: np.diff(m, axis=1), ROW_MAP, (1, 1)),
    "col_slice": Op(_col_slice, ROW_MAP, (1, 1), ("start", "stop")),
    # cols("prefix_day_{d}", d=days) -> (N, len(days)). Its column names are resolved at
    # compile time (expr.py rewrites the call), so impl only stacks what it is handed.
    "cols": Op(lambda *columns: np.column_stack(columns), COLS, (1, 1), ("*",)),
}
