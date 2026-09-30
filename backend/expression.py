"""
Safe, vectorized expression evaluator for frame selection — the SubframeSelector
approval-expression model.

Each property (HFR, FWHM, Eccentricity, Stars, Altitude, MoonSep, ...) is exposed
as a numpy array over the candidate set. Aggregate functions (median, mad, mean,
std, percentile, ...) reduce a property array to a scalar, so outlier-rejection
expressions work directly, e.g.:

    HFR < 3.5 && Eccentricity < 0.6 && FWHM < median(FWHM) + 2*mad(FWHM)

Evaluation is done over a Python `ast` with a strict node whitelist — no attribute
access, subscripting, comprehensions, lambdas, or calls to anything but the
whitelisted aggregate/math functions. This makes arbitrary code execution
impossible while keeping the expression language expressive.

Comparisons against NaN are False, so a frame missing a referenced metric is
rejected (you can't approve on an unknown value).

SQL-style sugar: `x between 2 and 3`, `x not between 2 and 3`, `path like '%x%'`,
`path not like '%x%'`. Datetime columns compare against ISO strings at the
precision typed: `date == '2026-05-13'` matches that whole (UTC) day, and
`date between '2026-05-01' and '2026-05-31 22:30'` is inclusive at both ends.
"""
from __future__ import annotations

import ast
import re
import operator
import warnings
import numpy as np


def _mad(a):
    a = np.asarray(a, dtype=float)
    med = np.nanmedian(a)
    return np.nanmedian(np.abs(a - med))


def _madstd(a):
    # MAD scaled to approximate a Gaussian sigma (SubframeSelector convention)
    return 1.4826 * _mad(a)


def _isnan(a):
    a = np.asarray(a)
    if np.issubdtype(a.dtype, np.datetime64):
        return np.isnat(a)
    return np.isnan(a.astype(float))


def to_datetime64(s) -> np.datetime64:
    """Parse an ISO date/time ('2026-05', '2026-05-01', '2026-05-01 22:30', ...),
    keeping the precision typed. Raises ValueError on anything else."""
    v = str(s).strip().upper().replace(" ", "T")
    if v.endswith("Z"):
        v = v[:-1]
    return np.datetime64(v)


def _is_datetime(v):
    return isinstance(v, (np.ndarray, np.datetime64)) and np.issubdtype(np.asarray(v).dtype, np.datetime64)


def _compare(op, left, right):
    """Elementwise comparison. A datetime column vs a date string is compared at
    the string's precision (the column is truncated to it)."""
    for col, lit, swapped in ((left, right, False), (right, left, True)):
        if _is_datetime(col) and isinstance(lit, str):
            try:
                d = to_datetime64(lit)
            except ValueError:
                raise ExpressionError(f"Invalid date: '{lit}' (use e.g. '2026-05-01' or '2026-05-01 22:30')")
            col = np.asarray(col).astype(d.dtype)
            left, right = (d, col) if swapped else (col, d)
            break
    with np.errstate(invalid="ignore"):
        return op(left, right)


def _between(x, lo, hi):
    # Inclusive; NaN / NaT is never between anything
    return np.logical_and(_compare(operator.ge, x, lo), _compare(operator.le, x, hi))


def _like_regex(pattern: str):
    # SQL LIKE: % = any run of chars, _ = one char; everything else literal
    rx = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.compile(rx, re.IGNORECASE | re.DOTALL)


def _like(values, pattern):
    if not isinstance(pattern, str):
        raise ExpressionError("like() pattern must be a string, e.g. like(path, '%reject%')")
    rx = _like_regex(pattern)
    if isinstance(values, str):
        return rx.fullmatch(values) is not None
    # Non-string entries (e.g. a numeric column) never match
    return np.array([isinstance(v, str) and rx.fullmatch(v) is not None for v in values], dtype=bool)


# Whitelisted functions. Aggregates reduce an array to a scalar; math funcs are
# elementwise. All are NaN-aware where it matters.
FUNCS = {
    "median": np.nanmedian,
    "mean": np.nanmean,
    "avg": np.nanmean,
    "std": np.nanstd,
    "sigma": np.nanstd,
    "min": np.nanmin,
    "max": np.nanmax,
    "sum": np.nansum,
    "count": lambda a: float(np.sum(np.isfinite(np.asarray(a, dtype=float)))),
    "mad": _mad,
    "madstd": _madstd,
    "percentile": lambda a, q: np.nanpercentile(a, q),
    "abs": np.abs,
    "sqrt": np.sqrt,
    "log": np.log,
    "log10": np.log10,
    # Missing-value tests (a field absent in the DB comes through as NaN / NaT)
    "isnan": _isnan,
    "missing": _isnan,
    "present": lambda a: ~_isnan(a),
    # Inclusive range: between(hfr, 1.5, median(hfr)), between(date, '2026-05-01', '2026-05-31')
    "between": _between,
    # SQL-style pattern match on string columns: like(path, '%reject%')
    "like": _like,
}

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}

_CMPOPS = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


class ExpressionError(ValueError):
    pass


