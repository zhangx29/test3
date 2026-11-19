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

## Gas prepay swaps
Background and conclusions are in `docs/prepay/context.md`. The front-end swap with the Issuer and the back-end swap with the SPV are ordinary monthly COMSWAP strips, one leg per monthly contract, in two netting sets. Cross-extinguishment, static collateral and the SA-CCR treatments are handled by the inputs below. The engine still stops at exposure and EAD: CVA, lost spread, KVA and RWA are computed downstream from these profiles.

**The example's vols, factor parameters, discount rate and CSRA/IM sizing are placeholders, not market data.** By default the forward curve is flat at $5.00. `--curve` replaces it with a real NG curve; see "Before a real run" below for what is still a placeholder after that.

### Running the scenarios
`.venv\Scripts\python examples\prepay\run_prepay_scenarios.py [--paths N] [--scenarios W7,F7,...] [--vm-posting] [--no-analytic] [--rebuild] [--curve file.csv] [--name base]`
- The input file is `examples\prepay\<name>.xlsx` (default `prepay_inputs`). It is generated by `eqxva.commodity.prepay_examples` if missing, or regenerated with `--rebuild`. **`--rebuild` overwrites any edits made in that workbook**, so leave it off after editing inputs by hand.
- `--curve` takes a forward-curve CSV with columns `month` (YYYY-MM) and `price` ($/MMBtu); `examples\prepay\ng_curve.csv` holds the NYMEX NG settles Dec26–Jan34 used so far. It is applied only when the workbook is (re)built. Months after the CSV's last point repeat its last 12 months (seasonal shape held flat), and months before its first point take the first price. Use a new `--name` per curve so earlier results are kept, e.g. `--curve examples\prepay\ng_curve.csv --name prepay_inputs_ngcurve --rebuild`.
- The results file is `examples\prepay\<name>_results.xlsx`. It has the usual sheets plus `PrepayScenarios`, a side-by-side table of peak EE, EPE, lifetime-average EE, peak PFE, `SACCR_EAD0`, `SA_NOTIONAL_TOTAL`/`AMORT` and M for every netting set.

| Scenario | Booking | SA-CCR |
|---|---|---|
| `W7` / `W30` | Strip Jan27–Dec33 / Jan27–Dec57, `window_months` 2 | Two-month notional (the fixed month included), M to the Dec 2033 / Dec 2057 end |
| `F7` / `F30` | Full strip, no window | Full notional, `sa_maturity` TRADE (literal reading) |
| `F7A` / `F30A` | Same | `sa_notional_basis` AMORT (amortization-weighted) |
| `B84` | The system booking: 84 two-month trades, trade k live in month k only (`active_from`/`active_to`) | Two-month notional, M to Dec 2033 |
| `N84` | The same 84 trades summed | Double counts (about ×2); a warning flags it |

Netting set ids are `<scenario>_<FE|BE>_<CSRA|NOCSRA|IM|NOIM|IMVM>`. The CSRA and the SPV IM are static and entered as `ia_received` on an uncollateralised netting set, so "with / without" is two netting sets. `IMVM` (`--vm-posting`) adds a CSA where bank posts VM to the SPV on the window value (`threshold_cp` huge, `threshold_bank` 0). `E_VM_posted` on EE_NEE is then the FVA input.

### Before a real run: prepay inputs to update
Everything below except the curve to Jan34 is still a placeholder in the example. Rows are in rough order of impact on the results.

