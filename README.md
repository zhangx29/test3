# eqxva: exposure calculator for XVA on equity TRS and options

This calculator produces EE, NEE, PFE, EPE/EEPE, FVA funding exposure (FCA/FBA) and SA-CCR EAD profiles on user-defined time buckets. It handles equity TRS (with or without reset, with initial MTM, and notional on the current or the previous average price), European calls and puts (bought or sold), and any combination of these within a trade (TRS + option, collars, and so on). Many netting sets can be run at once.

There are two front ends, and both drive the same engine:

| Form | How to run |
|---|---|
| Python / CLI | `.venv\Scripts\python -m eqxva run examples\trades_example.xlsx` writes `*_results.xlsx` with charts |
| Excel | `excel\EquityXVA_Exposure.xlsm`: choose an **Action** on the Control sheet and click **Run selected Action** |

## Setup
```
python -m venv .venv
.venv\Scripts\python -m pip install -e .[excel,dev]      # add [bloomberg] for xbbg
.venv\Scripts\python excel\build_workbook.py             # (re)builds the .xlsm with this machine's paths
.venv\Scripts\python -m pytest                           # 22 tests
```
- To run the Excel front end you need Excel and xlwings.
- The workbook calls `excel\EquityXVA_Exposure.py`. **Keep the two file names identical**: xlwings imports the module whose name matches the workbook.
- The `xlwings.conf` sheet holds the interpreter path and PYTHONPATH. Rebuild the workbook after moving the folder.

## Inputs (same sheets in the .xlsm and in a CLI .xlsx; `python -m eqxva template my.xlsx` writes an example)
- **Config**:
  - `valuation_date`
  - `buckets`: tenors and dates such as `1M,3M,6M,1Y,2Y`, or `EVERY:3M`
  - `n_paths`, `seed`, `pfe_quantile`
  - `fine_step`: the internal simulation step
  - `output_mode`: `AVERAGE` (time average over each bucket, the default) or `POINT`
  - `epe_horizon`
- **Market / Curve / VolSurface / Correlation**:
  - In Excel, BDP formulas (PX_LAST, EQY_DVD_YLD_IND, `<tenor>_IMPVOL_<mny>%MNY_DF`, and SOFR OIS `USOSFR*` par rates) feed the values.
  - Any `*override` column wins over the BDP value.
  - The `BBG_REFRESH` action pulls the same fields through xbbg into the override columns.
- **NettingSets**: `collateralized` Y/N, `threshold_cp`, `threshold_bank`, `mta`, `ia_received`, `ia_posted` (+ segregated flag), `mpor_days`, `rounding`.
- **Trades**: one row per leg. Legs that share a `trade_id` form one trade.
  - TRS: `direction` RECEIVE_EQUITY/PAY_EQUITY, `shares`, `maturity`, `equity_reset_freq` (NONE/1M/3M…), `funding_reset_freq`, `funding_spread`, `notional_basis` CURRENT/AVERAGE, `avg_price`, `initial_mtm`, `div_passthrough`.
  - Option: `direction` BUY/SELL, `option_type`, `strike` (number or `90%`), `option_maturity`, `vol_override`, `ovme_price`.
  - Dates can be given as a tenor or as a date.

## Methodology
- **Sign:** bank view; a positive value means the counterparty owes the bank.
- **Simulation:** risk-neutral GBM with exact lognormal steps. It uses the forward rate from the OIS curve, a continuous dividend yield and ATM forward variance. Paths are antithetic, and correlated underlyings use the correlation sheet.
- **TRS valuation:** each equity period is valued in closed form given S_t and the period's notional fixing. That covers:
  - the equity return with dividend pass-through;
  - the floating funding leg plus spread;
  - future periods, whose notional is re-struck on the forward;
  - the carried initial MTM, settled at the first reset.

  With `AVERAGE` basis the extension MTM is already in the strike, shares·(S0 − avg_price). A separate `initial_mtm` is then ignored with a warning, so it is never counted twice.
- **Options:** Black-Scholes with a sticky-strike vol from the surface.
- **Settlement convention:** cash flows on a grid date are treated as settled. The grid also includes the point one day before each event, so pre-settlement peaks are captured.
- **Collateral:** VM(t) = CSA(V(t − MPOR)), with thresholds, MTA and rounding. IA received reduces exposure. Exposure is zero once the netting set has matured.
- **Outputs per netting set:** EE/NEE/PFE collateralized and uncollateralized, discounted EE/NEE, FCA/FBA = E[(V − CSA(V(t)))±] (funding uses the margin held today, with no MPOR lag), EPE, ENE, EEPE (1y), and standalone profiles per trade.
- **Analytic engine:** Gauss–Legendre quadrature over (S_reset, S_t). It gives exact uncollateralized EE/NEE for a netting set on a single underlying, and is reported side by side with MC in `AnalyticCheck`.
- **SA-CCR (CRE52, equity):**
  - Supervisory factors 32% for single names and 20% for indices; correlations 50%/80%; supervisory option vol 120%/75%.
  - Supervisory delta for options; maturity factor unmargined or margined.
  - For reset TRS, M is the time to the next reset (CRE52.37(5)), floored at 10 business days (CRE52.48).
  - RC is margined or unmargined (with TH + MTA − NICA); the PFE multiplier applies, α = 1.4, and margined EAD is capped at the unmargined EAD.
  - The profile through time is reported both as the path-wise mean EAD and as the EAD along the deterministic forward path.

## Known simplifications
- Rates are deterministic (no rate/equity cross-gamma). Dividends are a flat continuous yield, and there is no business-day adjustment.
- Funding fixings for periods already in progress come from the curve, not from historical fixings.
- The ATM term structure drives the simulation, while the strike-dependent surface is used for repricing.
- Collateral is stateless (MTA applied to the call amount). Payment spikes inside the MPOR (e.g. at option expiry under a CSA) are kept, which is the classical model.
- OVME has no API, so its prices are pasted in for reconciliation only.
- Check the Bloomberg field mnemonics with FLDS for your entitlement.
