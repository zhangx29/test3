# Refreshing the "Gas Prepay Swaps — XVA Pricing" doc with real run results

The write-up is a Claude Doc: **https://claude.ai/artifact/YD666qn1ayMT7oQ9DsrwPE**
(doc id `fcbb5bf8-34cb-407c-ae07-bb695b0ab269`). It is stored on claude.ai, not in this folder. Any computer
signed in to the same claude.ai account with the Claude Docs connector can open and edit it.

The illustrative version (18 Nov 2026) was built from:
- `examples/prepay/prepay_inputs_ngcurve_results.xlsx`: all scenarios, NG curve, no VM sets;
- `examples/prepay/prepay_inputs_ngcurve_vm_results.xlsx`: W7/W30 with `--vm-posting`;
- `examples/prepay/xva_writeup.py` on both → `prepay_ngcurve_writeup.md`, `_writeup.xlsx`, `_pfe_annual.csv`.

## Deal setup the doc assumes (decided 18 Nov 2026)

| Side | Netting set used | Treatment |
| --- | --- | --- |
| Front-end vs Issuer | `*_FE_NOCSRA` | Unmargined. The CSRA is NOT relied on as collateral |
| Back-end vs SPV, exposure / CVA / FCA | `*_BE_IMVM` | One-way CSA: BMO posts VM at zero threshold, SPV posts static IM, 10-day MPoR, posted VM not segregated |
| Back-end vs SPV, SA-CCR | `*_BE_IM` | A CSA where only BMO posts counts as unmargined; IM is collateral |

A Word copy of the doc as it stood on 18 Nov 2026 (rev 33, chart included) is in
`docs/prepay/Gas_Prepay_XVA_Pricing_2026-11-18.docx`.

## Without a Claude account (Word report)

`xva_writeup.py` also writes **`<name>_report.docx`**: the full write-up (executive summary, trade, method,
Issuer, SPV, chart, capital, observations, caveats) with every number filled from the results. It needs no
Claude access. The text is a template that follows the Claude Doc. Sentences that depend on the results are
computed or switched on them (for example whether the IM covers the SPV window), but read the report before
sending it on. Deal terms (volume, strikes, spread to the reset, IM, curve range, valuation date) are read from
the `in_*` sheets of the results, so they match the run. Pass `--status "..."` to replace the "Illustrative ..."
sentence once the inputs are real, and `--no-docx` to skip the report.

The report needs `python-docx` and `matplotlib`: `.venv\Scripts\python -m pip install -e .[report]`.
If that PC cannot reach PyPI, download the wheels on a PC that can (same Python version, 3.12, Windows):
`python -m pip download python-docx matplotlib -d wheels`, copy the `wheels` folder over, then
`.venv\Scripts\python -m pip install --no-index --find-links wheels python-docx matplotlib`.
Without them the script still writes the Markdown, Excel and CSV outputs and says the report was skipped.

## Steps on the other computer

1. **Code.** Copy the project folder (or at least `src/`, `examples/prepay/`, `pyproject.toml` and this file) and install:
   `python -m venv .venv` then `.venv\Scripts\python -m pip install -e .[excel,dev,report]`.

2. **Engine scenarios with real inputs.** Run ONE workbook that includes the VM sets, so every netting set
   comes from the same simulation:
   ```
   .venv\Scripts\python examples\prepay\run_prepay_scenarios.py --curve <real_curve.csv> --name prepay_real --vm-posting --rebuild
   ```
   Then enter the real vols, factor parameters, discount curve, IM size and fixing dates in
   `examples\prepay\prepay_real.xlsx` (README "Before a real run") and run again **without** `--rebuild`:
   ```
   .venv\Scripts\python examples\prepay\run_prepay_scenarios.py --name prepay_real
   ```
   A different daily volume: add `--volume <MMBtu/d>` to the `--rebuild` run (IM and CSRA scale pro rata
   unless `--im` / `--csra` are given), or use `examples\prepay\set_volume.py <workbook> --volume <MMBtu/d>`
   on a workbook already edited by hand. Pass the same `--volume` to `xva_writeup.py` in step 4.
   The full run takes about 17 minutes at 10,000 paths. `--scenarios W7,W30,F7,F7A,F30,F30A` skips B84/N84 if
   they are not needed. `--vm-posting` only takes effect when the workbook is built, so it must be on the
   `--rebuild` run.

