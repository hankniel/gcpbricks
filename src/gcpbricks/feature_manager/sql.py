"""Lower exprs to SQL that gives what numpy gives (ops.py, expr.py).

The stateless part of a spec (`transforms`, an aggregation's input formula) runs in the
warehouse, so each op here is numpy's semantics written out for SQL: a missing value is
NaN (a column read turns a NULL into NaN); a comparison with NaN is False (True for !=);
division, logs, roots and exp follow IEEE where SQL would raise or return NULL.
tests/test_feature_sql.py holds every op to numpy on each engine.

A program is SSA: every op's result is a temporary column, computed in the layer after
the columns it reads, so an op reads its arguments as plain column references and the SQL
grows with the number of ops, not with how deeply they nest. A layer is
`SELECT *, <its columns> FROM (<the layer before>)` (sqlx.py): the engine drops what
nothing reads, and the query's text carries no column list per layer.

A value is a Python number (a constant), a V (one column's expression), or a list of
them (a matrix: cols() is a fixed list of columns, so every row_* op is a fixed
expression over them).
"""
from __future__ import annotations

import ast
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .expr import Expr, ExprError, _const
from .ops import OPS

NAN, INF = float("nan"), float("inf")
#: exp(x) overflows a float64 above this; SQL engines raise there, numpy gives inf.
_EXP_MAX = 709.782712893384
#: Every op with a SQL form here; the rest (row_gini, row_polyfit_r2, float32) are
#: Python only, and so is `%` (numpy's remainder is exact; BigQuery has no FLOAT64 MOD).
SQL_OPS = frozenset({
    "log", "log1p", "exp", "expm1", "sqrt", "abs", "tanh", "isnan", "fillna", "where",
    "maximum", "minimum", "clip", "float64", "cols", "row_sum", "row_mean", "row_max",
    "row_min", "row_std", "row_slope", "row_polyfit", "row_argmax", "row_diff",
    "col_slice", "row_detrend",
})
#: Exponents `**` lowers exactly: numpy's x**2 is x*x, x**0.5 is sqrt(x).
_POWERS = (-1, 0.5, 1, 2, 3, 4)


def unsupported(expr: Expr) -> list[str]:
    """What keeps `expr` out of SQL; empty when it lowers."""
    out = []
    for node in ast.walk(expr.tree):
        if isinstance(node, ast.Call) and node.func.id not in SQL_OPS:
            out.append(f"{node.func.id}()")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            out.append("% (numpy's remainder is exact; BigQuery has no FLOAT64 MOD)")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            try:
                power = _const(node.right, expr.source)
            except ExprError:
                power = None
            if power not in _POWERS:
                out.append(f"** {ast.unparse(node.right)} (only {', '.join(map(str, _POWERS))})")
    return list(dict.fromkeys(out))


# --- values ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class V:
    """One column's expression (an Ibis Deferred over named columns): `boolean` for a
    comparison's result, `level` the layer whose table has every column it reads."""

    expr: object
    boolean: bool = False
    level: int = 0
    reads: frozenset = field(default_factory=frozenset)
    #: A plain column reference: reading it again costs nothing.
    ref: bool = False
    #: About how many nodes its expression has: an op reads its arguments several times,
    #: so past INLINE one becomes a column of its own instead of being copied.
    size: int = 1


def _is_py(v) -> bool:
    return isinstance(v, (bool, int, float, np.bool_, np.integer, np.floating))


def _v(x) -> V:
    """A constant stays a Python number: an Ibis literal on the left of a Deferred does
    not combine with it, a number does (the Deferred's reflected op)."""
    if isinstance(x, V):
        return x
    if isinstance(x, (bool, np.bool_)):
        return V(bool(x), True)
    return V(float(x))


def _join(f, *args, boolean=False) -> V:
    vs = [_v(a) for a in args]
    return V(f(*(v.expr for v in vs)), boolean, max(v.level for v in vs),
             frozenset().union(*(v.reads for v in vs)), size=1 + sum(v.size for v in vs))


