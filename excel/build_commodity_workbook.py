"""Build excel/CommodityXVA_Exposure.xlsm (needs Windows + Excel + xlwings).

    .venv\\Scripts\\python excel\\build_commodity_workbook.py

Same engine and macro as EquityXVA_Exposure.xlsm, with the commodity input sheets. Contracts has a
Bloomberg BDP price, an override column that wins, and the price the engine reads.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import xlwings as xw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "excel"))

from build_workbook import CALC_FILL, INPUT_FILL, header, put_df, quickstart_template, validation  # noqa: E402
from eqxva.commodity.examples import TRADE_COLUMNS, VAL_DATE, example_sheets  # noqa: E402
from eqxva.xl_entry import OUTPUT_SHEETS  # noqa: E402

NAME = "CommodityXVA_Exposure"
OUT = ROOT / "excel" / f"{NAME}.xlsm"
DATE_FMT = "yyyy-mm-dd"

NOTES = [
    "How to use",
    "PLACEHOLDERS: the example prices, vols, factor parameters and correlations are not market data. "
    "Replace them before using any output.",
    "1. Contracts: one row per futures contract. price_bdp pulls PX_LAST when the Bloomberg add-in is loaded; "
    "price_override wins. Prices are in the product's price_unit (Bloomberg beans are C/BU, oil C/LB). "
    "implied_vol is the ATM vol of the contract's option, and option_expiry is that option's expiry.",
    "2. CommodityProducts: hedging_set and commodity_type drive SA-CCR. Giving two products the same "
    "commodity_type offsets them fully; this is a policy choice (CRE52.71).",
    "3. CommodityFactors: N rows per product (2 for agriculture: SHORT beta > 0, LONG beta = 0). vol_ratio is "
    "the factor's vol relative to the factor with ratio 1. Each contract is fitted to its own implied vol.",
    "4. FactorCorrelation: rho between factors named PRODUCT.FACTOR. These drive spread exposure. A blank pair "
    "is 0. Check the simulated spread vol against crush options or history.",
    "5. NettingSets: as the equity workbook, plus sa_alpha (blank = 1.4).",
    "6. Trades: one row per leg, COMSWAP. Legs sharing a trade_id form one trade. direction BUY = bank pays "
    "fixed. Enter lots or quantity. Blank fixed_price = at market, blank fixing_date = contract expiry, blank "
    "payment_date = fixing date.",
    "7. Pick an Action and press the button: RUN_ALL = Monte Carlo + analytic check, RUN_MC, RUN_ANALYTIC, CLEAR.",
    "Outputs: as the equity workbook (Summary, EE_NEE, TradeExposure, LegMTM, SACCR_Legs, SACCR_Entities). "
    "SACCR_Proxy gives the single-line SA_NOTIONAL at t0. AnalyticCheck adds Sigma and the normal approximation "
    "(EE_normal, PFE_normal = 1.9 Sigma).",
    "Python engine: see xlwings.conf sheet. Re-run excel/build_commodity_workbook.py after moving the folder.",
]


def dated(df: pd.DataFrame, cols) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        df[c] = pd.to_datetime(df[c])
    return df


def format_dates(sh, df: pd.DataFrame, cols):
    for c in cols:
        i = list(df.columns).index(c) + 1
        sh.range((2, i), (max(len(df), 1) + 1, i)).number_format = DATE_FMT


def contracts_sheet(sh, df: pd.DataFrame):
    cols = ["contract", "product", "expiry", "option_expiry", "bbg_ticker", "price_bdp", "price_override", "price",
            "implied_vol"]
    sh.range("A1").value = cols
    header(sh, len(cols))
    n = len(df)
    d = dated(df, ["expiry", "option_expiry"])
    for i, r in enumerate(d.itertuples(), start=2):
        sh.range((i, 1)).value = [r.contract, r.product, r.expiry.to_pydatetime(), r.option_expiry.to_pydatetime(),
                                  r.bbg_ticker]
        sh.range((i, 6)).formula = f'=IFERROR(BDP(E{i},"PX_LAST"),"")'
        sh.range((i, 7)).value = r.price
        sh.range((i, 8)).formula = f'=IF(G{i}<>"",G{i},F{i})'
        sh.range((i, 9)).value = r.implied_vol
    sh.range((2, 1), (n + 1, 5)).color = INPUT_FILL
    sh.range((2, 6), (n + 1, 6)).color = CALC_FILL
    sh.range((2, 7), (n + 1, 7)).color = INPUT_FILL
    sh.range((2, 8), (n + 1, 8)).color = CALC_FILL
    sh.range((2, 9), (n + 1, 9)).color = INPUT_FILL
    sh.range((2, 3), (n + 1, 4)).number_format = DATE_FMT
    sh.autofit("c")


def build():
    src = quickstart_template(NAME)
    if OUT.exists():
        OUT.unlink()
    shutil.copy(src, OUT)
    ex = example_sheets()

    app = xw.App(visible=False, add_book=False)
    app.display_alerts = False
    try:
        wb = app.books.open(str(OUT))
        first = wb.sheets[0]
        ctl = wb.sheets.add("Control", before=first)
        first.delete()
        ctl.range("A1").value = "Commodity XVA Exposure Calculator  (EE / NEE / PFE / funding exposure / SA-CCR)"
        ctl.range("A1").font.size = 14
        ctl.range("A1").font.bold = True
        ctl.range("A3").value = "Action"
        ctl.range("B3").value = "RUN_ALL"
        ctl.range("B3").color = INPUT_FILL
        validation(ctl.range("B3"), "RUN_ALL,RUN_MC,RUN_ANALYTIC,CLEAR")
        wb.names.add("Action", "=Control!$B$3")
        ctl.range("A4").value = "Status"
        wb.names.add("Status", "=Control!$B$4")
        ctl.range("A3:A4").font.bold = True
        btn = ctl.api.Buttons().Add(ctl.range("D3").left, ctl.range("D3").top, 180, 32)
        btn.OnAction = "SampleCall"
        btn.Caption = "Run selected Action"
        for i, n in enumerate(NOTES):
            ctl.range((7 + i, 1)).value = n
        ctl.range("A7").font.bold = True
        ctl.range("A:A").column_width = 14
        ctl.range("B:B").column_width = 60

        cfg = wb.sheets.add("Config", after=ctl)
        cdf = ex["Config"].copy()
        put_df(cfg, cdf)
        cfg.range("B2").value = pd.Timestamp(VAL_DATE).to_pydatetime()
        cfg.range("B2").number_format = DATE_FMT
        validation(cfg.range((list(cdf.key).index("output_mode") + 2, 2)), "AVERAGE,POINT")
        validation(cfg.range((list(cdf.key).index("event_buckets") + 2, 2)), "NONE,MATURITY,ALL")
        cfg.autofit("c")

        last = cfg
        ct = wb.sheets.add("Contracts", after=last)
        contracts_sheet(ct, ex["Contracts"])
        last = ct
        for name in ("CommodityProducts", "CommodityFactors", "FactorCorrelation", "Curve", "NettingSets"):
            sh = wb.sheets.add(name, after=last)
            put_df(sh, ex[name])
            sh.autofit("c")
            last = sh
        fx = wb.sheets.add("FX", after=last)
        fx.range("A1").value = ["pair", "spot"]
        header(fx, 2)
        last = fx

        tr = wb.sheets.add("Trades", after=last)
        tdf = dated(ex["Trades"][TRADE_COLUMNS], ["fixing_date", "payment_date"])
        put_df(tr, tdf)
        format_dates(tr, tdf, ["fixing_date", "payment_date"])
        tr.autofit("c")

        last = tr
        for name in ["Summary", "Charts"] + [s for s in OUTPUT_SHEETS if s != "Summary"]:
            sh = wb.sheets.add(name, after=last)
            last = sh

        conf = wb.sheets.add("xlwings.conf", after=wb.sheets[-1])
        py = ROOT / ".venv" / "Scripts" / "python.exe"
        conf.range("A1").value = [["Interpreter_Win", str(py)],
                                  ["PYTHONPATH", f"{ROOT / 'src'};{ROOT / 'excel'}"]]
        conf.autofit("c")

        ctl.activate()
        wb.save(str(OUT))
        wb.close()
    finally:
        app.quit()

    (ROOT / "excel" / f"{NAME}.py").write_text(
        f'"""Called by the xlwings SampleCall macro in {NAME}.xlsm."""\n'
        "import xlwings as xw\n\nfrom eqxva.xl_entry import dispatch\n\n\n"
        "def main():\n    dispatch(xw.Book.caller())\n\n\n"
        'if __name__ == "__main__":\n'
        f'    xw.Book(r"{OUT}").set_mock_caller()\n    main()\n', encoding="utf-8")
    print(f"Built {OUT}")


if __name__ == "__main__":
    build()