3. **System export (optional, once the request-id list arrives).**
   ```
   .venv\Scripts\python examples\prepay\run_system_export.py --requests req.csv --curve <real_curve.csv> --name prepay_system
   ```
   This gives IMPORT sets `S84_FE_NOCSRA`, `S84_BE_IM` (no IMVM import set: FCA then uses −NEE_uncoll).

4. **Tables for the doc.** Pass the real credit inputs (all are placeholders by default):
   ```
   .venv\Scripts\python examples\prepay\xva_writeup.py examples\prepay\prepay_real_results.xlsx ^
       --spread-issuer <bp> --spread-spv <bp> --lgd <0.xx> --funding <bp> ^
       --pd-issuer <%> --pd-spv <%> --rw-issuer <%> --rw-spv <%>
   ```
   Adding the system run: list both results files and add a column for it, e.g.
   `--case "To reset (W7)=W7_FE_NOCSRA:W7_BE_IMVM:W7_BE_IM" --case "To Dec 2057 (W30)=W30_FE_NOCSRA:W30_BE_IMVM:W30_BE_IM" --case "System booking (S84)=S84_FE_NOCSRA:S84_BE_IM"`.
   Every `--case`, `--capital` and `--stress` replaces the defaults, so repeat the default ones you want to keep
   (`--help` lists them). Missing sets are skipped and listed under Notes.
   Output: `prepay_real_writeup.md` (tables in doc order), `_writeup.xlsx`, `_pfe_annual.csv` (chart rows),
   `_pfe_chart.png` and `_report.docx` (the full write-up in Word).

5. **Update the doc** (only where you can sign in to claude.ai; otherwise use `_report.docx`). In Claude Code on that computer, from this folder:
   > Update the Claude Doc https://claude.ai/artifact/YD666qn1ayMT7oQ9DsrwPE from
   > examples/prepay/prepay_real_writeup.md, following docs/prepay/XVA_WRITEUP.md. Replace the numbers in
   > each table and every figure quoted in the text, redraw the exposure chart from prepay_real_pfe_annual.csv,
   > and relabel inputs that are now real (no longer "illustrative"). Keep the wording otherwise.

## Where each output table goes in the doc

| `_writeup.md` section | Doc section | Also update in the doc text |
| --- | --- | --- |
| Results facing the Municipal Issuer | Results facing the Municipal Issuer (front-end) | Lead sentence; bullets: PFE path ($ at 2027 / reset / peak), extra CVA from the horizon, CSRA reference figures |
| Results facing the Gas Supplier SPV | Results facing the Gas Supplier SPV (back-end) | Lead (residual PFE), IM cushion (IM − peak gross PFE), VM / FCA bullet |
| XVA terms | Key observations (table) | Lead: total XVA and % of spread PV; points 2–4 |
| SA-CCR EAD and capital | SA-CCR EAD and capital | Lead and bullets (proxy vs literal RWA, Issuer vs SPV EAD) |
| Stress: cross-extinguishment fails | SPV section, last bullet; Executive summary caveats | |
| Chart data | Exposure profile against collateral (chart widget rows) | Caption: source file and netting sets |
| All of the above | Executive summary table (W7 = "to 2033", W30 = "to 2057"), Highlights, Caveats | Byline date; Caveats table "Used here" column |

The spread PV and lost spread depend on `--volume`, `--spread-usd`, `--spread-end` and `--rate`
(defaults: 20,000/d, $0.035, Dec 2033, 4%); change them if the terms change.