def _num(v):
    """A number; a boolean as 1.0/0.0 (BigQuery refuses CAST(BOOL AS FLOAT64))."""
    from . import sqlx as X

    if _is_py(v):
        return float(v)
    return V(X.ifelse(v.expr, 1.0, 0.0), False, v.level, v.reads, size=v.size + 2) if v.boolean else v


def _isnan(v):
    if _is_py(v):
        return bool(math.isnan(float(v)))
    return False if v.boolean else _join(lambda e: e.isnan(), v, boolean=True)


def _isinf(v):
    # Not Ibis's isinf(): its PySpark form is the constant FALSE.
    if _is_py(v):
        return bool(math.isinf(float(v)))
    return _join(lambda e: (e == INF) | (e == -INF), v, boolean=True)


def _or(a, b):
    if _is_py(a) and _is_py(b):
        return bool(a) or bool(b)
    if _is_py(a):
        return True if a else b
    if _is_py(b):
        return True if b else a
    return _join(lambda x, y: x | y, a, b, boolean=True)


def _and(a, b):
    if _is_py(a) and _is_py(b):
        return bool(a) and bool(b)
    if _is_py(a):
        return b if a else False
    if _is_py(b):
        return a if b else False
    return _join(lambda x, y: x & y, a, b, boolean=True)


def _not(a):
    return (not a) if _is_py(a) else _join(lambda x: ~x, a, boolean=True)


def _if(cond, a, b):
    from . import sqlx as X

    if _is_py(cond):
        return a if cond else b
    a, b = _v(a), _v(b)
    if a.boolean != b.boolean:
        a, b = _num(a), _num(b)
    return _join(lambda c, x, y: X.ifelse(c, x, y), cond, a, b, boolean=a.boolean)


def _truthy(v):
    """numpy's truth of a value: nonzero, NaN included."""
    if _is_py(v):
        return bool(v) or bool(math.isnan(float(v)))
    if v.boolean:
        return v
    return _or(_isnan(v), _join(lambda e: e != 0, v, boolean=True))


def _broadcast(f, *args):
    """f applied cell by cell: a vector or scalar against each column of a matrix."""
    width = next((len(a) for a in args if isinstance(a, list)), None)
    if width is None:
        return f(*args)
    cols = [a if isinstance(a, list) else [a] * width for a in args]
    if any(len(c) != width for c in cols):
        raise ExprError("matrices of different widths")
    return [f(*cells) for cells in zip(*cols)]


# --- the program -----------------------------------------------------------------------------

#: The expression size past which a value is a column of its own; 0: every op's result
#: is one. A copied expression is copied again by each reader, and a layer's columns cost
#: the query nothing (SELECT *), so a column is the cheaper of the two.
INLINE = 0


class Program:
    """Named temporary columns, each a V over base columns and earlier temporaries."""

    def __init__(self, prefix: str = "_t", inline: int = INLINE):
        self.prefix, self.temps, self._n, self.inline = prefix, {}, 0, inline

    def bind(self, v, name: str | None = None):
        """v as a column of its own (a reference to it), unless it already is one or is
        a constant."""
        from . import sqlx as X

        if isinstance(v, list):
            return [self.bind(c) for c in v]
        if _is_py(v) or (name is None and (v.ref or v.size <= self.inline)):
            return v
        if name is None:
            name, self._n = f"{self.prefix}{self._n}", self._n + 1
        self.temps[name] = v
        return V(X.col(name), v.boolean, v.level + 1, frozenset({name}), ref=True)

    def sql(self, source, keep: list[str]):
        """`source` (a sqlglot query) with every temporary computed, layer by layer: a
        query whose columns are `keep` (source columns or temporaries)."""
        from sqlglot import exp

        from . import sqlx as X

        by_level: dict[int, dict[str, V]] = {}
        for name, v in self.temps.items():
            by_level.setdefault(v.level, {})[name] = v
        current = source
        for level in sorted(by_level):
            new = [exp.alias_(X.q(v.expr), n, quoted=True) for n, v in by_level[level].items()]
            current = exp.select(exp.Star(), *new).from_(current.subquery(f"_l{level}"))
        return exp.select(*[exp.column(c, quoted=True) for c in keep]).from_(current.subquery("_out"))


