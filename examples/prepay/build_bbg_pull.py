"""Build examples\\prepay\\prepay_bbg_pull.xlsx: Bloomberg Excel add-in formulas for the gas prepay inputs.

    .venv\\Scripts\\python examples\\prepay\\build_bbg_pull.py [--curve examples\\prepay\\ng_curve.csv]

Open the output on a Bloomberg terminal PC (Excel add-in loaded), let it refresh, check the flags,
then copy the Export sheet's two columns into a month,price CSV for run_prepay_scenarios.py --curve.

Sheets
  Setup      as-of date, ticker settings, notes
  Contracts  one row per strip month Dec26..Jan58: ticker, BDP price / last trade date / open interest,
             override, fallback (current curve CSV + seasonal extension), price_used, vol paste column
  Export     month | price | implied_vol  (formulas pointing at Contracts) -> save as CSV
  History    BDH daily PX_LAST for generics NG1..NG36 (factor / correlation fit)
  Curve      BDS SOFR OIS curve members (verify the curve id in ICVS) -> zero rates for the Curve sheet
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from eqxva import dates as dt
from eqxva.commodity import prepay_examples as pe

HERE = Path(__file__).resolve().parent
CODE = "FGHJKMNQUVXZ"
FIRST = date(2026, 12, 1)          # Dec26 (prompt at the curve date) so the screenshot rows reconcile
N_ROWS = pe.N_MONTHS + 2           # Dec26 + Jan27..Dec57 + one spare month (Jan58)
N_GENERIC = 36
BOLD = Font(bold=True)
INPUT = PatternFill("solid", fgColor="FFF2CC")      # yellow = user input
PULL = PatternFill("solid", fgColor="DDEBF7")       # blue = Bloomberg formula


def ticker(d: date, single_digit_until: int) -> str:
    """Bloomberg futures ticker. Active contracts use a 1-digit year; years past the ambiguity point
    use 2 digits. single_digit_until is the last year written with one digit (check in Setup)."""
    yy = f"{d.year % 10}" if d.year <= single_digit_until else f"{d.year % 100:02d}"
    return f"NG{CODE[d.month - 1]}{yy} Comdty"


def build(out: Path, curve_csv: Path | None, single_digit_until: int):
    fallback = {}
    if curve_csv:
        curve = pe.load_curve(curve_csv)
        ext = pe.curve_prices(curve)                    # Jan27.. with seasonal extension
        for i, p in enumerate(ext):
            d = pe.month_start(i)
            fallback[(d.year, d.month)] = p
        fallback.update(curve)                          # includes Dec26
    wb = Workbook()

    # ---------------- Setup
    s = wb.active
    s.title = "Setup"
    rows = [
        ("Gas prepay - Bloomberg pull", None),
        (None, None),
        ("as_of", date.today()),
        ("history_start", "=EDATE(B3,-36)"),
        ("price_field", "PX_SETTLE"),
        ("min_open_interest", 1),
        (None, None),
        ("Notes", None),
        ("1. Needs Excel with the Bloomberg add-in. Blue cells are BDP/BDH/BDS formulas; yellow cells are inputs.", None),
        ("2. Contracts!price_used = override, else the Bloomberg price if the contract is listed and has open "
         "interest, else fallback (the current curve CSV, with the 2033 seasonal shape repeated past Jan34).", None),
        ("3. Tickers: NG + month code + year. Contracts up to the year in B13 use a 1-digit year, later ones 2 "
         "digits. If a far contract shows #N/A, test both forms in the terminal (e.g. NGF36 vs NGF6) and rebuild with "
         "--single-digit-until.", None),
        ("4. Implied vol: no BDP field is assumed. Paste the ATM mid vol from <ticker> OMON <GO> (average call/put "
         "at the strike nearest the futures, decimal e.g. 0.55) into Contracts!implied_vol_paste, or the desk's "
         "NG vol surface. Leave blank past the listed options; the engine needs a value for every month, so fill "
         "by extrapolation and record that you did.", None),
        ("single_digit_until", single_digit_until),
        ("5. Curve sheet: confirm the SOFR OIS curve id in ICVS <GO> (README says 490). Convert to continuously "
         "compounded zero rates (-ln(DF)/t) for the engine's Curve sheet.", None),
        ("6. Export sheet -> File > Save As CSV (month,price[,implied_vol]) and run "
         "run_prepay_scenarios.py --curve <csv> --name <new name> --rebuild.", None),
        ("ALL OUTPUT OF THIS SHEET IS MARKET DATA ONLY; deal terms (volume, fixed prices, CSRA, IM) come from the "
         "term sheet.", None),
    ]
    for r, (a, b) in enumerate(rows, 1):
        s.cell(r, 1, a)
        if b is not None:
            c = s.cell(r, 2, b)
            if r in (3, 5, 6, 13):
                c.fill = INPUT
    s["A1"].font = Font(bold=True, size=13)
    s["B3"].number_format = s["B4"].number_format = "yyyy-mm-dd"
    s.column_dimensions["A"].width = 20
    s.column_dimensions["B"].width = 14

    # ---------------- Contracts
    c = wb.create_sheet("Contracts")
    hdr = ["month", "engine_contract", "bbg_ticker", "px_bbg", "last_trade_dt", "open_int",
           "price_override", "fallback_price", "price_used", "source", "implied_vol_paste", "screenshot_check"]
    for j, h in enumerate(hdr, 1):
        c.cell(1, j, h).font = BOLD
    for k in range(N_ROWS):
        r = k + 2
        d = dt.add_months(FIRST, k)
        tk = ticker(d, single_digit_until)
        c.cell(r, 1, f"{d.year}-{d.month:02d}")
        c.cell(r, 2, f"NG{CODE[d.month - 1]}{d.year % 100:02d}")
        c.cell(r, 3, tk)
        c.cell(r, 4, f'=IFERROR(BDP(C{r},Setup!$B$5),"")').fill = PULL
        e = c.cell(r, 5, f'=IFERROR(BDP(C{r},"LAST_TRADEABLE_DT"),"")')
        e.fill, e.number_format = PULL, "yyyy-mm-dd"
        c.cell(r, 6, f'=IFERROR(BDP(C{r},"OPEN_INT"),"")').fill = PULL
        c.cell(r, 7).fill = INPUT
        fb = fallback.get((d.year, d.month))
        if fb is not None:
            c.cell(r, 8, round(fb, 4))
        c.cell(r, 9, f'=IF(ISNUMBER(G{r}),G{r},IF(AND(ISNUMBER(D{r}),N(F{r})>=Setup!$B$6),D{r},'
                     f'IF(ISNUMBER(H{r}),H{r},NA())))')
        c.cell(r, 10, f'=IF(ISNUMBER(G{r}),"override",IF(AND(ISNUMBER(D{r}),N(F{r})>=Setup!$B$6),"bloomberg",'
                      f'IF(ISNUMBER(H{r}),"fallback","MISSING")))')
        c.cell(r, 11).fill = INPUT
        if fb is not None and curve_csv and (d.year, d.month) in curve:
            c.cell(r, 12, f'=IF(ISNUMBER(D{r}),D{r}-H{r},"")')   # Bloomberg minus screenshot
    for j, w in enumerate([9, 15, 16, 10, 13, 10, 13, 13, 11, 10, 16, 15], 1):
        c.column_dimensions[get_column_letter(j)].width = w
    c.freeze_panes = "D2"
    last = N_ROWS + 1
    c.cell(last + 2, 1, "counts").font = BOLD
    for j, lab in enumerate(["bloomberg", "fallback", "override", "MISSING"]):
        c.cell(last + 3 + j, 1, lab)
        c.cell(last + 3 + j, 2, f'=COUNTIF($J$2:$J${last},A{last + 3 + j})')

    # ---------------- Export (month, price, implied_vol) for the CSV
    x = wb.create_sheet("Export")
    for j, h in enumerate(["month", "price", "implied_vol"], 1):
        x.cell(1, j, h).font = BOLD
    for k in range(N_ROWS):
        r = k + 2
        x.cell(r, 1, f"=Contracts!A{r}")
        x.cell(r, 2, f"=Contracts!I{r}")
        x.cell(r, 3, f'=IF(ISNUMBER(Contracts!K{r}),Contracts!K{r},"")')

    # ---------------- History: BDH per generic, 2 columns each (date, PX_LAST) + a spacer
    h = wb.create_sheet("History")
    h.cell(1, 1, "Daily PX_LAST of NG generics (rolling front = NG1). For the factor fit, prefer returns "
                 "within each specific contract (README section 4) - add contract tickers the same way.").font = BOLD
    for g in range(1, N_GENERIC + 1):
        col = 1 + (g - 1) * 3
        tk = f"NG{g} Comdty"
        h.cell(3, col, tk).font = BOLD
        h.cell(4, col, f'=BDH("{tk}","PX_LAST",Setup!$B$4,Setup!$B$3,"Dir=V","Dts=S","Fill=P")').fill = PULL
        h.column_dimensions[get_column_letter(col)].width = 11
        h.column_dimensions[get_column_letter(col + 1)].width = 8
        h.column_dimensions[get_column_letter(col + 2)].width = 2

    # ---------------- Curve: SOFR OIS members
    v = wb.create_sheet("Curve")
    v["A1"] = "SOFR OIS curve: confirm the curve id in ICVS <GO> (YCSW0490 Index assumed; change B2 if needed)."
    v["A1"].font = BOLD
    v["A2"], v["B2"] = "curve_ticker", "YCSW0490 Index"
    v["B2"].fill = INPUT
    v["A4"] = "Members (tenor, ticker, rate) - spills down:"
    v["A5"] = '=BDS(B2,"CURVE_TENOR_RATES")'
    v["A5"].fill = PULL
    v["F4"] = ("Then enter engine Curve rows as tenor | zero_rate (continuous, decimal) | USD. If the curve "
               "gives par rates, bootstrap or use the discount factors from SWDF / ICVS: zero = -ln(DF)/t.")
    wb.save(out)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--curve", default=str(HERE / "ng_curve.csv"))
    ap.add_argument("--out", default=str(HERE / "prepay_bbg_pull.xlsx"))
    ap.add_argument("--single-digit-until", type=int, default=2035)
    a = ap.parse_args(argv)
    p = build(Path(a.out), Path(a.curve) if a.curve else None, a.single_digit_until)
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
