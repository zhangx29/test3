# Gas prepay commodity swaps — CCR, capital and XVA workstream

Context for Claude. I'm Xiaoli Zhang (CCR / XVA). I'm computing XVA on BMO's commodity swaps in a municipal natural gas prepay; Orest computes capital from it. Target: results for the Risk (Bimal) meeting, week of 23 Nov 2026.

Full write-up: Claude Doc "Gas Prepay Swaps — Exposure & Capital Review" https://claude.ai/artifact/193WMSY8GGfJioPtbSUWa4
Running notes: Claude project "CCR and XVA", doc `claude/gas-prepay-swaps-review.md`.

## The deal
- Municipal Issuer sells tax-exempt bonds, prepays a Gas Supplier SPV for ~30 years of gas; BMO sits between them with two commodity swaps.
- Front-end (vs Issuer): BMO pays fixed, receives index. Back-end (vs SPV): BMO receives fixed, pays index. Separate counterparties → separate unmargined netting sets.
- Rolling form: 2-month initial term, extends one month on each successful settlement (market standard, e.g. CCCFA 2025).
- Extinguishing feature: any EOD terminates BOTH swaps, no termination (MTM) payment; only unpaid settlements survive — at most two months (missed month + month in progress).
- Collateral: CSRA (Issuer side, static reserve) and SPV IM (static, two months at $0 price). BMO may post VM to the SPV on the two-month amount.
- Bonds have a mandatory tender / reset at year 7 (Dec 2033); the funding agreement ends there. Swaps end at the tender only on failed remarketing or refunding; on successful remarketing they continue (possibly amended).

### Terms
- Deck: 30,000 MMBtu/d; front $4.96, back $5.00; CSRA $12mm; SPV IM $9.3mm.
- 13 Nov proposal (current): 20,000 MMBtu/d; front Jan27–Dec57 @ $5.00; back Jan27–Dec33 @ $5.035, Jan34–Dec57 @ $5.00 → 3.5c spread to the reset only ($0.26mm/yr, $1.55mm PV). Capital/PFE proxy: 2-month swap Nov–Dec 2033 @ $5.00 each side. CSRA/IM at 20k/d not stated — I assume CSRA $8mm (pro rata) and IM $6.24mm.
- My system booking: 84 swaps per side, trade k covers months k and k+1 (Jan27/Feb27 … Dec33/Jan34).

## Established conclusions
- SA-CCR (OSFI CAR 2027 ch.7): maturity runs to 2057 — ¶124(a) "latest date the contract may still be active"; the tender is contingent and outside BMO's control; options don't shorten maturity (EBA Q&A 2018_4329); reset exception ¶130(e) doesn't apply (fair value not reset to zero). Only a scheduled termination of both swaps at the tender, with continuation as a new trade, gives 7 years.
- The notional reading decides viability, not tenor. Literal (full notional to 2057): total RWA ~$1,739mm; 7y hard stop ~$191mm (still uneconomic, 0.13% spread/RWA). Two-month notional (the proxy): $0.45mm (2033) vs $0.90mm (2057) — unmargined MF caps at 1y and IRB M at 5y, so only BA-CVA moves.
- The two-month proxy for regulatory capital is a policy decision needing Reg Cap + OSFI sign-off; present literal and proxy numbers side by side.
- Summing the 84 overlapping trades double counts (SA-CCR and any netting-set XVA run).
- Two-month exposure is defensible for XVA/limits if legal confirms: renewal only on payment received (CCCFA wording renews "upon the payment due date" → roll-gap risk of a third month); automatic/prompt termination; extinguishment both ways (critical: if the front-end survives an SPV default, loss = full MTM); enforceable in Chapter 9 / SPV insolvency; no other surviving claims. Walkaway-clause concern for capital netting.
- Market: rating agencies stop at the tender (funding agreement ends); commodity swaps run to the end of delivery; recent swap providers are BP (non-bank). JPM's view ("remarketed with changes to the swap") supports continuation, not termination.