def column(name: str, boolean: bool = False) -> V:
    """A base column's read: numeric, a NULL as NaN (a boolean as itself)."""
    from . import sqlx as X

    if boolean:
        return V(X.col(name), True, 0, frozenset({name}), ref=True)
    return V(X.coalesce(X.col(name).cast("float64"), NAN), False, 0, frozenset({name}))


class Lowering:
    """Lowers exprs into one Program; a name resolves through `lookup` (a base column's
    read, or an earlier entry's value)."""

    def __init__(self, program: Program, lookup: Callable[[str], object]):
        self.p, self.lookup = program, lookup

    def lower(self, expr: Expr):
        bad = unsupported(expr)
        if bad:
            raise ExprError(f"{expr.source!r}: not in SQL: {bad}")
        return self._lower(expr.tree.body)

    def _lower(self, node: ast.AST):
        b = self.p.bind
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.lookup(node.id)
        if isinstance(node, ast.BinOp):
            left, right = self._lower(node.left), self._lower(node.right)
            if isinstance(node.op, ast.Pow):
                power = _const(node.right, "")
                return b(_broadcast(lambda v: self._pow(v, power), left))
            return b(_broadcast(_BIN[type(node.op)], left, right))
        if isinstance(node, ast.UnaryOp):
            v = self._lower(node.operand)
            if isinstance(node.op, ast.UAdd):
                return v
            if isinstance(node.op, ast.USub):
                return b(_broadcast(lambda x: -float(x) if _is_py(x) else _join(lambda e: -e, _num(x)), v))
            return b(_broadcast(lambda x: _not(_truthy(x)), v))
        if isinstance(node, ast.Compare):
            return b(_broadcast(_CMP[type(node.ops[0])], self._lower(node.left),
                                self._lower(node.comparators[0])))
        args = [self._lower(a) for a in node.args]
        kwargs = {kw.arg: _const(kw.value, "") for kw in node.keywords}
        name = node.func.id
        if name == "cols":
            return list(args)
        if name in _ROW:
            return b(_ROW[name](self, *args, **kwargs))
        return b(_broadcast(lambda *cells: _ELEMENT[name](self, *cells, **kwargs), *args))

    def _pow(self, a, power):
        if power == 1:
            return _num(a)
        if power == 0.5:
            return _sqrt(self, a)
        if power == -1:
            return _div(1.0, a)
        out = _num(a)
        for _ in range(int(power) - 1):
            out = self.p.bind(_mul(out, a))
        return out


# --- elementwise ------------------------------------------------------------------------------

def _cmp(op: str):
    py = {"lt": np.less, "le": np.less_equal, "gt": np.greater, "ge": np.greater_equal,
          "eq": np.equal, "ne": np.not_equal}[op]
    sql = {"lt": lambda x, y: x < y, "le": lambda x, y: x <= y, "gt": lambda x, y: x > y,
           "ge": lambda x, y: x >= y, "eq": lambda x, y: x == y, "ne": lambda x, y: x != y}[op]

    def f(a, b):
        if _is_py(a) and _is_py(b):
            with np.errstate(all="ignore"):
                return bool(py(float(a), float(b)))
        nan = _or(_isnan(a), _isnan(b))
        test = _join(sql, _num(a), _num(b), boolean=True)
        return _if(nan, op == "ne", test)
    return f


_gt, _lt, _eq, _ge = _cmp("gt"), _cmp("lt"), _cmp("eq"), _cmp("ge")