| Input | Placeholder | Sheet / column | Source and how to get it |
|---|---|---|---|
| Forward curve after the listed NG months | Last 12 CSV months repeated flat | Contracts `price` (via `--curve`) | Listed NYMEX NG futures (`NGF35 Comdty` …, field `PX_SETTLE`), about 12 years out. Past the last listed month there is no exchange price: take the desk's long-dated curve to Dec 2057 from the same system as the NG gridpoint export |
| Implied vols | Generated from the 2-factor placeholders (45% short, κ = 1, 15% long) | Contracts `implied_vol` | `<ticker> OMON <GO>`: ATM mid vol (average call and put at the strike nearest the futures), as a decimal. Liquid for roughly 3–5 years; extrapolate further out, or use the desk's NG vol surface, and record which |
| Factor parameters | κ (`beta`) 1.0, SHORT `vol_ratio` 3.0, SHORT/LONG ρ 0 | CommodityFactors, FactorCorrelation | Calibrated NG parameters from the model owners first. Otherwise fit them from `BDH` history as in the commodity section "Factor parameters and correlations" |
| Discount curve | Flat 4% | Curve `zero_rate` (continuous) | USD SOFR OIS from `ICVS <GO>` / `SWDF <GO>` (confirm the curve id), zero = −ln(DF)/t, tenors 1M–40Y. Mostly moves full-strip MTMs; the two-month window hardly changes |
| CSRA and SPV IM | $8mm / $6.24mm (pro rata guesses at 20k/d) | NettingSets `ia_received` | Term sheet / legal documents, not Bloomberg. They decide whether the window EE is zero |
| Fixing dates | First of the delivery month | Contracts `expiry` | NG last trade date (3 business days before the month), Bloomberg `LAST_TRADEABLE_DT`. Small effect |
| Valuation date | 2026-11-13 | Config `valuation_date` | The as-of date of the curve and vol snapshot |
| Index | NYMEX Henry Hub | Contracts | Confirm in the deal confirmation. A regional index needs Henry Hub plus basis for the curve and a basis vol |

Confirm against the deal rather than replace:
- volume (20,000 MMBtu/day);
- fixed prices ($5.00 front end; $5.035 to Dec33 then $5.00 back end);
- tenor (Jan27–Dec57) and reset (Dec 2033);
- MPOR (10 days) and SA-CCR alpha (1.4).

SA-CCR supervisory factors are regulatory constants, not placeholders.

**Bloomberg pull workbook.** `.venv\Scripts\python examples\prepay\build_bbg_pull.py [--curve examples\prepay\ng_curve.csv] [--out f.xlsx] [--single-digit-until 2035]` writes `examples\prepay\prepay_bbg_pull.xlsx`. Open it in Excel with the Bloomberg add-in loaded. Blue cells hold Bloomberg formulas; yellow cells are for you to fill.

| Sheet | Contents |
|---|---|
| Setup | As-of date, history start (3 years back), price field (`PX_SETTLE` or `PX_LAST`), minimum open interest, notes |
| Contracts | One row per strip month, Dec26–Jan58: ticker; `BDP` price, `LAST_TRADEABLE_DT` and `OPEN_INT`; `price_override`; `fallback_price` (the `--curve` CSV with its seasonal extension); `price_used`; `source`; `implied_vol_paste`; `screenshot_check` |
| Export | `month, price, implied_vol` for the run's `--curve` CSV |
| History | Daily `PX_LAST` via `BDH` for generics `NG1`–`NG36 Comdty`, for the factor fit |
| Curve | `BDS("YCSW0490 Index","CURVE_TENOR_RATES")` for SOFR OIS |

How `price_used` is chosen: the override first. Next the Bloomberg price, but only if the contract is listed and its open interest is at least the Setup minimum. Otherwise the fallback curve. `source` records which one was used, and counts are shown under the table. A `MISSING` row must be fixed before export. `screenshot_check` shows Bloomberg minus the CSV price for the months the CSV quotes.

Workflow:
1. Run the builder, open the workbook on a terminal PC and let it refresh.
2. Check the following:
   - **Tickers.** Contracts up to `--single-digit-until` use a 1-digit year (`NGF4` = Jan 2034), later ones 2 digits (`NGF36`). If far contracts return `#N/A`, test both forms on the terminal and rebuild with a different `--single-digit-until`.
   - **Curve id.** `YCSW0490` follows the "curve 490" note above; confirm it in `ICVS`.
   - **Vols.** No Bloomberg field is assumed for NG option vols; paste them from `OMON`.
3. Save the Export sheet as CSV.
4. Run `run_prepay_scenarios.py --curve <csv> --name <new name> --rebuild`.
5. Enter the vols, factor parameters, discount curve, CSRA/IM and fixing dates in `<new name>.xlsx`, then run again **without** `--rebuild`.