_STRING = r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\""""
# Infix SQL sugar: `path like '%x%'` / `path not like '%x%'` -> like(path, '%x%')
_INFIX_LIKE = re.compile(rf"\b([A-Za-z_]\w*)\s+(not\s+)?like\s+({_STRING})", re.IGNORECASE)
# Infix `x [not] between LIT and LIT` (literal bounds; use between() for computed ones)
_LITERAL = rf"{_STRING}|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_INFIX_BETWEEN = re.compile(
    rf"\b([A-Za-z_]\w*)\s+(not\s+)?between\s+({_LITERAL})(?:\s+and\s+|\s*&&\s*)({_LITERAL})", re.IGNORECASE)


def _between_sub(m):
    x, neg, lo, hi = m.groups()
    # NOT BETWEEN as an explicit outside test, so missing values stay rejected
    return f"({x} < {lo} or {x} > {hi})" if neg else f"between({x}, {lo}, {hi})"


def _preprocess(expr: str) -> str:
    expr = _INFIX_BETWEEN.sub(_between_sub, expr)
    expr = _INFIX_LIKE.sub(
        lambda m: f"({'not ' if m.group(2) else ''}like({m.group(1)}, {m.group(3)}))", expr)

    def ops(code: str) -> str:
        # Accept C-style boolean operators in addition to and/or/not
        code = code.replace("&&", " and ").replace("||", " or ")
        # ! as logical not, but never touch != ; replace "!" not followed by "="
        return re.sub(r"!(?!=)", " not ", code)

    # Rewrite operators outside string literals only, so a pattern like '%!%' survives
    parts = re.split(f"({_STRING})", expr)
    return "".join(p if i % 2 else ops(p) for i, p in enumerate(parts))


class _Evaluator:
    def __init__(self, variables: dict, n: int, funcs: dict):
        self.vars = variables
        self.n = n
        self.funcs = funcs

    def _as_array(self, value):
        if np.isscalar(value) or (isinstance(value, np.ndarray) and value.ndim == 0):
            return np.full(self.n, value)
        return value

    def eval(self, node):
        if isinstance(node, ast.Expression):
            return self.eval(node.body)

        if isinstance(node, ast.BoolOp):
            vals = [self._as_array(self.eval(v)).astype(bool) for v in node.values]
            if isinstance(node.op, ast.And):
                out = vals[0]
                for v in vals[1:]:
                    out = np.logical_and(out, v)
                return out
            if isinstance(node.op, ast.Or):
                out = vals[0]
                for v in vals[1:]:
                    out = np.logical_or(out, v)
                return out
            raise ExpressionError("Unsupported boolean operator")

        if isinstance(node, ast.UnaryOp):
            val = self.eval(node.operand)
            if isinstance(node.op, ast.Not):
                return np.logical_not(self._as_array(val).astype(bool))
            if isinstance(node.op, ast.USub):
                return operator.neg(val)
            if isinstance(node.op, ast.UAdd):
                return val
            raise ExpressionError("Unsupported unary operator")

        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise ExpressionError(f"Unsupported operator: {type(node.op).__name__}")
            return op(self.eval(node.left), self.eval(node.right))

        if isinstance(node, ast.Compare):
            left = self.eval(node.left)
            result = None
            for op_node, comparator in zip(node.ops, node.comparators):
                cmp = _CMPOPS.get(type(op_node))
                if cmp is None:
                    raise ExpressionError("Unsupported comparison")
                right = self.eval(comparator)
                this = self._as_array(_compare(cmp, left, right)).astype(bool)
                result = this if result is None else np.logical_and(result, this)
                left = right  # support chained comparisons (a < b < c)
            return result

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError("Only direct function calls allowed")
            fname = node.func.id
            fn = self.funcs.get(fname)
            if fn is None:
                raise ExpressionError(f"Unknown function: {fname}")
            args = [self.eval(a) for a in node.args]
            return fn(*args)

        if isinstance(node, ast.Name):
            key = node.id
            if key in self.vars:
                return self.vars[key]
            raise ExpressionError(f"Unknown variable: {key}")

        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return node.value
            if isinstance(node.value, (int, float)):
                return float(node.value)
            if isinstance(node.value, str):
                return node.value   # for string-column comparisons, e.g. filter == 'h'
            raise ExpressionError("Only numeric/string constants allowed")

        raise ExpressionError(f"Disallowed expression element: {type(node).__name__}")


def evaluate(expression: str, variables: dict, n: int, extra_funcs: dict = None) -> np.ndarray:
    """
    Evaluate a selection expression. `variables` maps lowercased property name ->
    numpy array of length n. `extra_funcs` adds per-query callables (e.g. spatial
    predicates that close over the candidate frames). Returns a boolean mask.
    """
    expr = _preprocess(expression).strip()
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"Syntax error: {e.msg}")

    funcs = dict(FUNCS)
    if extra_funcs:
        funcs.update(extra_funcs)
    ev = _Evaluator(variables, n, funcs)
    # All-NaN columns (un-analyzed frames) make nanmedian/etc warn — harmless here.
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        result = ev.eval(tree)
        mask = ev._as_array(result).astype(bool)
    if mask.shape != (n,):
        raise ExpressionError("Expression did not produce a per-frame result")
    return mask