def _arith(np_fn, sql):
    def f(a, b):
        if _is_py(a) and _is_py(b):
            with np.errstate(all="ignore"):
                return float(np_fn(float(a), float(b)))
        return _join(sql, _num(a), _num(b))
    return f




def _overflowing(op):
    """x op y, +-inf where it overflows: BigQuery raises "double overflow" there."""
    def guarded(x, y):
        from . import sqlx as X

        return X.overflowing(op, x, y)
    return guarded


_raw_add = _arith(np.add, _overflowing("add"))
_raw_mul = _arith(np.multiply, _overflowing("mul"))


def _add(a, b):
    return _raw_add(a, b)


def _sub(a, b):
    return _raw_add(a, -float(b) if _is_py(b) else _join(lambda e: -e, _num(b)))


def _mul(a, b):
    return _raw_mul(a, b)


def _div(a, b):
    """IEEE: x/0 is inf with the sign of x times the sign of the zero; 0/0 and NaN NaN."""
    if _is_py(a) and _is_py(b):
        with np.errstate(all="ignore"):
            return float(np.divide(float(a), float(b)))
    zero = _eq(b, 0.0)
    if _is_py(zero) and not zero:
        return _join(lambda x, y: x / y, _num(a), _num(b))
    # atan2(-0.0, -1) is -pi: a zero's sign, read without a string cast. Spark's atan2
    # is +pi for both zeros, so there x / -0.0 is +inf.
    neg_zero = (math.copysign(1.0, b) < 0) if _is_py(b) else _join(
        lambda e: e.atan2(-1.0) < 0, _num(b), boolean=True)
    pos, neg = _if(neg_zero, -INF, INF), _if(neg_zero, INF, -INF)
    by_zero = _if(_gt(a, 0.0), pos, _if(_lt(a, 0.0), neg, NAN))
    return _if(zero, by_zero, _join(lambda x, y: x / y, _num(a), _if(zero, 1.0, b)))


def _const_fn(np_fn):
    """A one-argument op: folded by numpy on a constant, else lowered on its number."""
    def wrap(f):
        def g(low, x, **kw):
            if _is_py(x):
                with np.errstate(all="ignore"):
                    return float(np_fn(float(x)))
            return f(low, _num(x), **kw)
        return g
    return wrap


@_const_fn(np.log)
def _log(low, x):
    from . import sqlx as X

    pos = low.p.bind(_and(_gt(x, 0.0), _not(_isinf(x))))
    safe = _join(lambda c, e: X.ifelse(c, e, 1.0).ln(), pos, x)
    return _if(_eq(x, INF), INF, _if(pos, safe, _if(_eq(x, 0.0), -INF, NAN)))


@_const_fn(np.log1p)
def _log1p(low, x):
    """ln(1+x), accurate for tiny x (Kahan): ln(u) * x / (u - 1), u = 1 + x."""
    from . import sqlx as X

    inside = low.p.bind(_and(_gt(x, -1.0), _not(_isinf(x))))
    u = low.p.bind(_join(lambda c, e: X.ifelse(c, 1.0 + e, 2.0), inside, x))
    exact = _join(lambda uu, e: X.ifelse(
        uu == 1.0, e, uu.ln() * (e / X.ifelse(uu == 1.0, 1.0, uu - 1.0))), u, x)
    return _if(_eq(x, INF), INF, _if(inside, exact, _if(_eq(x, -1.0), -INF, NAN)))


@_const_fn(np.exp)
def _exp(low, x):
    from . import sqlx as X

    over, under = low.p.bind(_gt(x, _EXP_MAX)), low.p.bind(_lt(x, -_EXP_MAX - 40))
    safe = _join(lambda o, u, e: X.ifelse(o | u, 0.0, e).exp(), over, under, x)
    return _if(over, INF, _if(under, 0.0, _if(_isnan(x), NAN, safe)))


