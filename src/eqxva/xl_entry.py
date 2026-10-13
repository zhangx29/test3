"""xlwings bridge for EquityXVA_Exposure.xlsm.

Every button in the workbook runs the same xlwings macro, which calls ``dispatch(Book.caller())``.
The action comes from the named cell ``Action`` on the Control sheet:
  RUN_ALL       Monte Carlo + analytic cross-check
  RUN_MC        Monte Carlo only
  RUN_ANALYTIC  analytic (uncollateralised) EE/NEE only
  BBG_REFRESH   pull spot / dividend yield / vol surface through xbbg into the *_override columns
  CLEAR         clear output sheets
"""
from __future__ import annotations

import time
import traceback

import numpy as np
import pandas as pd

from .analytic import run_analytic
from .exposure import run_monte_carlo
from .io_excel import INPUT_SHEETS, parse_inputs

OUTPUT_SHEETS = ["Summary", "EE_NEE", "TradeExposure", "LegMTM", "SACCR_Legs", "SACCR_Entities",
                 "AnalyticCheck", "GridProfile", "Warnings"]


def read_sheets(wb) -> dict[str, pd.DataFrame]:
    names = [s.name for s in wb.sheets]
    out = {}
    for s in INPUT_SHEETS:
        if s not in names:
            continue
        rng = wb.sheets[s].range("A1").expand()
        df = rng.options(pd.DataFrame, index=False, header=True, empty=np.nan).value
        out[s] = df
    return out


def _status(wb, msg: str):
    try:
        wb.sheets["Control"].range("Status").value = msg
    except Exception:
        pass


def _write(wb, name: str, df: pd.DataFrame):
    names = [s.name for s in wb.sheets]
    sh = wb.sheets[name] if name in names else wb.sheets.add(name, after=wb.sheets[-1])
    sh.clear_contents()
    if df is None or df.empty:
        sh.range("A1").value = "(no rows)"
        return sh
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].where(df[c].notna(), None)
    sh.range("A1").options(index=False).value = df
    sh.range("1:1").font.bold = True
    sh.autofit("c")
    return sh


def _charts(wb, ns_profile: pd.DataFrame):
    names = [s.name for s in wb.sheets]
    sh = wb.sheets["Charts"] if "Charts" in names else wb.sheets.add("Charts", after=wb.sheets["Summary"])
    for ch in list(sh.charts):
        ch.delete()
    sh.clear_contents()
    cols = ["bucket_date", "EE", "NEE", "PFE", "EE_uncoll", "FCA_exposure", "SACCR_EAD_mean"]
    row = 1
    for ns, g in ns_profile.groupby("netting_set", sort=False):
        sh.range((row, 1)).value = f"Netting set {ns}"
        sh.range((row, 1)).font.bold = True
        tbl = g[cols].reset_index(drop=True)
        sh.range((row + 1, 1)).options(index=False).value = tbl
        data = sh.range((row + 1, 1), (row + 1 + len(tbl), len(cols)))
        top = sh.range((row, 9)).top
        ch = sh.charts.add(left=sh.range("I1").left, top=top, width=620, height=240)
        ch.chart_type = "line"
        ch.set_source_data(data)
        try:
            ch.api[1].HasTitle = True
            ch.api[1].ChartTitle.Text = f"{ns}: exposure profile"
        except Exception:
            pass
        row += max(len(tbl) + 3, 19)


def run(wb, action: str):
    t0 = time.time()
    if action == "CLEAR":
        for n in OUTPUT_SHEETS:
            if n in [s.name for s in wb.sheets]:
                wb.sheets[n].clear_contents()
        _status(wb, "Outputs cleared")
        return
    if action == "BBG_REFRESH":
        return bbg_refresh(wb)
    _status(wb, f"Running {action} ...")
    mkt, nss, cfg, warnings = parse_inputs(read_sheets(wb))
    res = None
    if action in ("RUN_ALL", "RUN_MC"):
        res = run_monte_carlo(nss, mkt, cfg, warnings)
        for name, df in res.tables().items():
            if name != "AnalyticCheck":
                _write(wb, name, df)
        _charts(wb, res.ns_profile)
    if action in ("RUN_ALL", "RUN_ANALYTIC"):
        _write(wb, "AnalyticCheck", run_analytic(nss, mkt, cfg, res))
    if res is None:
        _write(wb, "Warnings", pd.DataFrame({"warning": warnings or ["(none)"]}))
    _status(wb, f"{action} done in {time.time() - t0:.1f}s - {len(nss)} netting sets, "
                f"{sum(len(n.trades) for n in nss)} trades, {cfg.n_paths} paths"
                + (f", {len(warnings)} warning(s)" if warnings else ""))
    wb.sheets["Summary"].activate()


def bbg_refresh(wb):
    from . import bloomberg
    if not bloomberg.available():
        _status(wb, "xbbg/blpapi not available - use the BDP formulas in the Market sheet or overrides")
        return
    sh = wb.sheets["Market"]
    mk = sh.range("A1").expand().options(pd.DataFrame, index=False, header=True).value
    tick = {r.underlying: r.bbg_ticker for r in mk.itertuples() if isinstance(r.bbg_ticker, str) and r.bbg_ticker}
    mdf, vdf = bloomberg.fetch_market(tick)
    hdr = list(mk.columns)
    for i, r in enumerate(mk.itertuples(), start=2):
        m = mdf[mdf.underlying == r.underlying]
        if m.empty:
            continue
        for col, src in (("spot_override", "spot"), ("div_yield_override", "div_yield"), ("vol_override", "flat_vol")):
            if col in hdr:
                sh.range((i, hdr.index(col) + 1)).value = float(m.iloc[0][src])
    if not vdf.empty:
        vs = wb.sheets["VolSurface"]
        vt = vs.range("A1").expand().options(pd.DataFrame, index=False, header=True).value
        vh = list(vt.columns)
        key = {(a, str(b).upper(), round(float(c), 4)): v for a, b, c, v in vdf.itertuples(index=False)}
        for i, r in enumerate(vt.itertuples(), start=2):
            v = key.get((r.underlying, str(r.tenor).upper(), round(float(r.moneyness), 4)))
            if v is not None and "override" in vh:
                vs.range((i, vh.index("override") + 1)).value = v
    _status(wb, f"Bloomberg refresh done ({len(tick)} underlyings)")


def dispatch(wb):
    try:
        action = str(wb.sheets["Control"].range("Action").value or "RUN_ALL").strip().upper()
        run(wb, action)
    except Exception as e:  # surface errors in the workbook rather than a Python traceback popup
        _status(wb, f"ERROR: {e}")
        try:
            _write(wb, "Warnings", pd.DataFrame({"error": traceback.format_exc().splitlines()}))
        except Exception:
            pass
        raise