**Limitation:** the `--curve` CSV is read for `month` and `price` only; its `implied_vol` column is ignored for now. Vols, rates and factor parameters therefore go into the generated workbook by hand (step 5).

### Inputs
Trades (COMSWAP):

| Column | Meaning |
|---|---|
| `active_from`, `active_to` | Exposure window of the leg, [from, to); blank = always. Outside it the leg counts zero in exposure and SA-CCR (LegMTM still shows the booked value) |
| `window_months`, `window_convention` | Trade level: derive every leg's window from the strip's own fixing dates f_i. `FWD` (months {k, k+1} at a date in month k): leg i is active on [f_{i−W+1}, f_{i+1}). `BWD` (months {k−1, k}): [f_i, f_{i+W}). Both stop at the leg's payment, and the first legs start at the valuation date. Explicit active dates win. **BWD caveat:** a leg counts zero from its payment date, so when month i is paid at f_{i+1} (as in the example) BWD keeps only the month in progress, and a warning says so. A missed settlement that stays owed past its due date is not modelled; use FWD |
| `sa_maturity` | Date, tenor or `TRADE` (last payment): M = sa_maturity − t for every leg (CAR ch.7 ¶124(a)), floored at the leg's own payment. Blank = each leg's own payment (the default) |
| `sa_fixed_notional` | Y/N; blank = Y on windowed trades. Y keeps a fixed but unpaid month's notional (at the fixing price) while it is in the window, so the window always carries its full notional |
| `sa_notional_basis` | `MARKET` (default) or `AMORT`: d_i(t) = q_i·F_i(t)·(fixing_i − t)/(M − t), the time-average over [t, M] of the notional still outstanding, path by path. Ignored with a warning on a windowed trade |

NettingSets: `value_source` MODEL (default) or IMPORT, and `import_sa_price` IMPLIED (default) or FORWARD (below).

- **Window = SELECT.** A netting-set sum of windowed legs is the window exposure, so overlapping windows are never double counted. Two trades in one netting set with overlapping live legs on the same contract and direction raise a warning.
- **Summary `MTM0`** is the value counted in exposure at t = 0, so it excludes legs outside their window.
- **SACCR_Proxy** adds per trade:
  - `SA_NOTIONAL_TOTAL`: today's notional carrying SA-CCR.
  - `SA_NOTIONAL_AMORT` = (1/M)·Σ q_i·F_i·|SA-live interval_i ∩ [0, M]|, which is about half of TOTAL for a level strip and the two-month notional for a window.
  - `M_trade`, `sa_notional_basis` and `windowed`.
  - With `sa_maturity` every leg shares one MF, so `SA_NOTIONAL` is no longer NaN for strips.
- **NEE nets IA received.** NEE = E[min(V − CSRA, 0)], so use `NEE_uncoll` for DVA.

### Path-by-path import (bank system PVs)
A netting set with `value_source` IMPORT takes each trade's PVs per scenario and date from files listed on a `PathImport` sheet. The columns are `trade_id | date | file | sheet | column | scenario_column | scale | sign`, with one row per trade and date.
- **Files:** CSV, Excel or Parquet, relative to the input workbook. Rows are scenarios.
- **scenario_column** (optional) aligns rows across files. Without it, row order is used and the counts must match.
- **scale and sign** turn the export into a CSA-currency amount, bank view.
- **Helper:** `eqxva.pathimport.build_path_import_sheet(trade_ids, dates, file="pv_{date:%Y%m%d}.csv", column="PV_{trade_id}", export_ids=...)` writes the rows from name patterns. One export column can feed several trade_ids, e.g. the with-CSRA and without-CSRA netting sets.
- **The booked trades are still needed.** They give the windows, maturity and SA-CCR notional terms. Imported PVs are zeroed where the booked trade is not live.
- **Downstream is unchanged:** collateral, statistics, buckets, FCA/FBA and SA-CCR all run as for MODEL sets. The grid is the export dates plus t = 0; t = 0 uses the booked MTM if the export has no valuation-date column. `n_paths` is the export's scenario count.
- **SA-CCR notional (only PVs are exported):**
  - `IMPLIED` (default): each live trade's PV is turned into the uniform price move r = (PV + Σ ω·q·K·DF) / Σ ω·q·DF·F0 on that path, and its contracts get r·F0. This is exact for a one-price trade.
  - `FORWARD`: today's forward on every scenario. It ignores the co-movement of notional and MTM, and on the illustrative inputs it understates path-wise EAD under the CSRA by up to about 40%. IMPLIED came within 4% of the model (tests).
  - RC and the multiplier are path-wise from the imported PVs in both cases.