## XVA method (the agreed approach)
- Exposure at t in month k = trade k only (SELECT, never SUM): E_I(t) = max(Σ_{i∈{k,k+1}} q_i (X_i − K_F) DF − CSRA, 0); SPV analogous with (K_B − X) and IM.
- Build via trade-level cube + mask, or 84 one-trade dummy netting sets stitched by month. Fixings simulated on path.
- Horizon to Dec 2057; weight post-2033 windows by p = probability of successful remarketing (run p = 0, 0.5, 1). Step the SPV hazard up after 2033.
- Terms: CVA on the window; lost-spread term (spread stops at either default, no claim) — largest credit term; FVA on VM posted to SPV; KVA on capital profile built the same way; WWR stress price spike × Issuer default.
- Illustrative results (flat $5; 45% short vol κ=1 + 15% long vol; Issuer 50bp / SPV 100bp, LGD 60%): peak EE pre-CSRA window 1.24 / strip 15.5 / sum-of-84 30.8; Issuer CVA net CSRA 0.001 (to 2057: 0.018); lost spread 0.13; FCA 0.03; SPV CVA 0.
- Validation: window EE ≈ q × Black call, strike K + CSRA/q (Dec33: 0.10 vs 0.097 sim); sum-of-84 / strip ≈ 2.

## Files
- scripts/prepay_window_xva.py — window exposure profile and XVA (main XVA script); writes prepay_window_profiles.csv.
- scripts/prepay_note_check.py — capital for literal booking vs proxies at 20k/d.
- scripts/prepay_ead_rwa.py — SA-CCR/IRB/BA-CVA for 2-month / 7y / 30y and the 84-trade booking (deck terms, 30k/d); writes prepay_ead_rwa.csv.
- scripts/slice_pfe.py — two-month PFE above CSRA at years 1/7/15/31.
- scripts/prepay_capital.py — original deck analysis and OU PFE simulation (30k/d).
- outputs/ — CSVs from the runs.

## Conventions
- USD mm; flat $5.00 curve; 4% continuous discounting; valuation date 13 Nov 2026; first delivery Jan 2027.
- PD/LGD/spreads/vols are illustrative — label them as such and replace with BMO's ratings, CDS proxies and the engine's calibration.
- BA-CVA reduced: SCVA = RW·M·EAD·DF/α; K = 0.65·√((0.5ΣS)² + 0.75ΣS²). Don't apply DS per counterparty.

## Precedents read from official statements (15 Nov)
- PEAK 2025B ($643.0mm; CUSIP 74440DFQ2, 74440DFP4): BP Energy both swaps (BPNA guarantee, capped). 2-month initial term, extends a month "upon each monthly net settlement amount payment date" through delivery (Apr 2026–Aug 2056). Tear-up: no termination payment beyond accrued amounts. Either swap's termination notice ends the other; a Gas Delivery Termination Date ends both. Issuer non-payment cure up to 120 days (3 business days if PEAK could have sold receivables) — breaks a strict 2-month cap. Initial reset period to Oct 31, 2033; bonds extendable maturity Dec 1, 2033.
- Minnesota 2026A ($803.4mm; CUSIP 60406XAJ0 + serials): BP both swaps, same roll and tear-up. Issuer cure 3 business days; must suspend gas to a non-paying participant by next business day; supplier cure 1 business day. Margin "for approximately rolling two months' exposure"; $11mm Commodity Swap Reserve. "Under certain circumstances" issuer, supplier and BP can each terminate their swap "as of the last day of the then-current Reset Period" (circumstances not in the OS). Initial reset period Jul 2026–Jul 2035; bonds extendable maturity Sep 1, 2035.
- Black Belt 2024A: BP; "rolling two-month terms that renew automatically" through delivery to 2055; tear-up; margin for rolling two months' exposure.
- Reset Period = gas delivery block matching one bond interest rate period (defined in the Repricing Agreement); ends one month before the rate period. Later periods set at each remarketing.
- Implications: market form runs to final delivery (SA-CCR M ≈ 30y); renewal on due date = roll-gap risk; ask for Minnesota terms (reset-date exit, 3-day cure + prompt gas suspension, 2-month margin).

## Open items
- Legal: the five two-month conditions; CSRA on bond acceleration; CSA exposure definition; which two months survive and grace period.
- Docs: mandatory tender vs optional put; final delivery date (Jan27–Dec57 is 31 years); index (monthly FOM vs daily; can it go negative).
- Commercial: CSRA/IM at 20k/d; reset rights (scheduled termination, spread reset, assignment).
- Reg Cap: OSFI view on two-month notional; walkaway point; leverage exposure.