@_const_fn(np.expm1)
def _expm1(low, x):
    """exp(x) - 1, accurate for tiny x (Kahan): (u - 1) * x / ln(u), u = exp(x)."""
    from . import sqlx as X

    u = low.p.bind(_exp(low, x))
    finite = low.p.bind(_not(_or(_isinf(u), _isnan(u))))
    mid = low.p.bind(_and(finite, _join(lambda e: (e != 1.0) & (e != 0.0), u, boolean=True)))
    exact = _join(lambda m, uu, e: (X.ifelse(m, uu, 2.0) - 1.0) * (e / X.ifelse(m, uu, 2.0).ln()),
                  mid, u, x)
    inner = _join(lambda uu, e, ex: X.ifelse(uu == 1.0, e, X.ifelse(uu == 0.0, -1.0, ex)),
                  u, x, exact)
    return _if(finite, inner, _if(_eq(u, INF), INF, NAN))


@_const_fn(np.sqrt)
def _sqrt(low, x):
    from . import sqlx as X

    ok = low.p.bind(_ge(x, 0.0))
    return _if(ok, _join(lambda c, e: X.ifelse(c, e, 0.0).sqrt(), ok, x), NAN)


@_const_fn(np.tanh)
def _tanh(low, x):
    """expm1(2x) / (expm1(2x) + 2): no engine here has a tanh that matches numpy's."""
    from . import sqlx as X

    big = low.p.bind(_gt(_join(lambda e: e.abs(), x), 20.0))
    small = low.p.bind(_join(lambda c, e: X.ifelse(c, 0.0, e) * 2.0, big, x))
    e = low.p.bind(_expm1(low, small))
    return _if(big, _join(lambda v: X.ifelse(v > 0, 1.0, -1.0), x), _div(e, _add(e, 2.0)))


@_const_fn(np.abs)
def _abs(low, x):
    return _join(lambda e: e.abs(), x)


def _fillna(low, x, value):
    return _if(_isnan(x), value, x)


def _maximum(low, a, b):
    return _if(_or(_isnan(a), _isnan(b)), NAN, _if(_ge(a, b), a, b))


def _minimum(low, a, b):
    return _if(_or(_isnan(a), _isnan(b)), NAN, _if(_ge(b, a), a, b))


def _clip(low, x, lo=None, hi=None):
    if lo is not None:
        x = low.p.bind(_maximum(low, x, lo))
    if hi is not None:
        x = _minimum(low, x, hi)
    return x


_ELEMENT: dict[str, Callable] = {
    "log": _log, "log1p": _log1p, "exp": _exp, "expm1": _expm1, "sqrt": _sqrt,
    "abs": _abs, "tanh": _tanh, "isnan": lambda low, x: _isnan(x), "fillna": _fillna,
    "where": lambda low, c, a, b: _if(_truthy(c), a, b), "maximum": _maximum,
    "minimum": _minimum, "clip": _clip, "float64": lambda low, x: _num(x),
}


# --- rows of a matrix -------------------------------------------------------------------------

def _xs(m: list, x) -> np.ndarray:
    if x is None:
        return np.arange(len(m), dtype=float)
    x = np.asarray(x, dtype=float)
    if x.shape != (len(m),):
        raise ExprError(f"x has {len(x)} values for a {len(m)}-column matrix")
    return x


def _sum(cells, low=None):
    out = cells[0]
    for c in cells[1:]:
        out = _add(out, c)
        if low is not None:
            out = low.p.bind(out)
    return out


def _row_mean(low, m):
    return _div(_sum(m, low), float(len(m)))


def _any_nan(m):
    out = _isnan(m[0])
    for c in m[1:]:
        out = _or(out, _isnan(c))
    return out


def _row_extreme(low, m, pick):
    """numpy's max/min: NaN when any cell is; GREATEST/LEAST otherwise, which then sees
    no NaN."""
    from . import sqlx as X

    if all(_is_py(c) for c in m):
        return float((np.max if pick == "max" else np.min)([float(c) for c in m]))
    fn = X.greatest if pick == "max" else X.least
    return _if(_any_nan(m), NAN, _join(lambda *es: fn(*es), *[_num(c) for c in m]))


