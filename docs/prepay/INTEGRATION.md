# Integrating the gas prepay swaps into the exposure / SA-CCR calculator

Background, conclusions and numbers are in `context.md` (read it first). Reference implementations are in `reference/prepay/` — they are prototypes for checking results, not code to copy in. Build on the calculator's existing two-factor commodity model, trade objects, netting/collateral logic and SA-CCR engine.

## What is new compared with an ordinary commodity swap

1. **Linked pair across two counterparties.** A front-end swap with the Issuer (BMO receives index, pays fixed) and a back-end swap with the SPV (BMO pays index, receives fixed), same volumes, booked to the final delivery date (Dec 2057).
2. **Cross-extinguishment.** Default by either counterparty terminates BOTH swaps with no MTM payment; only unpaid settlements survive.
3. **Window exposure.** Loss at default = unpaid settlements in a short window (base: 2 months; stress: 3 months for the roll gap; PEAK-style 120-day cure ≈ 5 months), NOT the MTM of the remaining strip.
4. **Static collateral.** CSRA (Issuer side) and SPV IM are fixed amounts, not margin with an MPoR. BMO may post VM to the SPV on the window amount.
5. **Continuation after the reset.** Exposure after the reset (Dec 2033) is weighted by p = probability of successful remarketing (0 / 0.5 / 1). The SPV hazard steps up after the reset.
6. **Lost spread.** The spread stops at either party's default, with no claim: CVA term Σ spread_i·DF_i·(1 − S_I·S_S).

## Proposed design (adapt names to the codebase)

### Trade / product
- New product `PrepaySwapPair` holding: front leg, back leg (each a commodity swap leg on the existing index), counterparties, volume schedule, strikes by period (back-end 5.035 to Dec33, 5.00 after; front 5.00), reset date, final delivery date, `window_months` (2), CSRA, IM, `p_continue`, `vm_posting` flag.
- Keep the booked legs available for valuation/P&L (full strip), but route exposure through the window logic below.

### Exposure (XVA / PFE)
- Reuse the two-factor commodity simulation and the existing forward-curve / fixing machinery. Fixings must be path-simulated.
- New aggregation `window_exposure(t)`: for t in month k, value only months in W(k) = {k, k+1} (forward convention) or {k−1, k} (backward); subtract static collateral; E = max(V − C, 0). Never sum overlapping trades.
- If the engine can only aggregate per netting set: put each window in its own dummy netting set and stitch EE/PFE by month (exact for EE and PFE).
- Outputs: EE, PFE97.5, ENE per counterparty; E[VM posted]; post-reset weighting by p.
- XVA: CVA per counterparty on window EE; lost-spread term; FCA on VM posted; KVA from the capital profile below; WWR stress (price spike × Issuer default).

### SA-CCR (regulatory capital)
Implement as a configurable treatment, reported side by side:
- `literal` (Pillar 1 default): booked legs, full notional, M to final delivery, unmargined MF, CSRA/IM as collateral, multiplier, α = 1.4.
- `two_period` (proposed, needs Reg Cap / OSFI sign-off): adjusted notional = price × window volume, M to final delivery (M only affects BA-CVA), same collateral.
- Report EAD, CCR RWA (IRB), BA-CVA RWA (reduced, ρ 0.5, DS 0.65 across counterparties, M uncapped), leverage exposure.
- Guard against the overlapping-window booking (84 two-month trades): detect and warn, or deduplicate to one strip.

### Validation (port as tests)
- Window EE ≈ q × Black call with strike K + C/q at each date (Dec33: 0.10 vs 0.097 sim; within MC error).
- Sum-of-84 / strip EE ≈ 2; window EE ≈ 1.24 peak pre-CSRA (flat $5, 45% short vol κ=1, 15% long vol, 20k/d).
- SA-CCR proxy Nov–Dec 2033: Issuer EAD 0.108, SPV 0.152, total RWA 0.45; Nov–Dec 2057: 0.90.
- Literal booking to 2057 at 20k/d: EAD 280/282, total RWA ≈ 1,739, leverage ≈ 573.
- SPV window EE ≈ 0 when IM ≥ 2 × q × K_back.

## Status in the engine (Nov 2026)
Done, exposure and EAD only. See README "Gas prepay swaps" and `tests/test_prepay.py`.
- **Window:**
  - Booked as leg-level active windows (`active_from`/`active_to`, or `window_months` FWD/BWD on a strip) on ordinary COMSWAP legs. No new product class.
  - BWD (missed month + month in progress) needs the missed settlement to stay owed past its payment date, which is not modelled yet. With payment at the next fixing it collapses to one month, and a warning says so.
  - The netting-set sum of windowed legs is the SELECT.
  - Overlapping bookings are flagged.
- **Scenarios:** W7, W30, F7, F30 (MARKET and AMORT), B84 and N84, each × FE/BE × with/without CSRA or IM (`eqxva.commodity.prepay_examples`, `examples/prepay/run_prepay_scenarios.py`).
- **SA-CCR:**
  - `sa_maturity` (M to the reset, to final delivery, or TRADE).
  - Two-month notional kept after fixing (`sa_fixed_notional`).
  - Amortization-weighted notional (`sa_notional_basis` AMORT, plus `SA_NOTIONAL_AMORT`).
  - The literal and two-period targets above are reproduced.
- **System import:**
  - `value_source` IMPORT plus a `PathImport` sheet.
  - The SA-CCR notional is implied from the PVs.
  - Still needed: a sample export, to fix the file and column pattern in `build_path_import_sheet`.
- **Not in the engine (by scope):** CVA, lost spread, FCA charge, KVA, IRB/BA-CVA RWA, leverage, p-weighting, WWR. Use the EE_NEE / SACCR profiles and `reference/prepay/`.

## Suggested Claude Code workflow
1. Start in plan mode: "Read docs/prepay/context.md and INTEGRATION.md, then map the design onto this codebase: which existing classes cover the commodity two-factor model, trades, netting sets, collateral and SA-CCR. Propose the minimal changes."
2. Implement the product and window aggregation first, with the validation tests above, against the illustrative inputs.
3. Add the SA-CCR treatment switch and the side-by-side report.
4. Swap illustrative inputs (PD, spreads, vols, CSRA/IM, volumes) for BMO data; keep that data out of the repo if policy requires.
5. Keep `context.md` current as legal/commercial items close (cure period, reset exit, CSRA on acceleration).
