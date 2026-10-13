"""Date helpers: tenor parsing, schedules, year fractions and the time-bucket grid.

Model time is ACT/365F from the valuation date. Funding accruals use ACT/360.
No business-day adjustment is applied (documented simplification).
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

import numpy as np

_TENOR_RE = re.compile(r"^\s*(\d+)\s*([DWMY])\s*$", re.IGNORECASE)


def to_date(x) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    if hasattr(x, "to_pydatetime"):  # pandas Timestamp
        return x.to_pydatetime().date()
    if isinstance(x, (int, float)) and not isinstance(x, bool):  # Excel serial
        return date(1899, 12, 30) + timedelta(days=int(x))
    return datetime.strptime(str(x).strip()[:10], "%Y-%m-%d").date()


def is_tenor(x) -> bool:
    return isinstance(x, str) and bool(_TENOR_RE.match(x))


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def add_tenor(d: date, tenor: str, n: int = 1) -> date:
    m = _TENOR_RE.match(tenor)
    if not m:
        raise ValueError(f"Bad tenor '{tenor}' (expected e.g. 1W, 3M, 1Y)")
    k, unit = int(m.group(1)) * n, m.group(2).upper()
    if unit == "D":
        return d + timedelta(days=k)
    if unit == "W":
        return d + timedelta(weeks=k)
    if unit == "M":
        return add_months(d, k)
    return add_months(d, 12 * k)


def tenor_years(tenor: str) -> float:
    base = date(2000, 1, 1)
    return (add_tenor(base, tenor) - base).days / 365.0


def date_or_tenor(x, ref: date) -> date:
    """Accept either a date or a tenor string relative to ``ref``."""
    if is_tenor(x):
        return add_tenor(ref, x.strip().upper())
    return to_date(x)


def yf(d0: date, d1: date) -> float:
    """ACT/365F model time."""
    return (d1 - d0).days / 365.0


def act360(d0: date, d1: date) -> float:
    return (d1 - d0).days / 360.0


def schedule(start: date, end: date, freq: str | None) -> list[date]:
    """Unadjusted roll dates start, start+freq, ... with the final stub ending at ``end``."""
    if freq is None or str(freq).strip().upper() in ("", "NONE", "0", "NAN"):
        return [start, end]
    out = [start]
    n = 1
    while True:
        d = add_tenor(start, str(freq).strip().upper(), n)
        if d >= end:
            break
        out.append(d)
        n += 1
    out.append(end)
    return out


def parse_buckets(spec, val: date, horizon: date) -> list[date]:
    """Bucket spec: comma list of tenors/dates ("1M,3M,1Y,2025-06-30") or "EVERY:1M" for a regular step."""
    s = str(spec).strip()
    if s.upper().startswith("EVERY:"):
        step = s.split(":", 1)[1].strip().upper()
        return [d for d in schedule(val, horizon, step)[1:]]
    out = sorted({date_or_tenor(tok.strip().upper() if is_tenor(tok.strip()) else tok.strip(), val)
                  for tok in s.split(",") if tok.strip()})
    return out


def merge_times(*arrays, tol: float = 1e-9) -> np.ndarray:
    t = np.unique(np.concatenate([np.atleast_1d(np.asarray(a, dtype=float)) for a in arrays]))
    t = t[t >= 0.0]
    if t.size == 0:
        return t
    keep = np.concatenate([[True], np.diff(t) > tol])
    return t[keep]
