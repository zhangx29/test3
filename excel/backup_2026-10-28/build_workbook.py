"""Build excel/EquityXVA_Exposure.xlsm (needs Windows + Excel + xlwings).

    .venv\\Scripts\\python excel\\build_workbook.py

Starts from the xlwings *standalone* quickstart workbook (it embeds the xlwings VBA module and a
`SampleCall` macro that runs `EquityXVA_Exposure.main()`), then adds input sheets with Bloomberg
BDP formulas + manual overrides, a formula-only Checks sheet, output sheets and the xlwings.conf
sheet that points at this project's Python interpreter.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import xlwings as xw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from eqxva.examples import NS_COLUMNS, TRADE_COLUMNS, VAL_DATE, example_sheets  # noqa: E402
from eqxva.xl_entry import OUTPUT_SHEETS  # noqa: E402

NAME = "EquityXVA_Exposure"
OUT = ROOT / "excel" / f"{NAME}.xlsm"
N_ROWS = 100            # formula rows prepared on Trades-driven sheets

BBG_VOL_TENOR = {"1M": "30DAY", "2M": "60DAY", "3M": "3MTH", "6M": "6MTH", "1Y": "12MTH",
                 "18M": "18MTH", "2Y": "24MTH"}
OIS_TICKERS = {"1W": "USOSFR1Z Curncy", "1M": "USOSFRA Curncy", "3M": "USOSFRC Curncy",
               "6M": "USOSFRF Curncy", "1Y": "USOSFR1 Curncy", "2Y": "USOSFR2 Curncy",
               "3Y": "USOSFR3 Curncy", "5Y": "USOSFR5 Curncy", "10Y": "USOSFR10 Curncy"}

HDR_FILL = (31, 78, 121)
INPUT_FILL = (255, 242, 204)
CALC_FILL = (226, 239, 218)


def quickstart_template() -> Path:
    tmp = Path(tempfile.mkdtemp())
    exe = Path(sys.executable).with_name("xlwings.exe")
    cmd = [str(exe)] if exe.exists() else [sys.executable, "-m", "xlwings"]
    subprocess.run(cmd + ["quickstart", NAME, "--standalone"], cwd=tmp, check=True)
    return tmp / NAME / f"{NAME}.xlsm"


def header(sh, ncols, row=1):
    r = sh.range((row, 1), (row, ncols))
    r.color = HDR_FILL
    r.font.color = (255, 255, 255)
    r.font.bold = True


def put_df(sh, df, fill=INPUT_FILL):
    sh.range("A1").options(index=False).value = df
    header(sh, len(df.columns))
    if len(df):
        sh.range((2, 1), (len(df) + 1, len(df.columns))).color = fill


def validation(rng, items: str):
    v = rng.api.Validation
    v.Delete()
    v.Add(3, 1, 1, items)


def build():
    src = quickstart_template()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    shutil.copy(src, OUT)
    ex = example_sheets()

    app = xw.App(visible=False, add_book=False)
    app.display_alerts = False
    try:
        wb = app.books.open(str(OUT))
        first = wb.sheets[0]

        # ---------------------------------------------------------------- Control
        ctl = wb.sheets.add("Control", before=first)
        first.delete()
        ctl.range("A1").value = "Equity XVA Exposure Calculator  (EE / NEE / PFE / funding exposure / SA-CCR)"
        ctl.range("A1").font.size = 14
        ctl.range("A1").font.bold = True
        ctl.range("A3").value = "Action"
        ctl.range("B3").value = "RUN_ALL"
        ctl.range("B3").color = INPUT_FILL
        validation(ctl.range("B3"), "RUN_ALL,RUN_MC,RUN_ANALYTIC,BBG_REFRESH,CLEAR")
        wb.names.add("Action", "=Control!$B$3")
        ctl.range("A4").value = "Status"
        wb.names.add("Status", "=Control!$B$4")
        ctl.range("A3:A4").font.bold = True
        btn = ctl.api.Buttons().Add(ctl.range("D3").left, ctl.range("D3").top, 180, 32)
        btn.OnAction = "SampleCall"
        btn.Caption = "Run selected Action"
        notes = [
            "How to use",
            "1. Market / Curve / VolSurface: BDP formulas pull Bloomberg data when the Bloomberg add-in is loaded; "
            "any value typed in an *override column wins (overrides are pre-filled with example data).",
            "2. NettingSets: one row per netting set / CSA (collateralized Y/N, thresholds, MTA, IA, MPOR in business days).",
            "3. Trades: one row per leg; legs sharing a trade_id form one trade (TRS + options, collars, ...). "
            "Strike as number or text like 90% (column is text-formatted).",
            "4. Config: valuation date, time buckets (e.g. 1M,3M,6M,1Y,2Y or EVERY:3M), paths, seed, PFE quantile, "
            "output_mode AVERAGE (bucket time-average, recommended for CVA/FVA integration) or POINT.",
            "5. Pick an Action and press the button:  RUN_ALL = Monte Carlo + analytic check, RUN_MC, RUN_ANALYTIC, "
            "BBG_REFRESH (Python xbbg -> override columns), CLEAR.",
            "Outputs: Summary (EPE, EEPE, SA-CCR t0), EE_NEE (per netting set, collateralised & uncollateralised, "
            "discounted, FCA/FBA funding exposure, SA-CCR EAD profile), TradeExposure (standalone), LegMTM "
            "(incl. OVME reconciliation), SACCR_Legs / SACCR_Entities, AnalyticCheck, Charts.",
            "Checks: formula-only Black-Scholes / closed-form values per leg vs. the Python engine.",
            "OVME: paste the OVME per-share price in Trades.ovme_price; LegMTM shows engine vs OVME.",
            "Python engine: see xlwings.conf sheet (Interpreter_Win, PYTHONPATH). Re-run excel/build_workbook.py "
            "after moving the project folder.",
        ]
        for i, n in enumerate(notes):
            ctl.range((7 + i, 1)).value = n
        ctl.range("A7").font.bold = True
        ctl.range("A:A").column_width = 14
        ctl.range("B:B").column_width = 60

        # ---------------------------------------------------------------- Config
        cfg = wb.sheets.add("Config", after=ctl)
        cdf = ex["Config"].copy()
        put_df(cfg, cdf)
        cfg.range("B2").value = pd.Timestamp(VAL_DATE).to_pydatetime()
        cfg.range("B2").number_format = "yyyy-mm-dd"
        validation(cfg.range((list(cdf.key).index("output_mode") + 2, 2)), "AVERAGE,POINT")
        cfg.autofit("c")

        # ---------------------------------------------------------------- Market
        mk = wb.sheets.add("Market", after=cfg)
        m = ex["Market"]
        mcols = ["underlying", "bbg_ticker", "is_index", "bbg_spot", "bbg_div_yield", "bbg_atm_vol",
                 "spot_override", "div_yield_override", "vol_override", "spot", "div_yield", "flat_vol"]
        mk.range("A1").value = mcols
        header(mk, len(mcols))
        for i, r in enumerate(m.itertuples(), start=2):
            mk.range((i, 1)).value = [r.underlying, r.bbg_ticker, r.is_index]
            mk.range((i, 4)).formula = f'=IFERROR(BDP(B{i},"PX_LAST"),"")'
            mk.range((i, 5)).formula = f'=IFERROR(BDP(B{i},"EQY_DVD_YLD_IND")/100,"")'
            mk.range((i, 6)).formula = f'=IFERROR(BDP(B{i},"12MTH_IMPVOL_100.0%MNY_DF")/100,"")'
            mk.range((i, 7)).value = [r.spot, r.div_yield, r.flat_vol]
            mk.range((i, 10)).formula = f'=IF(G{i}<>"",G{i},D{i})'
            mk.range((i, 11)).formula = f'=IF(H{i}<>"",H{i},E{i})'
            mk.range((i, 12)).formula = f'=IF(I{i}<>"",I{i},F{i})'
        n = len(m) + 1
        mk.range(f"A2:C{n}").color = INPUT_FILL
        mk.range(f"G2:I{n}").color = INPUT_FILL
        mk.range(f"J2:L{n}").color = CALC_FILL
        mk.autofit("c")

        # ---------------------------------------------------------------- Curve
        cv = wb.sheets.add("Curve", after=mk)
        ccols = ["tenor", "bbg_ticker", "bbg_par_rate_pct", "override", "zero_rate", "t_years"]
        cv.range("A1").value = ccols
        header(cv, len(ccols))
        for i, r in enumerate(ex["Curve"].itertuples(), start=2):
            cv.range((i, 1)).value = [r.tenor, OIS_TICKERS.get(r.tenor, "")]
            cv.range((i, 3)).formula = f'=IFERROR(BDP(B{i},"PX_LAST"),"")'
            cv.range((i, 4)).value = r.zero_rate
            # OIS par rate (annual, %) -> continuous zero (approximation adequate for exposure)
            cv.range((i, 5)).formula = f'=IF(D{i}<>"",D{i},IF(ISNUMBER(C{i}),LN(1+C{i}/100),""))'
            cv.range((i, 6)).formula = (f'=VALUE(LEFT(A{i},LEN(A{i})-1))*CHOOSE(MATCH(RIGHT(A{i},1),'
                                        f'{{"D","W","M","Y"}},0),1/365,7/365,1/12,1)')
        n = len(ex["Curve"]) + 1
        cv.range(f"A2:B{n}").color = INPUT_FILL
        cv.range(f"D2:D{n}").color = INPUT_FILL
        cv.range(f"E2:F{n}").color = CALC_FILL
        cv.autofit("c")

        # ---------------------------------------------------------------- VolSurface
        vs = wb.sheets.add("VolSurface", after=cv)
        vcols = ["underlying", "tenor", "moneyness", "bbg_field", "bbg_vol", "override", "vol"]
        vs.range("A1").value = vcols
        header(vs, len(vcols))
        for i, r in enumerate(ex["VolSurface"].itertuples(), start=2):
            code = BBG_VOL_TENOR.get(r.tenor)
            fld = f"{code}_IMPVOL_{r.moneyness * 100:.1f}%MNY_DF" if code else ""
            vs.range((i, 1)).value = [r.underlying, r.tenor, r.moneyness, fld]
            vs.range((i, 5)).formula = (f'=IF(D{i}="","",IFERROR(BDP(INDEX(Market!$B:$B,MATCH(A{i},'
                                        f'Market!$A:$A,0)),D{i})/100,""))')
            vs.range((i, 6)).value = r.vol
            vs.range((i, 7)).formula = f'=IF(F{i}<>"",F{i},E{i})'
        n = len(ex["VolSurface"]) + 1
        vs.range(f"A2:D{n}").color = INPUT_FILL
        vs.range(f"F2:F{n}").color = INPUT_FILL
        vs.range(f"G2:G{n}").color = CALC_FILL
        vs.autofit("c")

        # ---------------------------------------------------------------- Correlation / NettingSets
        co = wb.sheets.add("Correlation", after=vs)
        put_df(co, ex["Correlation"])
        co.autofit("c")
        ns = wb.sheets.add("NettingSets", after=co)
        put_df(ns, ex["NettingSets"][NS_COLUMNS])
        validation(ns.range(f"C2:C{N_ROWS}"), "Y,N")
        validation(ns.range(f"I2:I{N_ROWS}"), "Y,N")
        ns.autofit("c")

        # ---------------------------------------------------------------- Trades
        tr = wb.sheets.add("Trades", after=ns)
        tdf = ex["Trades"][TRADE_COLUMNS].copy()
        col = {c: i + 1 for i, c in enumerate(TRADE_COLUMNS)}
        tr.range((1, col["strike"]), (N_ROWS + 1, col["strike"])).number_format = "@"
        tdf["strike"] = tdf["strike"].map(lambda x: "" if pd.isna(x) else str(x))
        tdf = tdf.astype(object).where(tdf.notna(), None)
        put_df(tr, tdf)
        L = lambda c: xw.utils.col_name(col[c])  # noqa: E731
        validation(tr.range(f"{L('leg_type')}2:{L('leg_type')}{N_ROWS}"), "TRS,OPTION")
        validation(tr.range(f"{L('direction')}2:{L('direction')}{N_ROWS}"), "RECEIVE_EQUITY,PAY_EQUITY,BUY,SELL")
        validation(tr.range(f"{L('notional_basis')}2:{L('notional_basis')}{N_ROWS}"), "CURRENT,AVERAGE")
        validation(tr.range(f"{L('option_type')}2:{L('option_type')}{N_ROWS}"), "CALL,PUT")
        validation(tr.range(f"{L('equity_reset_freq')}2:{L('equity_reset_freq')}{N_ROWS}"),
                   "NONE,1W,1M,3M,6M,1Y")
        tr.autofit("c")

        # ---------------------------------------------------------------- outputs (placeholders)
        last = tr
        for name in ["Summary", "Charts"] + [s for s in OUTPUT_SHEETS if s != "Summary"]:
            sh = wb.sheets.add(name, after=last)
            last = sh
        lm = wb.sheets["LegMTM"]
        lm.range("A1").value = ["netting_set", "trade_id", "leg_id", "leg_type", "underlying", "mtm0"]

        # ---------------------------------------------------------------- Checks (formula only)
        ck = wb.sheets.add("Checks", after=tr)
        build_checks(ck, col)

        # ---------------------------------------------------------------- xlwings.conf
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

    # python module called by the SampleCall macro (module name = workbook name)
    (ROOT / "excel" / f"{NAME}.py").write_text(
        '"""Called by the xlwings SampleCall macro in EquityXVA_Exposure.xlsm."""\n'
        "import xlwings as xw\n\nfrom eqxva.xl_entry import dispatch\n\n\n"
        "def main():\n    dispatch(xw.Book.caller())\n\n\n"
        'if __name__ == "__main__":\n'
        f'    xw.Book(r"{OUT}").set_mock_caller()\n    main()\n', encoding="utf-8")
    print(f"Built {OUT}")


def build_checks(ck, col):
    T = {c: f"Trades!${xw.utils.col_name(i)}" for c, i in col.items()}
    ck.range("A1").value = ["EE horizon t (yrs)", 0.5, "", "Valuation date", "=Config!B2"]
    ck.range("B1").color = INPUT_FILL
    ck.range("E1").number_format = "yyyy-mm-dd"
    ck.range("A2").value = ("Formula-only closed forms: options = Black-Scholes (flat vol, step zero rate); TRS MTM0 is "
                            "approximate (spread annuity on initial notional); standalone EE at t: long option = "
                            "V0/DF(t), short option = 0, no-reset TRS = Black on the forward (spread ignored).")
    cols = ["trade_id", "leg_id", "leg_type", "underlying", "S0", "q", "vol", "T_yrs", "r(T)", "K_or_ref",
            "sign", "shares", "d1", "MTM0_excel", "MTM0_python", "diff", "EE_t_excel"]
    ck.range("A3").value = cols
    header(ck, len(cols), row=3)
    val = "Config!$B$2"
    for k in range(N_ROWS):
        i = k + 4                 # Checks row
        j = k + 2                 # Trades row
        tid = f"{T['trade_id']}{j}"
        f = {}
        f["A"] = f'=IF({tid}="","",{tid})'
        f["B"] = f'=IF(A{i}="","",{T["leg_id"]}{j})'
        f["C"] = f'=IF(A{i}="","",UPPER({T["leg_type"]}{j}))'
        f["D"] = f'=IF(A{i}="","",{T["underlying"]}{j})'
        f["E"] = f'=IF(A{i}="","",INDEX(Market!$J:$J,MATCH(D{i},Market!$A:$A,0)))'
        f["F"] = f'=IF(A{i}="","",INDEX(Market!$K:$K,MATCH(D{i},Market!$A:$A,0)))'
        f["G"] = (f'=IF(A{i}="","",IF(AND(C{i}="OPTION",{T["vol_override"]}{j}<>""),{T["vol_override"]}{j},'
                  f'INDEX(Market!$L:$L,MATCH(D{i},Market!$A:$A,0))))')
        x = f'IF(C{i}="OPTION",{T["option_maturity"]}{j},{T["maturity"]}{j})'
        f["H"] = (f'=IF(A{i}="","",IF(ISNUMBER({x}),({x}-{val})/365,VALUE(LEFT({x},LEN({x})-1))*'
                  f'CHOOSE(MATCH(RIGHT({x},1),{{"D","W","M","Y"}},0),1/365,7/365,1/12,1)))')
        f["I"] = f'=IF(A{i}="","",IFERROR(INDEX(Curve!$E:$E,MATCH(H{i},Curve!$F:$F,1)),Curve!$E$2))'
        k_opt = (f'IF(ISNUMBER({T["strike"]}{j}),{T["strike"]}{j},IF(RIGHT({T["strike"]}{j},1)="%",'
                 f'VALUE(SUBSTITUTE({T["strike"]}{j},"%",""))/100*E{i},VALUE({T["strike"]}{j})))')
        f["J"] = (f'=IF(A{i}="","",IF(C{i}="OPTION",{k_opt},IF(UPPER({T["notional_basis"]}{j})="AVERAGE",'
                  f'{T["avg_price"]}{j},E{i})))')
        f["K"] = (f'=IF(A{i}="","",IF(OR(UPPER({T["direction"]}{j})="BUY",'
                  f'UPPER({T["direction"]}{j})="RECEIVE_EQUITY"),1,-1))')
        f["L"] = f'=IF(A{i}="","",{T["shares"]}{j})'
        f["M"] = f'=IF(A{i}="","",(LN(E{i}/J{i})+(I{i}-F{i}+G{i}^2/2)*H{i})/(G{i}*SQRT(H{i})))'
        call = f'(E{i}*EXP(-F{i}*H{i})*NORM.S.DIST(M{i},TRUE)-J{i}*EXP(-I{i}*H{i})*NORM.S.DIST(M{i}-G{i}*SQRT(H{i}),TRUE))'
        put = f'(J{i}*EXP(-I{i}*H{i})*NORM.S.DIST(-(M{i}-G{i}*SQRT(H{i})),TRUE)-E{i}*EXP(-F{i}*H{i})*NORM.S.DIST(-M{i},TRUE))'
        im = f'IF(UPPER({T["notional_basis"]}{j})="AVERAGE",0,N({T["initial_mtm"]}{j}))'
        trs = (f'K{i}*L{i}*(E{i}-J{i})+{im}-K{i}*L{i}*J{i}*N({T["funding_spread"]}{j})*H{i}*365/360'
               f'*EXP(-I{i}*H{i}/2)')
        f["N"] = (f'=IF(A{i}="","",IF(C{i}="OPTION",K{i}*L{i}*IF(LEFT(UPPER({T["option_type"]}{j}),1)="C",'
                  f'{call},{put}),{trs}))')
        f["O"] = f'=IF(A{i}="","",SUMIFS(LegMTM!$F:$F,LegMTM!$B:$B,A{i},LegMTM!$C:$C,B{i}))'
        f["P"] = f'=IF(A{i}="","",N{i}-O{i})'
        t = "$B$1"
        d1t = f'((LN(E{i}/J{i})-F{i}*{t}+G{i}^2/2*{t})/(G{i}*SQRT({t})))'
        fwd = f'(E{i}*EXP((I{i}-F{i})*{t}))'
        kt = f'(J{i}*EXP(I{i}*{t}))'
        trs_ee = (f'IF(K{i}>0,L{i}*({fwd}*NORM.S.DIST({d1t},TRUE)-{kt}*NORM.S.DIST({d1t}-G{i}*SQRT({t}),TRUE)),'
                  f'L{i}*({kt}*NORM.S.DIST(-({d1t}-G{i}*SQRT({t})),TRUE)-{fwd}*NORM.S.DIST(-{d1t},TRUE)))')
        reset = f'UPPER({T["equity_reset_freq"]}{j})'
        f["Q"] = (f'=IF(A{i}="","",IF({t}>=H{i},0,IF(C{i}="OPTION",IF(K{i}>0,N{i}*EXP(I{i}*{t}),0),'
                  f'IF(OR({reset}="",{reset}="NONE"),{trs_ee},"reset: see AnalyticCheck"))))')
        for c, v in f.items():
            ck.range(f"{c}{i}").formula = v
    ck.range("E4:Q200").number_format = "#,##0.0000"
    ck.range("N4:Q200").number_format = "#,##0.00"
    ck.autofit("c")
    ck.range("A2").wrap_text = False


if __name__ == "__main__":
    build()