def _row_std(low, m, ddof=0):
    mean = low.p.bind(_row_mean(low, m))
    dev = [low.p.bind(_sub(c, mean)) for c in m]
    squares = [low.p.bind(_mul(d, d)) for d in dev]
    return _sqrt(low, low.p.bind(_div(_sum(squares, low), float(len(m) - ddof))))


def _row_slope(low, m, x=None, eps=0.0):
    xc = _xs(m, x) - _xs(m, x).mean()
    mean = low.p.bind(_row_mean(low, m))
    terms = [low.p.bind(_mul(low.p.bind(_sub(c, mean)), float(w))) for c, w in zip(m, xc)]
    return _div(_sum(terms, low), float(xc @ xc + eps))


def _row_polyfit(low, m, deg=1, k=1, x=None):
    a = np.vander(_xs(m, x), int(deg) + 1, increasing=True)
    inv = np.linalg.inv(a.T @ a)
    # numpy's order: (m @ a) @ inv.
    t = [low.p.bind(_sum([low.p.bind(_mul(c, float(a[i, j]))) for i, c in enumerate(m)], low))
         for j in range(a.shape[1])]
    return _sum([low.p.bind(_mul(tj, float(inv[j, int(k)]))) for j, tj in enumerate(t)], low)


def _row_argmax(low, m, last=0):
    """np.argmax: the first maximum's position, a NaN counting as the maximum; with
    last=1, the last's."""
    cells = list(reversed(m)) if last else list(m)
    n = len(cells)
    pos = (lambda i: float(n - 1 - i)) if last else float
    best = low.p.bind(_row_extreme(low, cells, "max"))
    first_nan, first_max = NAN, NAN
    for i in reversed(range(n)):
        first_nan = _if(_isnan(cells[i]), pos(i), first_nan)
        first_max = _if(_eq(cells[i], best), pos(i), first_max)
    return _if(_any_nan(cells), first_nan, first_max)


def _row_detrend(low, m, x=None):
    xs = _xs(m, x)
    b = low.p.bind(_row_slope(low, m, xs))
    a = low.p.bind(_sub(_row_mean(low, m), _mul(b, float(xs.mean()))))
    return [_sub(c, low.p.bind(_add(a, low.p.bind(_mul(b, float(xi)))))) for c, xi in zip(m, xs)]


_ROW: dict[str, Callable] = {
    "row_sum": lambda low, m: _sum(m, low), "row_mean": _row_mean,
    "row_max": lambda low, m: _row_extreme(low, m, "max"),
    "row_min": lambda low, m: _row_extreme(low, m, "min"),
    "row_std": _row_std, "row_slope": _row_slope, "row_polyfit": _row_polyfit,
    "row_argmax": _row_argmax, "row_detrend": _row_detrend,
    "row_diff": lambda low, m: [_sub(m[i + 1], m[i]) for i in range(len(m) - 1)],
    "col_slice": lambda low, m, start=0, stop=None: m[int(start):None if stop is None else int(stop)],
}
assert set(_ROW) | set(_ELEMENT) | {"cols"} == SQL_OPS <= set(OPS)

_BIN = {ast.Add: _add, ast.Sub: _sub, ast.Mult: _mul, ast.Div: _div,
        ast.BitAnd: lambda a, b: _and(_truthy(a), _truthy(b)),
        ast.BitOr: lambda a, b: _or(_truthy(a), _truthy(b))}
_CMP = {ast.Lt: _cmp("lt"), ast.LtE: _cmp("le"), ast.Gt: _cmp("gt"), ast.GtE: _cmp("ge"),
        ast.Eq: _cmp("eq"), ast.NotEq: _cmp("ne")}


# --- a plan's SQL stage ---------------------------------------------------------------------

