# eqxva: exposure calculator for XVA on equity and commodity trades

This calculator produces EE, NEE, PFE, EPE/EEPE, FVA funding exposure (FCA/FBA) and SA-CCR EAD profiles on user-defined time buckets. It handles equity TRS (with or without reset, with initial MTM, and notional on the current or the previous average price), European calls and puts (bought or sold), and any combination of these within a trade (TRS + option, collars, and so on). Many netting sets can be run at once.

There are two front ends, and both drive the same engine:

| Form | How to run |
|---|---|
| Python / CLI | `.venv\Scripts\python -m eqxva run examples\trades_example.xlsx` writes `*_results.xlsx` with charts |
| Excel | `excel\EquityXVA_Exposure.xlsm`: choose an **Action** on the Control sheet and click **Run selected Action** |
| Excel, commodity | `excel\CommodityXVA_Exposure.xlsm`: same engine and button, commodity input sheets. See [Commodity](#commodity) |

## Setup
```
python -m venv .venv
.venv\Scripts\python -m pip install -e .[excel,dev]      # add [bloomberg] for xbbg
.venv\Scripts\python excel\build_workbook.py             # (re)builds the .xlsm with this machine's paths
.venv\Scripts\python excel\build_commodity_workbook.py   # (re)builds the commodity .xlsm
.venv\Scripts\python -m pytest
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
  - `output_mode`: `AVERAGE` (time average over each bucket, the default) or `POINT`. In `AVERAGE` mode a bucket that runs past the last maturity averages the live part over the full bucket width (exposure is 0 after maturity).
  - `event_buckets`: `NONE` (default: the standard buckets only), `MATURITY` (also add each trade's maturity / option expiry to the output of its own netting set) or `ALL` (also TRS equity reset dates). Event dates after the last standard bucket are not added. In `AVERAGE` mode the added bucket ends on the event date, so the bucket before it holds only live exposure and nothing straddles a maturity; in `POINT` mode it is placed one day before the event (the last pre-settlement value). Either way, the area under each profile (value × bucket width) is unchanged.
  - `epe_horizon`
  - `reporting_ccy`: the currency of every exposure output (EE/NEE/PFE, FCA/FBA, SA-CCR EAD). It can be left blank only when a single currency is used throughout.
- **Market / Curve / FX / VolSurface / Correlation**:
  - Market `ccy` is each underlying's currency (spot, strike and option/TRS values are in it). Curve `ccy` tags each row; every currency used (underlyings, CSAs and the reporting currency) needs its own curve.
  - FX: `pair | spot` with 1 BASE = spot QUOTE, e.g. `USDCAD | 1.40` (C$1.40 per US$1); a ticker like `USDCAD Curncy` also works. Crosses are chained through the reporting currency.
  - A blank `ccy` means the reporting currency.
  - In Excel, BDP formulas (PX_LAST, EQY_DVD_YLD_IND, `<tenor>_IMPVOL_<mny>%MNY_DF`, and SOFR OIS `USOSFR*` par rates) feed the values.
  - Any `*override` column wins over the BDP value.
  - The `BBG_REFRESH` action pulls the same fields through xbbg into the override columns.
- **NettingSets**: `collateralized` Y/N, `threshold_cp`, `threshold_bank`, `mta`, `ia_received`, `ia_posted` (+ segregated flag), `mpor_days`, `rounding`, `csa_ccy` (currency of the CSA amounts; blank = reporting currency).
- **Trades**: one row per leg. Legs that share a `trade_id` form one trade.
  - TRS: `direction` RECEIVE_EQUITY/PAY_EQUITY, `shares`, `maturity`, `equity_reset_freq` (NONE/1M/3M…), `funding_reset_freq`, `funding_spread`, `notional_basis` CURRENT/AVERAGE (`BLEND_EXTEND` is an alias of AVERAGE), `avg_price`, `initial_mtm`, `div_passthrough`, `sa_notional_basis` MARKET/STRIKE (blank = STRIKE for AVERAGE / blend & extend, MARKET otherwise).
  - Option: `direction` BUY/SELL, `option_type`, `strike` (number or `90%`), `option_maturity`, `vol_override`, `ovme_price`.
    A numeric strike below 20% of spot raises a warning (usually a moneyness typed as a number). In the workbook a strike cell with a % number format is read as moneyness, so `111.21%` means 111.21% of spot either way.
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
- **Off-market start:**
  - *Instantaneous price shock*: the Trades column `spot_shock` (e.g. `50%` or 0.5, the same on every leg of a trade; blank = none) says the spot moved by that much right after the trade was struck. The Market sheet keeps the real current spot, so the same trade can run unshocked and shocked side by side in different netting sets.
    - The trade terms stay at the current spot S: the CURRENT-basis TRS notional (shares·S) and any `%` strike.
    - Valuation, simulation and the SA-CCR notional use S·(1 + shock), so MTM and SA notional reflect the move.
    - Internally the trade is priced on a shocked copy of its underlying, named e.g. `SPCX+50%`. That copy uses the same random numbers as the market underlying (so its paths are the market paths scaled when vol is flat) and counts as the same SA-CCR entity. Adding shocked trades never changes the results of the unshocked ones.
    - Vols are sticky strike: the surface stays quoted against S, an option keeps the vol of its strike, and the simulation uses the vol at the new ATM (moneyness 1 + shock).
    - LegMTM shows `underlying` (the shocked copy), `spot`, `trade_spot` and `spot_shock`. The Warnings sheet lists each shocked trade.
    - A value over 1000% is rejected: enter 50% or 0.5, not 50.
  - *Blend & extend*: enter the restructured trade with its new maturity and `notional_basis` AVERAGE (or BLEND_EXTEND), with `avg_price` = the previous strike. The current MTM is carried in the strike and extended to the new maturity. The SA-CCR adjusted notional defaults to shares × previous strike (`sa_notional_basis` STRIKE). After an equity reset it becomes shares × the reset price fixed on the path. Set `sa_notional_basis` MARKET to use shares × current price, the CRE52 default.
  - With `equity_reset_freq` set, the MTM is still carried to the **new maturity**, not settled at the first reset. This is conservative, and matches clients deferring an MTM owed to the bank:
    - Every equity period is struck at market: the first at the (pre-shock) spot, later ones at the path reset price.
    - The extension MTM, ω·shares·(S_pre − previous strike), is a deferred amount. It accretes at OIS and is paid at maturity, so it stays in the exposure for the whole life.
    - The SA notional stays at shares × previous strike for life.
    - LegMTM shows `notional_basis`, `carried_mtm` and `carried_mtm_settle`.
    - Any `initial_mtm` entered is ignored with a warning, since the carried MTM comes from `avg_price`.
    - `AVERAGE` with resets keeps its own behaviour: the carried MTM settles at the first reset.
  - The two combine: a blend & extend trade under a shock is valued at the shocked spot and keeps the previous strike as its SA notional.
  - Collateral under a shock: VM is computed from the shocked V, so the CSA is treated as already margined against the shocked MTM at t = 0. A separately entered `initial_mtm` on a CURRENT TRS is added on top of the move caused by the shock.
- **Currency:** each leg is valued in its underlying's currency on that currency's curve, converted to the CSA currency, where collateral and SA-CCR are computed, then converted to the reporting currency. FX is deterministic: X(t) = X(0)·P_from(t)/P_to(t), the forward implied by the two curves, so discounted EE is the same PV in any reporting currency. LegMTM keeps `mtm0` and the per-share price in the leg currency (for OVME) and adds `leg_ccy` and `mtm0_reporting`.
- **Settlement convention:** cash flows on a grid date are treated as settled. The grid also includes the point one day before each event, so pre-settlement peaks are captured.
- **Collateral:** VM(t) = CSA(V(t − MPOR)), with thresholds, MTA and rounding. IA received reduces exposure. Exposure is zero once the netting set has matured.
- **Bucket convention (for downstream CVA/DVA/FVA):** each EE_NEE / TradeExposure row has `bucket_start`, `bucket_date` and `bucket_type` (STANDARD or EVENT). In `AVERAGE` mode the value is the time-average over (bucket_start, bucket_date]; hold it constant over that interval rather than interpolating between bucket dates. Netting sets can have different bucket dates when `event_buckets` is not `NONE`.
- **Outputs per netting set:** EE/NEE/PFE collateralized and uncollateralized, discounted EE/NEE, FCA/FBA = E[(V − CSA(V(t)))±] (funding uses the margin held today, with no MPOR lag), EPE, ENE, EEPE (1y), and standalone profiles per trade.
- **Analytic engine:** Gauss–Legendre quadrature over (S_reset, S_t). It gives exact uncollateralized EE/NEE for a netting set on a single underlying, and is reported side by side with MC in `AnalyticCheck`.
- **SA-CCR (CRE52, equity):**
  - Supervisory factors 32% for single names and 20% for indices; correlations 50%/80%; supervisory option vol 120%/75%.
  - Supervisory delta for options; maturity factor unmargined or margined.
  - Adjusted notional d = shares × current price (`MARKET`), or for a TRS with `sa_notional_basis` STRIKE, shares × the reference price of the equity period in force. SACCR_Legs shows the basis used.
  - For reset TRS, M is the time to the next reset (CRE52.37(5)), floored at 10 business days (CRE52.48).
  - RC is margined or unmargined (with TH + MTA − NICA); the PFE multiplier applies, α = 1.4, and margined EAD is capped at the unmargined EAD, path by path. RC, AddOn, multiplier and PFE are reported for whichever calculation gives the EAD, so EAD = 1.4·(RC + PFE) always reconciles. Summary `SACCR_basis0` says which one applied at t=0 (MARGINED, UNMARGINED_CAP or UNMARGINED), and the SACCR_Legs / SACCR_Entities breakdown follows it.
  - Once every trade in the netting set has matured, all SA-CCR outputs are 0 (no TH + MTA floor on RC).
  - The profile through time is in the EE_NEE sheet:
    - `SACCR_EAD_mean`, `SACCR_RC_mean`, `SACCR_PFE_mean`, `SACCR_AddOn_mean`, `SACCR_multiplier_mean`: path-wise means at the bucket date (point values). `SACCR_cap_share` is the share of paths where the unmargined cap binds.
    - `SACCR_EAD_fwd`: EAD along the deterministic forward path, at the bucket date.
    - `SACCR_EAD_avg`: in `AVERAGE` mode, the time-average of the mean EAD over (bucket_start, bucket_date], the same convention as EE; use it for KVA/capital integration. In `POINT` mode it equals `SACCR_EAD_mean`.
    - SA-CCR uses the first 2,000 simulated paths.
  - The t=0 values are in Summary, with the breakdown in SACCR_Legs / SACCR_Entities.

## Known simplifications
- FX is deterministic (no FX vol, no equity/FX correlation) and there are no quanto structures: a TRS funds in its underlying's currency.
- Rates are deterministic (no rate/equity cross-gamma). Dividends are a flat continuous yield, and there is no business-day adjustment.
- Funding fixings for periods already in progress come from the curve, not from historical fixings.
- The ATM term structure drives the simulation, while the strike-dependent surface is used for repricing.
- Collateral is stateless (MTA applied to the call amount). Payment spikes inside the MPOR (e.g. at option expiry under a CSA) are kept, which is the classical model.
- OVME has no API, so its prices are pasted in for reconciliation only.
- Check the Bloomberg field mnemonics with FLDS for your entitlement. The workbook's CAD curve rows have no tickers: enter your CORRA OIS tickers (SECF) or override values.
- The Checks sheet looks up each trade's zero rate from its currency's Curve rows, which must be sorted by tenor within each currency.

## Commodity
Commodity bullet swaps run on the same engine as equity: the same grid, buckets, CSA and MPOR logic, FCA/FBA exposure, statistics and output sheets. The calculator produces exposure profiles only; it does not calculate CVA, DVA or FVA charges or a hurdle. Equity and commodity trades can share a netting set in a CLI input file. A `Contracts` sheet replaces `Market` when there is no equity.

**The example's prices, vols, factor parameters and correlations are placeholders, not market data.**

### Running the calculation
Run the commands from the project folder. Update the inputs first (see [Before a real run](#before-a-real-run-inputs-to-update)), and close any results file open in Excel, or the run stops with a `PermissionError`.

| To get | Run | Output |
|---|---|---|
| The three maturities standalone, 100,000 paths | `.venv\Scripts\python examples\run_soybean_crush_3_maturities.py` | `examples\soybean_crush_3_maturities_results.xlsx` |
| The three maturities and the netted strip | `.venv\Scripts\python -m eqxva run examples\soybean_crush_inputs.xlsx --paths 100000` | `examples\soybean_crush_inputs_results.xlsx` |
| The same run in Excel | Open `excel\CommodityXVA_Exposure.xlsm`, set Action to `RUN_ALL`, press **Run selected Action** | Output sheets in the workbook |
| Analytic results only, no simulation | Same workbook, Action `RUN_ANALYTIC` | `AnalyticCheck` sheet |

- The two commands read `examples\soybean_crush_inputs.xlsx`. The workbook has its own inputs, so update whichever you run.
- `python -m eqxva run` also takes `--out file.xlsx`, `--seed N` and `--no-analytic`. Without `--paths` it uses `n_paths` from Config. `--no-analytic` also drops ExposureCompare.
- `examples\build_soybean_crush.py` creates the input file if it is missing, then runs it at Config `n_paths`.

### Inputs
- **Contracts**: `contract`, `product`, `expiry`, `option_expiry` (blank = expiry), `price` (in the product's `price_unit`), `implied_vol` (ATM vol of that contract's option), `bbg_ticker`. The workbook adds `price_bdp` and `price_override`, and the override wins.
- **CommodityProducts**: `product`, `hedging_set` (ENERGY, METALS, AGRICULTURAL, OTHER), `commodity_type` (blank = product; ELECTRICITY gets the 40% factor), `lot_size`, `quantity_unit`, `price_unit`, `ccy`. Price units: `C/BU`, `$/BU`, `$/TON`, `C/LB`, `$/LB`, `$/CWT`, `$/BBL`, `$/MMBTU`, `$/MWH`, `$/MT`, `$/OZ`. A `price_unit` that does not match the `quantity_unit` is rejected.
- **CommodityFactors**: `product`, `factor`, `beta`, `vol_ratio`. N rows per product, so N is an input: 2 for agriculture (SHORT with beta > 0, LONG with beta = 0). One factor per product needs `vol_ratio` 1; the others are relative to it.
- **FactorCorrelation**: `factor1`, `factor2`, `rho`, with factors named `PRODUCT.FACTOR`. A blank pair is 0 and raises a warning. A matrix that is not positive semi-definite is rejected.
- **NettingSets**: as for equity, plus optional `sa_alpha` (blank = 1.4, any asset class).
- **Trades**: `leg_type` COMSWAP, one row per leg, with legs sharing a `trade_id` forming one trade. `underlying` is the contract, `direction` BUY (bank pays fixed) or SELL, `lots` or `quantity`, `fixed_price` (quote units; blank = at market), `fixing_date` (blank = contract expiry), `payment_date` (blank = fixing date). A quantity that is not a whole number of lots raises a warning.

### Before a real run: inputs to update
The example's market inputs are placeholders. Update the groups below in order; groups 1 to 4 are required. Bloomberg fields and curve numbers should be confirmed with `FLDS <GO>` for your entitlement.

**1. Trade and legal terms (not on Bloomberg)**

| Sheet and field | What to enter | Source |
|---|---|---|
| Config `valuation_date` | Run date | You |
| Trades `quantity` or `lots` | Crush on 5mm bu: meal 110,000 tons, beans 5mm bu, oil 55mm lb or 900 lots | Term sheet |
| Trades `fixed_price` | Blank = at market. Otherwise per leg, in quote units | Sales or trading |
| Trades `fixing_date` | Example: 14 May 2027, 12 Nov 2027, 12 May 2028 | Term sheet |
| Trades `payment_date` | Blank = fixing date. Enter the real settlement lag | Term sheet |
| NettingSets `collateralized`, thresholds, `mta`, IA, `mpor_days` | The example assumes no CSA | Legal or credit |
| NettingSets `sa_alpha` | 1.4 unless policy says otherwise | Regulatory policy |
| CommodityProducts `commodity_type` | Three types, or one shared type | Regulatory policy |

**2. Market data per contract (Contracts sheet)**

Bloomberg tickers: `S K7 Comdty`, `SMK7 Comdty`, `BOK7 Comdty`, changing the month and year code for each contract.

| Field | How to get it |
|---|---|
| `price` | **Clear `price_override`** so `price_bdp` flows through; a pre-filled override wins. `price_bdp` pulls `PX_LAST`, so for an end-of-day mark paste the settlement price into the override |
| `expiry` | `<ticker> DES <GO>`, or field `LAST_TRADEABLE_DT` |
| `option_expiry` | `<ticker> OMON <GO>`: the expiry of the standard option on that contract |
| `implied_vol` | In `OMON`, the mid implied vol at the strike nearest the futures price. Average the call and put, and enter a decimal such as 0.20 |

Two checks:
- **Z7 option expiry:** the example uses 19 Nov 2027, but the CME rule suggests about 26 Nov 2027. Confirm it in `OMON`.
- **K8 option vols:** May 2028 options may be thinly quoted. If so, interpolate from nearby contracts and record that you did.

**3. Discount curve (Curve sheet)**

Enter the USD SOFR OIS curve as continuously compounded zero rates, not par rates. Use `ICVS <GO>` or `SWDF <GO>` and select the SOFR OIS curve (curve 490; confirm the number). From discount factors, use -ln(DF)/t. Add a CAD curve and `USDCAD` on the FX sheet only if reporting in CAD.

**4. Factor parameters and correlations (CommodityFactors, FactorCorrelation)**

Bloomberg gives the price history, not these parameters. First ask the model owners for the calibrated LiTES parameters for soybeans, meal and oil. If they are not available, estimate them from about 2 to 3 years of daily data:

1. **Prices:** `=BDH("S K7 Comdty","PX_LAST",start,end)` in Excel for a range of contracts, or generics such as `S 1 Comdty`. Calculate log returns within each contract, so roll jumps do not distort them.
2. **`beta` and SHORT `vol_ratio`:** group annualized return vol by time to expiry tau for each product, then fit sigma(tau)^2 = q^2 [k^2 exp(-2 beta tau) + 1 + 2 rho k exp(-beta tau)], where k is `vol_ratio` and rho is the SHORT/LONG correlation. LONG stays at beta 0 and ratio 1.
3. **Correlations:** `CORR <GO>` is a quick cross-check. The workbook rejects an invalid matrix.

| Pair on FactorCorrelation | Estimate from |
|---|---|
| LONG with LONG, across products | Returns of deferred contracts, about 12 months or more out |
| SHORT with SHORT, across products | Slope returns: nearby return minus deferred return |
| SHORT with LONG, within and across products | Slope returns against deferred returns |

**5. Validate the crush vol**

Simulated spread vol is the main model risk:

1. Run the calculation and take `Sigma` from AnalyticCheck.
2. Model crush vol = Sigma / 5,000,000 / sqrt(t), in $/bu per sqrt(year).
3. Compare it with historical vol of `SRK7 Comdty`, `SRZ7 Comdty` and `SRK8 Comdty` (standard deviation of daily changes x sqrt(252)), and with crush option vols in `OMON` if quoted.
4. If the model vol is far off, review the correlations first.

**6. Final settings**
- Config `n_paths`: about 100,000 for reported numbers, because 10,000 paths can be 3 to 5% off. `EE_analytic` in AnalyticCheck is the reference.
- CommodityProducts: confirm `lot_size` with `FUT_CONT_SIZE` (5,000 bu, 100 tons, 60,000 lb). Keep `price_unit` as `C/BU`, `$/TON` and `C/LB` to match Bloomberg's quote units; check with `DES`, because a mismatch makes the price 100x wrong.
- Warnings sheet: read it after the run. The 916.67-lot warning is expected at 55mm lb.

### Soybean crush
Three legs in one trade: BUY meal, BUY oil, SELL beans. On 5,000,000 bu the formula quantities are 110,000 tons and 55,000,000 lb. Enter `lots` 900 instead if lots are contractual (54,000,000 lb). The Dec bullet references X7 beans and Z7 meal and oil, and the example fixes all three on the X7 expiry, following the CME crush. Set `fixing_date` per leg if the term sheet differs: the legs then fix separately, and the position is outright between the two dates. Rounding of the published crush to 1/4 cent is not modelled.

### Methodology
- **Model:** N-factor futures model in its seasonal form, dF/F = sum_i q_i(T) exp(-beta_i (T - t)) dW_i. Factors are stepped exactly, so the grid adds no discretisation error. Futures prices are martingales, and a contract stops moving at its expiry.
- **Calibration:** q_i(T) = vol_ratio_i x q(T), and q(T) is solved in closed form so the model variance to the option expiry equals the contract's implied variance. `beta`, `vol_ratio` and correlations are inputs.
- **Products are simulated directly**, not as a representative product plus a spread, so each leg stays available for SA-CCR and for separate fixings.
- **Swap value:** omega Q (F(t) - K) P(t, pay) before fixing, omega Q (F(fix) - K) P(t, pay) from fixing to payment, 0 from payment.
- **SA-CCR:** d = quantity x current contract price on the path, delta = +1 / -1, M = time to payment. Factor 18% (40% electricity), correlation 40%. Add-ons are summed across hedging sets and across asset classes, and same-type positions offset fully. RC, the multiplier, the margined cap and the MF floors are shared with equity. From fixing to payment a leg has no add-on but stays in RC.
- **Commodity type is a policy input.** Three types for the crush give about 7x the add-on of one shared type, so agree the treatment with regulatory policy. Basis treatment (half factor) is not applied, since CRE52.46 is written for a pair of risk factors.
- **SACCR_Proxy:** per trade, `SA_NOTIONAL` = AddOn / (SF x MF), the single position with the same add-on. It holds at t = 0 for a standalone trade whose legs share SF and MF. Keep the legs for the EAD profile and for netting with other agricultural trades.
- **AnalyticCheck:** with up to 3 independent drivers live, EE/NEE are integrated exactly; otherwise they use the normal approximation. For any commodity netting set it adds `Sigma` (exact standard deviation) and the normal approximation `EE_normal`, `NEE_normal`, `PFE_normal` (mean + 1.9 Sigma). 1.645 Sigma understates the 95% PFE of a spread because it is right-skewed; 1.9 was tested on crush inputs only, so check it against the Monte Carlo PFE.

### ExposureCompare sheet
Written for commodity netting sets when Monte Carlo and the analytic check both run (`RUN_ALL`, or the CLI default). `RUN_ANALYTIC` alone needs no simulation, and gives the analytic columns on AnalyticCheck: `EE_normal`, `NEE_normal`, `AddOn_fwd`, `SACCR_EAD_cons`.

| Column | Meaning |
|---|---|
| `netting_set`, `ccy`, `bucket_start`, `bucket_date`, `bucket_type`, `t` | Bucket, same convention as EE_NEE |
| `EE_sim`, `NEE_sim` | Full simulation (`EE`, `NEE` on EE_NEE), after any CSA |
| `EE_exact`, `NEE_exact` | Closed-form values with no simulation, uncollateralised (`EE_analytic`, `NEE_analytic`) |
| `EE_cons`, `NEE_cons` | Analytic conservative estimate: normal approximation, uncollateralised |
| `SACCR_EAD_sim` | Full simulation, path-wise mean (`SACCR_EAD_avg`, same bucket convention as EE) |
| `SACCR_EAD_cons` | Analytic conservative estimate: alpha x (`EE_cons` + AddOn at forward prices), multiplier 1, no collateral |
| `EE_cons_over_sim`, `NEE_cons_over_sim`, `EAD_cons_over_sim` | Conservative divided by simulated |

**`EE_sim` and `EE_exact` (same for NEE).** These estimate the same quantity with the same model and inputs, by different methods. `EE_sim` averages simulated paths, so it has sampling noise and changes with the seed. `EE_exact` integrates the joint lognormal distribution directly, with no paths.
- On an uncollateralised trade the gap is only Monte Carlo noise: within 0.9% at 100,000 paths on the example, and up to 5% at 10,000 paths.
- Under a CSA, `EE_sim` falls and `EE_exact` does not, so the gap is real. Compare `EE_exact` with `EE_uncoll` on EE_NEE instead.
- With more than 3 live price drivers, such as a netted strip, `EE_exact` is really the normal approximation and equals `EE_cons`.
- "Exact" means exact for the model. Both columns depend on the same vols and correlations.

Use `EE_exact` for an uncollateralised standalone trade, and as a check on the simulation. Use `EE_sim` when there is a CSA.

**`EE_cons` and `NEE_cons`.** The normal approximation using the exact mean and standard deviation. `EE_cons` stayed at or above `EE_exact` in the tests, while `EE_cons_over_sim` can dip just below 1 from simulation noise. `NEE_cons` is larger in size than exact, which is prudent for a cost but flatters a benefit such as DVA or FBA.

**`SACCR_EAD_sim` and `SACCR_EAD_cons`.** These are not two methods for one number: `SACCR_EAD_cons` is deliberately higher, and there is no exact closed-form EAD. Both are projections of EAD at future dates. Today's figure is the t = 0 EAD (`SACCR_EAD0` on Summary), where the two agree for an at-market, uncollateralised swap.

| | `SACCR_EAD_sim` | `SACCR_EAD_cons` |
|---|---|---|
| Method | SA-CCR on each simulated path, then averaged | One formula per date, no paths |
| Formula | alpha x (RC + multiplier x AddOn) per path | alpha x (`EE_cons` + AddOn at forward prices) |
| RC | max(V - collateral, 0) on each path | `EE_cons` |
| Multiplier | Below 1 on paths with negative MTM | Fixed at 1 |
| Add-on prices | Simulated contract prices | Today's contract prices (the forwards) |
| Collateral | CSA, margined calculation and unmargined cap | Ignored |
| Sampling noise | Yes, from the first 2,000 paths | None |

Both use the same alpha, supervisory factor, correlation, maturity factor and commodity types.
- `SACCR_EAD_cons` is higher mainly because it gives no credit for the multiplier, an effect that grows as MTM gets large relative to the add-on, so the gap is widest near maturity. `EE_cons` adds a little.
- The add-on rises when prices move, so forward prices understate the average add-on. This is why `SACCR_EAD_cons` is an estimate, not a proven upper bound, although it stayed above `SACCR_EAD_sim` in every live bucket tested (about 2 to 19% on the example).
- Under a CSA, `SACCR_EAD_sim` falls sharply and `SACCR_EAD_cons` does not, so the ratio stops being meaningful.

Use `SACCR_EAD_sim` for the EAD profile, especially with a CSA. Use `SACCR_EAD_cons` for a quick indication without simulation, or as a sanity check. If it falls below `SACCR_EAD_sim` on real inputs, investigate before relying on it.

### Commodity simplifications
- Spread exposure is driven mainly by the correlation inputs. Simulated spreads do not mean-revert, so check the implied crush vol against crush options or history.
- `beta`, `vol_ratio` and correlations are entered by hand: the PCA calibration to historical returns is not built. The time-dependent volatility form p(t) for non-seasonal products and the spread model for non-representative products are also not built.
- The 2-factor model has no crop-year break: correlation between contracts depends only on maturity distance.
- Bullet swaps only: no averaging swaps or commodity options. No `spot_shock` on COMSWAP legs. No equity/commodity correlation (independent random numbers). Rates and FX are deterministic.
- The commodity workbook has no Checks sheet and no BBG_REFRESH action: prices come from BDP or overrides, and implied vols are typed in.
