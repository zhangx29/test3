"""Read inputs from workbook sheets (DataFrames) and write results to Excel.

Input sheets (same layout in the .xlsm front end and in a plain .xlsx used by the CLI):
  Config       key | value
  Market       underlying | spot | div_yield | is_index | flat_vol | bbg_ticker
  Curve        tenor | zero_rate                      (continuously compounded, decimal)
  VolSurface   underlying | tenor | moneyness | vol    (optional; long format)
  Correlation  underlying1 | underlying2 | rho         (optional)
  NettingSets  netting_set | counterparty | collateralized | threshold_cp | threshold_bank | mta |
               ia_received | ia_posted | ia_posted_segregated | mpor_days | rounding
  Trades       trade_id | netting_set | leg_id | leg_type | underlying | direction | shares |
               start_date | maturity | equity_reset_freq | funding_reset_freq | funding_spread |
               notional_basis | avg_price | initial_mtm | div_passthrough |
               option_type | strike | option_maturity | vol_override | ovme_price
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import dates as dt
from .market import MarketData, Underlying, VolSurface, ZeroCurve
from .model import RunConfig, build_netting_sets

INPUT_SHEETS = ["Config", "Market", "Curve", "VolSurface", "Correlation", "NettingSets", "Trades"]


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, [c for c in df.columns if c and not c.startswith("Unnamed") and c != "None"]]
    return df.dropna(how="all")


def _num(x, what: str) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: '{x}' is not a number (Bloomberg still loading or #N/A?)") from None
    if np.isnan(v):
        raise ValueError(f"{what}: missing value")
    return v


def _truthy(x) -> bool:
    return str(x).strip().upper() in ("Y", "YES", "TRUE", "1", "1.0")


def _records(df: pd.DataFrame) -> list[dict]:
    recs = []
    for r in df.to_dict("records"):
        recs.append({k: (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in r.items()})
    return recs


def parse_config(df: pd.DataFrame) -> dict:
    df = _clean(df)
    kv = {str(k).strip().lower(): v for k, v in zip(df.iloc[:, 0], df.iloc[:, 1]) if str(k).strip()}
    return kv


def parse_inputs(sheets: dict[str, pd.DataFrame]):
    """Return (market, netting_sets, run_config, warnings)."""
    warnings: list[str] = []
    kv = parse_config(sheets["Config"])
    val = dt.to_date(kv["valuation_date"])

    curve_df = _clean(sheets["Curve"])
    ts = np.array([dt.yf(val, dt.date_or_tenor(str(t).strip().upper(), val)) for t in curve_df["tenor"]])
    rs = np.array([_num(r, f"Curve {t}") for t, r in zip(curve_df["tenor"], curve_df["zero_rate"])])
    order = np.argsort(ts)
    curve = ZeroCurve(ts[order], rs[order])

    vs = _clean(sheets["VolSurface"]) if "VolSurface" in sheets and sheets["VolSurface"] is not None else None
    unds: dict[str, Underlying] = {}
    for r in _records(_clean(sheets["Market"])):
        name = str(r["underlying"]).strip()
        if not name or name == "None":
            continue
        surf = None
        if vs is not None and len(vs):
            sub = vs[vs["underlying"].astype(str).str.strip() == name]
            sub = sub[pd.to_numeric(sub["vol"], errors="coerce").notna()]   # blank = no BBG data, no override
            if len(sub):
                sub = sub.assign(T=[dt.tenor_years(str(t).strip().upper()) if dt.is_tenor(str(t).strip())
                                    else float(t) for t in sub["tenor"]],
                                 m=sub["moneyness"].astype(float), v=[_num(v, f"Vol {name}") for v in sub["vol"]])
                piv = sub.pivot_table(index="T", columns="m", values="v")
                if piv.isna().any().any():
                    piv = piv.interpolate(axis=1, limit_direction="both")
                surf = VolSurface(piv.index.values.astype(float), piv.columns.values.astype(float), piv.values)
        if surf is None:
            surf = VolSurface.flat(_num(r.get("flat_vol"), f"Market {name} flat_vol"))
        unds[name] = Underlying(name, _num(r["spot"], f"Market {name} spot"),
                                0.0 if r.get("div_yield") is None else _num(r["div_yield"], f"{name} div_yield"),
                                _truthy(r.get("is_index", "N")), surf, str(r.get("bbg_ticker") or ""))
    corr = {}
    if "Correlation" in sheets and sheets["Correlation"] is not None:
        for r in _records(_clean(sheets["Correlation"])):
            if r.get("underlying1") and r.get("underlying2"):
                corr[(str(r["underlying1"]).strip(), str(r["underlying2"]).strip())] = float(r["rho"])
    mkt = MarketData(val, curve, unds, corr)

    trade_rows = _records(_clean(sheets["Trades"]))
    ns_rows = _records(_clean(sheets["NettingSets"])) if "NettingSets" in sheets else []
    nss = build_netting_sets(trade_rows, ns_rows, mkt, warnings)
    horizon = val + dt.timedelta(days=int(max(ns.maturity for ns in nss) * 365) + 1)
    cfg = RunConfig(
        valuation_date=val,
        buckets=dt.parse_buckets(kv.get("buckets", "1M,3M,6M,1Y,2Y,3Y,5Y"), val, horizon),
        n_paths=int(float(kv.get("n_paths", 10000))),
        seed=int(float(kv.get("seed", 42))),
        pfe_quantile=float(kv.get("pfe_quantile", 0.95)),
        fine_step=str(kv.get("fine_step", "1W")).strip().upper(),
        output_mode=str(kv.get("output_mode", "AVERAGE")).strip().upper(),
        antithetic=_truthy(kv.get("antithetic", "Y")),
        epe_horizon=str(kv.get("epe_horizon", "1Y")).strip().upper(),
        analytic_nodes=int(float(kv.get("analytic_nodes", 201))),
    )
    return mkt, nss, cfg, warnings


def read_workbook(path: str | Path) -> dict[str, pd.DataFrame]:
    xl = pd.ExcelFile(path)
    out = {}
    for s in INPUT_SHEETS:
        if s in xl.sheet_names:
            out[s] = xl.parse(s, header=0 if s != "Config" else 0)
    missing = {"Config", "Market", "Curve", "Trades"} - set(out)
    if missing:
        raise ValueError(f"{path}: missing sheets {sorted(missing)}")
    return out


def write_results(results, path: str | Path, inputs: dict[str, pd.DataFrame] | None = None) -> Path:
    from openpyxl.chart import LineChart, Reference, Series
    from openpyxl.utils import get_column_letter

    path = Path(path)
    tables = results.tables()
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in tables.items():
            df.to_excel(xw, sheet_name=name, index=False)
        if inputs:
            for name, df in inputs.items():
                df.to_excel(xw, sheet_name=f"in_{name}"[:31], index=False)
        wb = xw.book
        for ws in wb.worksheets:
            for col in ws.columns:
                w = max(len(str(c.value)) if c.value is not None else 0 for c in col[:50])
                ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(10, w + 2), 40)
            ws.freeze_panes = "A2"
        # charts: one per netting set from EE_NEE
        ws = wb["EE_NEE"]
        cs = wb.create_sheet("Charts", 1)
        df = tables["EE_NEE"]
        cols = list(df.columns)
        anchor_row = 1
        for ns, g in df.groupby("netting_set", sort=False):
            r0, r1 = g.index.min() + 2, g.index.max() + 2
            ch = LineChart()
            ch.title = f"{ns}: EE / NEE / PFE"
            ch.y_axis.title = "Exposure"
            ch.x_axis.title = "Bucket"
            for c in ("EE", "NEE", "PFE", "EE_uncoll", "SACCR_EAD_mean"):
                ci = cols.index(c) + 1
                ch.series.append(Series(Reference(ws, min_col=ci, min_row=r0, max_row=r1), title=c))
            ci = cols.index("bucket_date") + 1
            ch.set_categories(Reference(ws, min_col=ci, min_row=r0, max_row=r1))
            ch.height, ch.width = 8, 18
            cs.add_chart(ch, f"A{anchor_row}")
            anchor_row += 17
    return path