- **Approximations:** a bucket that is not an export date uses the last export date before it for SA-CCR, and EE/PFE are interpolated. The MPOR lag of a collateralised IMPORT set is only as fine as the export dates. Warnings flag both.
- **Not analysed:** AnalyticCheck skips IMPORT sets, and also commodity sets with more than 40 live legs (full strips), which get Monte Carlo results only.

### bank risk-profile export (SystemRequests sheet)
The bank export comes as one set of CSVs per request (one request per strip). This is read directly, with no PathImport rows. The sheet is `SystemRequests: trade_id | request_id | folder | undiscount`: one row per booked strip trade, with `folder` relative to the input workbook and `undiscount` Y by default.

File names are matched ignoring case, spaces, `.` and `_`. Per request id:

| File | Content | Use |
|---|---|---|
| `RiskProfile_<id>.Test DiscPosNet.csv` | Wide: first row = dates, one row per scenario; discounted positive net exposure with the new trade | Required |
| `RiskProfile_<id>.Test DiscNegNet.csv` | Same, negative part | Required |
| `RiskProfile_<id>.Base DiscPosNet.csv` / `...Base DiscNegNet.csv` | Before the new trade; empty or missing = 0 | Optional |
| `RiskProfile_<id>_SACCR_PathEADs_TT_Test.csv` | Long: `NettingSet, Valuation_Date, PathId, RC, Multiple, PFE, EAD` | Optional |

- **Trade PV per scenario and date** = (Pos + Neg)_Test − (Pos + Neg)_Base. With `undiscount` Y it is divided by DF(first date, date) on the engine's curve, so the CSRA / IM nets against the undiscounted value.
- **Request consistency:** every request needs the same dates and scenarios, and row i must be the same scenario in every file.
- **Valuation date:** set Config `valuation_date` to the export's first date.
- **Buckets:** best set to the export dates, otherwise EE/PFE are interpolated.
- **System SA-CCR:** EE_NEE gains `SACCR_EAD_system`, `SACCR_RC_system`, `SACCR_PFE_system` and `system_live_trades`.
  - These are the path means of the strip live at the bucket (strip k in month k), at the last export date on or before it.
  - NaN means the SA-CCR file has no row for that period.
- **Other outputs:** Summary gains `SACCR_EAD0_system`, and the `SystemSACCR` sheet lists every strip's path-mean RC, multiplier, PFE and EAD by date.
- **Engine SA-CCR (`SACCR_EAD_*`):** these columns are still computed on the booked terms, so the system's EAD (as booked) and the engine's window treatment (two-month notional, M to the reset) sit side by side.
- **Setup helper:** `prepay_examples.add_system_import(sheets, {"FE": [84 ids], "BE": [84 ids]}, folder="exports")` adds IMPORT netting sets `S84_<side>_<CSRA|NOCSRA|IM|NOIM>` with the 84 strips booked as windows, plus the SystemRequests sheet.
- **`SystemRequests.folder`** can be relative to the input workbook, absolute, or a UNC path.
- **t = 0 values:** Summary `MTM0` and `SACCR_EAD0` always use the engine's deterministic t = 0 value of the booked strips, even when the export has a t0 column. Buckets from the first export date on use the imported paths.