def series_width(window: dict) -> int:
    """How many windows a series is: series_start / step_size (step_size: window_size)."""
    start, step = _days(window["series_start"]), _days(window.get("step_size") or window["window_size"])
    if (-start) % step:
        raise ExprError(f"series_start {window['series_start']} is not a whole number of "
                        f"step_size {window.get('step_size') or window['window_size']}")
    return -start // step


def _days(text: str) -> int:
    return 0 if text == "0" else int(text[:-1])


def stage_program(stage, read: Callable[[str], V]) -> tuple[Program, list[str]]:
    """A Program computing a SqlStage's `needed` values over a table that has the raw
    columns `read` gives and each aggregation's columns (stage.prefix + name, a series
    as prefix + name + "__<i>"). Returns it and the columns it leaves: `needed`'s."""
    program, values, reads = Program("_s"), {}, {}

    def lookup(name):
        if name in values:
            return values[name]
        if name not in reads:
            reads[name] = program.bind(read(name))
        return reads[name]

    low, out = Lowering(program, lookup), []
    needed = set(stage.needed)
    for node in stage.nodes:
        col = node.params["sql_column"]
        if node.kind == "aggregate":
            width = series_width(node.params["window"]) if node.shape == "matrix" else None
            names = [col] if width is None else [f"{col}__{i}" for i in range(width)]
            cells = [program.bind(column(n)) for n in names]
            values[node.name] = cells[0] if width is None else cells
            if node.name in needed:
                out += names
            continue
        v = low.lower(node.expr)
        if node.name in needed:
            if isinstance(v, list):
                v = [program.bind(_v(c), name=f"{col}__{i}") for i, c in enumerate(v)]
                out += [f"{col}__{i}" for i in range(len(v))]
            else:
                v = program.bind(_v(v), name=col)
                out.append(col)
        values[node.name] = v
    return program, out


def compute_stage(stage, df, *, drop_sql_only: bool = True):
    """The stage's values over a DataFrame, on DuckDB in-process, as the feature store would
    hand them back: df's columns plus the stage's (stage.prefix + name), less the raw
    columns only SQL read when drop_sql_only. For tests and golden fixtures; a stage with
    aggregations needs its long views, so the feature store."""
    import duckdb
    from sqlglot import exp

    from . import sqlx as X

    if any(n.kind == "aggregate" for n in stage.nodes):
        raise ExprError("compute_stage: aggregations read long views; use the feature store")
    frame = df.reset_index(drop=True).assign(__row=lambda d: range(len(d)))
    program, out = stage_program(stage, column)
    tree = program.sql(exp.select("*").from_("frame"), [*frame.columns, *out])
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.register("frame", frame)
    got = con.sql(X.render(tree, "duckdb")).arrow()
    got = (got.read_all() if hasattr(got, "read_all") else got).to_pandas()
    got = got.sort_values("__row").drop(columns="__row").set_axis(df.index)
    if drop_sql_only:
        produced = {n.name for n in stage.nodes}
        sql_reads = {r for n in stage.nodes for r in n.reads if r not in produced}
        got = got.drop(columns=[c for c in sql_reads - set(stage.python_columns) if c in got])
    return got


def ibis_schema(df) -> dict[str, str]:
    """A DataFrame's columns as Ibis types, from pandas' dtypes (Ibis's own from_pyarrow
    needs pyarrow_hotfix, which nothing else here does)."""
    import pandas as pd

    out = {}
    for name, dtype in df.dtypes.items():
        if pd.api.types.is_bool_dtype(dtype):
            out[name] = "boolean"
        elif pd.api.types.is_integer_dtype(dtype):
            out[name] = "int64"
        elif pd.api.types.is_float_dtype(dtype):
            out[name] = "float64"
        elif isinstance(dtype, pd.DatetimeTZDtype):
            out[name] = f"timestamp('{dtype.tz}')"
        elif pd.api.types.is_datetime64_dtype(dtype):
            out[name] = "timestamp"
        elif str(dtype) == "dbdate":
            out[name] = "date"
        else:
            out[name] = "string"
    return out
