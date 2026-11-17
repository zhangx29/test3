"""Path-by-path trade PVs imported from an external system (e.g. BMO's 84-strip prepay booking).

A netting set with ``value_source = IMPORT`` takes its trade values from these PVs instead of the
simulation. Everything downstream is the engine's own: window masking by the booked trades' active
dates, static collateral / CSA, statistics, buckets, FCA/FBA and SA-CCR.

PathImport sheet, one row per (trade, date):
  trade_id | date | file | sheet | column | scenario_column | scale | sign
  - file: CSV, Excel or Parquet, relative to the input workbook's folder. Rows are scenarios.
  - column: the PV column of that trade at that date. One (file, column) may feed several trade_ids
    (e.g. the same booking in a with-CSRA and a without-CSRA netting set).
  - scenario_column (optional): aligns rows across files by scenario id; otherwise row order is used
    and every column must have the same number of rows.
  - scale (default 1) and sign (default +1) convert the export to the netting set's CSA currency
    amount, bank view (positive = the counterparty owes the bank).
The booked trade terms (Trades sheet) are still required: they give the active windows, the
netting-set maturity and the SA-CCR notional. Only PVs are imported, so the SA-CCR adjusted notional
uses quantity x today's forward on every path (a warning says so); RC and the multiplier are
path-wise from the imported PVs. If the export has no valuation-date column, the t = 0 value is the
booked trade's deterministic MTM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from . import dates as dt

IMPORT_COLUMNS = ["trade_id", "date", "file", "sheet", "column", "scenario_column", "scale", "sign"]


@dataclass
class PathCube:
    dates: list[date]
    times: np.ndarray                                   # years from the valuation date, sorted
    values: dict[str, np.ndarray] = field(default_factory=dict)   # trade_id -> (n_scen, len(times))
    n_scen: int = 0
    # system SA-CCR per trade: DataFrame(t, date, RC, multiplier, PFE, EAD) of path means per date
    saccr: dict[str, pd.DataFrame] = field(default_factory=dict)
    requests: dict[str, str] = field(default_factory=dict)        # trade_id -> request id


def _blank(x) -> bool:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return True
    if not isinstance(x, str) and x != x:      # NaT
        return True
    return str(x).strip() == ""


def _read(path: Path, sheet) -> pd.DataFrame:
    suf = path.suffix.lower()
    if suf in (".csv", ".txt"):
        return pd.read_csv(path)
    if suf in (".parquet", ".pq"):
        return pd.read_parquet(path)
    if suf in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, sheet_name=0 if _blank(sheet) else sheet)
    raise ValueError(f"PathImport: unsupported file type '{path.name}' (use CSV, Excel or Parquet)")


def load_path_import(sheets: dict[str, pd.DataFrame], base_dir, val: date) -> PathCube:
    """Read every file named on the PathImport sheet into a PathCube."""
    df = sheets.get("PathImport")
    if df is None or df.dropna(how="all").empty:
        raise ValueError("A netting set has value_source = IMPORT but there is no PathImport sheet")
    base = Path(base_dir)
    rows = [r for r in df.to_dict("records") if not _blank(r.get("trade_id"))]
    for c in ("date", "file", "column"):
        if any(_blank(r.get(c)) for r in rows):
            raise ValueError(f"PathImport: every row needs a {c}")
    cache: dict[tuple, pd.DataFrame] = {}
    cells: dict[tuple[str, date], np.ndarray] = {}
    n_scen = None
    for r in rows:
        tid = str(r["trade_id"]).strip()
        d = dt.to_date(r["date"])
        key = (str(r["file"]).strip(), "" if _blank(r.get("sheet")) else str(r["sheet"]).strip())
        if key not in cache:
            p = base / key[0]
            if not p.exists():
                raise ValueError(f"PathImport: file not found: {p}")
            cache[key] = _read(p, key[1])
        src = cache[key]
        col = str(r["column"]).strip()
        if col not in src.columns:
            raise ValueError(f"PathImport {tid} {d}: column '{col}' not in {key[0]}")
        if not _blank(r.get("scenario_column")):
            sc = str(r["scenario_column"]).strip()
            if sc not in src.columns:
                raise ValueError(f"PathImport {tid} {d}: scenario_column '{sc}' not in {key[0]}")
            s = src.sort_values(sc, kind="stable")[col]
        else:
            s = src[col]
        v = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
        if np.isnan(v).any():
            raise ValueError(f"PathImport {tid} {d}: {int(np.isnan(v).sum())} blank or non-numeric PVs in "
                             f"{key[0]}:{col}")
        scale = 1.0 if _blank(r.get("scale")) else float(r["scale"])
        sign = 1.0 if _blank(r.get("sign")) else float(r["sign"])
        if n_scen is None:
            n_scen = len(v)
        elif len(v) != n_scen:
            raise ValueError(f"PathImport {tid} {d}: {len(v)} scenarios in {key[0]}:{col}, expected {n_scen}")
        if (tid, d) in cells:
            raise ValueError(f"PathImport: {tid} {d} is mapped twice")
        cells[(tid, d)] = v * scale * sign
    dates = sorted({d for _, d in cells})
    if any(d < val for d in dates):
        raise ValueError(f"PathImport: dates before the valuation date {val}")
    times = np.array([dt.yf(val, d) for d in dates])
    cube = PathCube(dates, times, {}, n_scen or 0)
    for tid in sorted({t for t, _ in cells}):
        missing = [d for d in dates if (tid, d) not in cells]
        if missing:
            raise ValueError(f"PathImport: trade {tid} has no PVs for {len(missing)} date(s), e.g. {missing[0]}")
        cube.values[tid] = np.column_stack([cells[(tid, d)] for d in dates])
    return cube


def build_path_import_sheet(trade_ids, dates, file: str, column: str, sheet: str = "",
                            scenario_column: str = "", scale: float = 1.0, sign: float = 1.0,
                            export_ids=None) -> pd.DataFrame:
    """PathImport rows for every trade x date from name patterns. ``file`` and ``column`` are format
    strings with {trade_id} (the export's id, ``export_ids[k]`` if given, else the trade_id),
    {k} (trade index) and {date} (a datetime.date, e.g. {date:%Y%m%d}).
    Example: file="pv_{date:%Y%m%d}.csv", column="PV_{trade_id}"."""
    trade_ids = list(trade_ids)
    ex = list(export_ids) if export_ids is not None else trade_ids
    rows = []
    for k, (tid, xid) in enumerate(zip(trade_ids, ex)):
        for d in dates:
            f = dict(trade_id=xid, k=k, date=d)
            rows.append(dict(trade_id=tid, date=d, file=file.format(**f), sheet=sheet.format(**f),
                             column=column.format(**f), scenario_column=scenario_column, scale=scale, sign=sign))
    return pd.DataFrame(rows, columns=IMPORT_COLUMNS)


# ----------------------------------------------------------------------------- BMO risk-profile export
SYSTEM_COLUMNS = ["trade_id", "request_id", "folder", "undiscount"]


def _norm(name: str) -> str:
    """File name -> lower-case letters and digits only, so 'RiskProfile_281985.Test DiscPosNet.csv',
    'RiskProfile_281985_Test DiscPosNet.csv' and 'Risk_Profile_281985 test discposnet.csv' all match."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _find(files: dict[str, Path], rid: str, kind: str) -> Path | None:
    """kind: testdiscposnet, testdiscnegnet, basediscposnet, basediscnegnet, or saccr (SACCR_PathEADs ... Test)."""
    want = f"riskprofile{rid}"
    hits = []
    for n, p in files.items():
        if not n.startswith(want) or not n.endswith("csv"):
            continue
        rest = n[len(want):-3]
        if rest[:1].isdigit():
            continue                        # a longer request id that starts with this one
        ok = (rest.startswith("saccrpatheads") and rest.endswith("test")) if kind == "saccr" else rest == kind
        if ok:
            hits.append(p)
    if len(hits) > 1:
        raise ValueError(f"Request {rid}: several files match {kind}: {', '.join(h.name for h in hits)}")
    return hits[0] if hits else None


def _wide(path: Path | None) -> pd.DataFrame | None:
    """Wide exposure file: first row = dates, one row per scenario. None if missing or empty."""
    if path is None:
        return None
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return None
    df = df.dropna(how="all")
    if df.empty:
        return None
    cols = []
    for c in df.columns:
        s = str(c).strip()
        if "/" in s and s.count(".") == 1:  # pandas renames a repeated header '11/12/2026' to '11/12/2026.1'
            raise ValueError(f"{path.name}: date {s.split('.')[0]} appears twice in the header")
        cols.append(dt.to_date(s))
    df.columns = cols
    return df.apply(pd.to_numeric, errors="coerce").fillna(0.0)


def _saccr_paths(path: Path | None) -> pd.DataFrame | None:
    """Path-wise SA-CCR file (NettingSet, Valuation_Date, PathId, RC, Multiple, PFE, EAD) -> path means per date."""
    if path is None:
        return None
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return None
    df.columns = [str(c).strip() for c in df.columns]
    need = {"Valuation_Date", "PathId", "RC", "PFE", "EAD"}
    if not need <= set(df.columns):
        raise ValueError(f"{path.name}: expected columns {sorted(need)} (got {list(df.columns)})")
    df = df.dropna(subset=["Valuation_Date"])
    if df.empty:
        return None
    mult = "Multiple" if "Multiple" in df.columns else "Multiplier" if "Multiplier" in df.columns else None
    df["date"] = [dt.to_date(x) for x in df["Valuation_Date"]]
    g = df.groupby("date")
    out = pd.DataFrame({"RC": g["RC"].mean(), "multiplier": g[mult].mean() if mult else np.nan,
                        "PFE": g["PFE"].mean(), "EAD": g["EAD"].mean(), "n_paths": g["PathId"].nunique()})
    return out.reset_index()


def load_system_risk_profiles(sheets: dict[str, pd.DataFrame], base_dir, val: date, curve) -> PathCube:
    """Read BMO's per-request risk-profile export for the trades on the SystemRequests sheet.

    SystemRequests: trade_id | request_id | folder | undiscount
      folder: relative to the input workbook (blank = its folder); undiscount: Y (default) or N.
    Per request id the folder holds (names matched ignoring case, spaces, '.' and '_'):
      RiskProfile_<id>.Test DiscPosNet.csv, RiskProfile_<id>.Test DiscNegNet.csv   (required)
      RiskProfile_<id>.Base DiscPosNet.csv, RiskProfile_<id>.Base DiscNegNet.csv   (optional, may be empty)
      RiskProfile_<id>_SACCR_PathEADs_TT_Test.csv                                  (optional)
    The trade's PV per scenario and date is (Pos + Neg)_Test - (Pos + Neg)_Base: the netting-set value
    with the new trade minus without it. Values are discounted to the file's first date, so with
    undiscount = Y they are divided by DF(first date, date) from ``curve``. Every request must have
    the same dates and scenario count (row i = the same scenario in every file)."""
    df = sheets.get("SystemRequests")
    if df is None or df.dropna(how="all").empty:
        raise ValueError("No SystemRequests sheet")
    base = Path(base_dir)
    rows = [r for r in df.to_dict("records") if not _blank(r.get("trade_id"))]
    listing: dict[Path, dict[str, Path]] = {}
    cache: dict[tuple, tuple] = {}
    cube = None
    for r in rows:
        tid = str(r["trade_id"]).strip()
        if _blank(r.get("request_id")):
            raise ValueError(f"SystemRequests: trade {tid} has no request_id")
        rid = str(r["request_id"]).strip()
        if rid.endswith(".0"):
            rid = rid[:-2]
        folder = base if _blank(r.get("folder")) else base / str(r["folder"]).strip()
        undisc = _blank(r.get("undiscount")) or str(r["undiscount"]).strip().upper() in ("Y", "YES", "TRUE", "1")
        key = (folder, rid, undisc)
        if key not in cache:
            if folder not in listing:
                if not folder.is_dir():
                    raise ValueError(f"SystemRequests: folder not found: {folder}")
                listing[folder] = {_norm(p.name): p for p in folder.iterdir() if p.is_file()}
            files = listing[folder]
            pos, neg = _wide(_find(files, rid, "testdiscposnet")), _wide(_find(files, rid, "testdiscnegnet"))
            if pos is None or neg is None:
                raise ValueError(f"Request {rid} ({tid}): Test DiscPosNet and Test DiscNegNet files are required "
                                 f"in {folder}")
            if list(pos.columns) != list(neg.columns) or pos.shape != neg.shape:
                raise ValueError(f"Request {rid}: DiscPosNet and DiscNegNet have different dates or scenarios")
            V = pos.to_numpy() + neg.to_numpy()
            for kind in ("basediscposnet", "basediscnegnet"):
                b = _wide(_find(files, rid, kind))
                if b is not None:
                    if list(b.columns) != list(pos.columns) or b.shape != pos.shape:
                        raise ValueError(f"Request {rid}: {kind} has different dates or scenarios from Test")
                    V = V - b.to_numpy()
            dates = list(pos.columns)
            if undisc:
                tau = np.array([(d - dates[0]).days / 365.0 for d in dates])
                V = V / np.asarray(curve.df(tau))[None, :]
            cache[key] = (dates, V, _saccr_paths(_find(files, rid, "saccr")))
        dates, V, sa = cache[key]
        if cube is None:
            if any(d < val for d in dates):
                raise ValueError(f"Request {rid}: export dates start {dates[0]}, before the valuation date {val}; "
                                 f"set Config valuation_date to the export's first date")
            cube = PathCube(dates, np.array([dt.yf(val, d) for d in dates]), {}, V.shape[0])
        elif dates != cube.dates or V.shape[0] != cube.n_scen:
            raise ValueError(f"Request {rid} ({tid}): dates or scenario count differ from the first request")
        if tid in cube.values:
            raise ValueError(f"SystemRequests: trade {tid} is listed twice")
        cube.values[tid] = V
        cube.requests[tid] = rid
        if sa is not None:
            cube.saccr[tid] = sa.assign(t=[dt.yf(val, d) for d in sa["date"]])
    if cube is None:
        raise ValueError("SystemRequests has no rows")
    return cube


def merge_cubes(a: PathCube | None, b: PathCube | None) -> PathCube | None:
    if a is None or b is None:
        return a or b
    if a.dates != b.dates or a.n_scen != b.n_scen:
        raise ValueError("PathImport and SystemRequests must have the same dates and scenario count")
    clash = set(a.values) & set(b.values)
    if clash:
        raise ValueError(f"Trades on both PathImport and SystemRequests: {', '.join(sorted(clash))}")
    return PathCube(a.dates, a.times, {**a.values, **b.values}, a.n_scen, {**a.saccr, **b.saccr},
                    {**a.requests, **b.requests})


def load_imports(sheets: dict[str, pd.DataFrame], base_dir, mkt) -> PathCube:
    """PathImport and / or SystemRequests, whichever sheets are present."""
    def has(s):
        return sheets.get(s) is not None and not sheets[s].dropna(how="all").empty
    if not has("PathImport") and not has("SystemRequests"):
        raise ValueError("A netting set has value_source = IMPORT but there is no PathImport or SystemRequests sheet")
    a = load_path_import(sheets, base_dir, mkt.valuation_date) if has("PathImport") else None
    b = load_system_risk_profiles(sheets, base_dir, mkt.valuation_date, mkt.curve) if has("SystemRequests") else None
    return merge_cubes(a, b)