### Running the system export (`run_system_export.py`)
The archive holds one folder per request id: `\\cmcscpdsfil01\LITES_ARCHIVE\Intraday\Tertiary\output\reports\intraday\<request id>\`. Ids need not be consecutive. Each folder holds the RiskProfile files listed above, with 2,000 paths.

```
.venv\Scripts\python examples\prepay\run_system_export.py --requests req.csv [--side FE|BE] [--root <folder>]
    [--curve examples\prepay\ng_curve.csv] [--name prepay_system] [--with-model] [--paths N]
    [--check-only] [--no-snapshot] [--rebuild] [--no-analytic]
```

**Request list (`req.csv`, or `.xlsx`).**
- `request_id` is required.
- `side` (FE / BE) is optional; without it, `--side` applies to every row.
- `strip` (0–83) is optional; without it, the row order is the strip order. Strip k covers delivery months k and k+1, with Jan27 = 0.
- Each side needs 84 distinct ids.

**Steps.**
1. **Preflight** writes `examples\prepay\<name>_preflight.csv` with one row per strip. It checks:
   - which files were found;
   - the scenario and date counts, and that they are the same across requests;
   - `base_max_abs`, which should be 0;
   - the system t0 PV against the engine's booked MTM (`pv0_diff`), and its spread across paths (`sys_pv0_std` ≈ 0);
   - `last_nonzero_date`, which must rise with the strip number (strip order);
   - the SA-CCR path and date counts.

   Missing files, or dates and scenario counts that differ between requests, stop the run. Smaller issues go in the `problem` column. `--check-only` stops after this step.
2. **Snapshot:** the files are copied to `examples\prepay\system_export\<name>\<id>\` so the run can be repeated. `--no-snapshot` reads the share directly.
3. **Workbook:** `examples\prepay\<name>.xlsx` is written only if it is missing, or with `--rebuild`.
   - The valuation date is the export's first date, the buckets are the export dates, and the mode is POINT.
   - It holds the IMPORT sets `S84_<side>_<CSRA|NOCSRA|IM|NOIM>`.
   - `--with-model` adds the engine's simulated `B84_<side>_*` sets for comparison.
4. **Run:** the results go to `examples\prepay\<name>_results.xlsx`, with two extra sheets:
   - `PrepaySystem`: MTM0, peak EE, EPE, EEPE, peak PFE, and the engine and system EAD at t0 and at their peaks;
   - `Preflight`: the preflight table.

**Before relying on the numbers.**
- `--curve` must match the system's curve; otherwise `pv0_diff` and the implied SA-CCR prices are off.
- The Curve sheet still holds the flat 4% placeholder, and it is used for undiscounting.
- Imported peak EE can sit slightly below the model's (`--with-model`), because the import sees only the export dates and misses the pre-settlement peaks.

**How the 84 strips are combined.**
- **Per-strip values.** Each request gives one matrix per strip: PV_k(path, date) = (Pos + Neg)_Test − (Pos + Neg)_Base, divided by DF.
- **No double count.** Strip k is booked with both its months but is live only in month k: `active_from`/`active_to` on both legs, with strip 0 starting at the valuation date. Outside that window its PV is zeroed (`exposure._imported_value`). The one-month overlap between neighbouring strips is therefore never counted twice.
- **Netting-set value.** The value is V(path, date) = Σ_k PV_k · 1[strip k live]: a single continuous two-month window value over all paths. Swap k's PV includes month k+1. It is still open whether the system's PV of strip k in month k includes month k after it has fixed; if it does not, the window carries one month only.
- **XVA exposure** comes from V net of the static IA: EE, NEE, PFE, EPE/EEPE, `_disc`. `EE_uncoll`, `NEE_uncoll` and FCA/FBA come from V itself.
- **Engine SA-CCR** runs path by path on all paths:
  - The add-on uses each path's prices implied from the live strip's PV: the two-month notional, with M to the Dec 2033 reset.
  - RC and the multiplier come from V − IA.
  - EAD = 1.4 × (RC + multiplier × AddOn), reported as `SACCR_EAD_mean`.
- **System SA-CCR** (`SACCR_EAD_system`) is selected, not combined path by path. At each bucket it is the path mean of the strip live then, at the last SA-CCR date on or before the bucket.
  - `system_live_trades` should be 1 at every bucket.
  - These are the system's as-booked, uncollateralised numbers: each request is a stand-alone two-month swap with M at its own end.

### Changing the fixed price or the CSRA / IM
Edit the generated workbook, then rerun **without** `--rebuild`, which would regenerate it and discard the edits. Work on a copy under a new `--name` so the base case stays.

- **Fixed price, engine scenarios** (`run_prepay_scenarios.py --name <copy>`): Trades `fixed_price`, one row per monthly leg.
  - Front end: rows of `*_FE_*` netting sets (direction BUY).
  - Back end: rows of `*_BE_*` sets (direction SELL). The back end is $5.035 before Jan 2034 and $5.00 after; filter on `fixing_date` to change only one period.
  - Script: `.venv\Scripts\python examples\prepay\set_fixed_price.py <workbook.xlsx> --side FE|BE --price 5.50 [--from DATE] [--until DATE] [--sets W7,F7] [--out new.xlsx]`. It skips IMPORT sets and prints how many legs it changed and what they were before.
- **Fixed price, system import (`S84_*`): do not edit `fixed_price`.**
  - The imported PVs already contain the price booked in the system. Editing the trades leaves exposure unchanged and makes the IMPLIED SA-CCR prices inconsistent, because r is solved with the new K against PVs at the old K.
  - Rebook the strips in the system at the new price and run with the new request ids instead.
  - A swap is linear in K, so PV_new = PV_old − ω·q·(K_new − K_old)·DF(t, pay) would be exact on each path, but this adjustment is not built.
- **Daily volume (MMBtu/d):** each leg's Trades `quantity` = days in its delivery month × volume.
  - New workbook: `run_prepay_scenarios.py --volume 25000 --rebuild` (plus your usual `--curve`, `--name`, `--vm-posting`). The IM and CSRA scale pro rata (6.24mm and 8mm at 20,000/d) unless `--im` / `--csra` give them in USD.
  - Workbook you have already edited: `.venv\Scripts\python examples\prepay\set_volume.py <workbook.xlsx> --volume 25000 [--side FE|BE] [--sets W7,W30] [--im USD] [--csra USD] [--no-collateral-scaling] [--out new.xlsx]`, then rerun without `--rebuild`. It rescales `quantity` and the IM/CSRA `ia_received` and skips IMPORT sets.
  - System import (`S84_*`): the volume is in the system PVs, so rebook the strips at the new volume.
  - `xva_writeup.py --volume 25000` so the spread PV and lost spread use the same volume.
- **CSRA / IM:** NettingSets `ia_received` (USD, static) on `*_FE_CSRA` (front-end CSRA) and on `*_BE_IM` / `*_BE_IMVM` (back-end SPV IM). Keep `*_NOCSRA` / `*_NOIM` at 0.
  - Works the same for MODEL and IMPORT sets.
  - Changes: EE, NEE, PFE, EPE/EEPE and the engine's SA-CCR (RC and the multiplier).
  - Unchanged: the `_uncoll` columns, FCA/FBA and `SACCR_EAD_system`, since the system ran without collateral.

### Validation (`tests/test_prepay.py`, illustrative inputs)
- A windowed strip equals the explicit 84-trade booking path by path. Window EE matches exact quadrature, and, net of the CSRA, matches q × Black at strike K + C/q.
- Summed 84 / strip ≈ 2. The SPV window EE is 0 when IM ≥ two months at $0.
- SA-CCR reproduces `reference/prepay/prepay_note_check.py`:
  - two-month proxy EAD 0.108 / 0.152 (Nov–Dec 2033 or 2057);
  - literal 2057 strip 279.8 / 282.1;
  - 7-year strip 59.1 / 61.3.
- The import round trip reproduces the model's EE/NEE/PFE exactly.

### Prepay simplifications
- Futures prices are computed on demand from the simulated factors (memory scales with factors × grid × paths, not contracts), with identical numbers.
- The monthly index fixes on the contract expiry (FOM proxy); daily-index averaging is not modelled.
- p-weighting after the 2033 reset, the SPV hazard step-up, lost spread and WWR are downstream. Use W7/W30 (p = 0 / 1) as the bounds.
