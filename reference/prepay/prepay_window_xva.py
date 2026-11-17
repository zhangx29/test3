"""
Exposure profile for XVA on the gas prepay swaps when only a 2-month window can be lost.

Your booking: 84 swaps per side; trade j covers months j and j+1 (Jan27/Feb27 ... Dec33/Jan34).
If the counterparty defaults during month k, the loss is the 2-month window of trade k only
(the cross-extinguishment tears up everything else). So at each date:

    E(t) = max( V_{k(t)}(t) - C , 0 )        k(t) = the trade whose window starts in t's month

i.e. SELECT one trade per date, never SUM the 84 (a normal netting-set run sums them: every month
counted twice, and the whole remaining strip is exposed).

Compares four aggregations of the same simulated paths:
  window-fwd : trade k for defaults in month k (months k, k+1)        <- your stated assumption
  window-bwd : months k-1 and k (the missed payment + the month in progress at the missed payment)
  naive      : netting-set sum of all live trades of the 84 (what an engine does by default)
  strip      : one non-overlapping strip over the same months (full-notional view)

Price model (illustrative, replace with your engine's): two-factor forward curve
  dF(t,T)/F = SIG_S * exp(-KAPPA (T-t)) dW1 + SIG_L dW2   (Samuelson short factor + long factor, independent)
Monthly index fixes at the start of its month (first-of-month index). Payments at month end.
All amounts USD mm.
"""
import math
import csv
import numpy as np
from datetime import date

# ------------------------------------------------------------------ inputs
VAL = date(2026, 11, 13)
FIRST = date(2027, 1, 1)          # first delivery month
VOL_D = 20_000                    # MMBtu/d
N_TRADES = 84                     # booked 2-month windows
K_FRONT = 5.00                    # BMO pays fixed to Issuer, receives index
K_BACK_7Y, K_BACK_AFTER = 5.035, 5.00   # BMO receives fixed from SPV, pays index (to Dec33 / after)
RESET_MONTH = 84                  # 0-based index of Jan 2034 (first month after the reset)
F0 = 5.00                         # flat initial forward curve
SIG_S, KAPPA, SIG_L = 0.45, 1.0, 0.15
RATE = 0.04
CSRA = 8.0                        # Issuer-side reserve (assumed 20k/d sizing)
IM_SPV = 6.24                     # SPV IM = 2 x 31 days x 20,000 x 5.035
LGD = 0.60
SPREAD_ISSUER, SPREAD_SPV = 0.0050, 0.0100   # credit spreads -> flat hazard = spread / LGD
FUND_SPREAD = 0.0050              # for FVA on VM BMO posts to the SPV (if the CSA has BMO posting)
EXT_MONTHS = 372                  # Jan27..Dec57 for the contractual-horizon run
P_CONTINUE = 1.0                  # weight on windows after the reset (1 = contractual, <1 = expected life)
N_PATHS, SEED = 20_000, 11


# ------------------------------------------------------------------ calendar
def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def yf(d):
    return (d - VAL).days / 365.25


M_ALL = EXT_MONTHS + 1
starts = [add_months(FIRST, i) for i in range(M_ALL + 1)]
t_start = np.array([yf(d) for d in starts])                      # month i starts / fixes at t_start[i]
t_pay = t_start[1:]                                               # month i paid at t_start[i+1]
q = np.array([(starts[i + 1] - starts[i]).days * VOL_D / 1e6 for i in range(M_ALL)])
k_back = np.array([K_BACK_7Y if i < RESET_MONTH else K_BACK_AFTER for i in range(M_ALL)])
label = [d.strftime("%b%y") for d in starts]

# ------------------------------------------------------------------ simulate the two factors at each month start
rng = np.random.default_rng(SEED)
Y = np.zeros((N_PATHS, M_ALL + 1))
Z = np.zeros((N_PATHS, M_ALL + 1))
y, z, t_prev = np.zeros(N_PATHS), np.zeros(N_PATHS), 0.0
for i in range(M_ALL + 1):
    dt = t_start[i] - t_prev
    y = y * math.exp(-KAPPA * dt) + SIG_S * math.sqrt((1 - math.exp(-2 * KAPPA * dt)) / (2 * KAPPA)) * rng.standard_normal(N_PATHS)
    z = z + math.sqrt(dt) * rng.standard_normal(N_PATHS)
    Y[:, i], Z[:, i], t_prev = y, z, t_start[i]
VARY = SIG_S ** 2 * (1 - np.exp(-2 * KAPPA * t_start)) / (2 * KAPPA)


def fwd(k, months):
    """F(t_k, T_i) on every path for delivery months `months` (T_i = month start; i == k gives the fixing)."""
    tau = t_start[months] - t_start[k]
    a = np.exp(-KAPPA * tau)
    logf = (math.log(F0) + a[None, :] * Y[:, k][:, None] - 0.5 * (a ** 2)[None, :] * VARY[k]
            + SIG_L * Z[:, k][:, None] - 0.5 * SIG_L ** 2 * t_start[k])
    return np.exp(logf)


FIX = np.column_stack([fwd(i, np.array([i]))[:, 0] for i in range(M_ALL)])   # simulated fixing of each month


def window_values(k, months):
    """Front (vs Issuer) and back (vs SPV) values at t_k of the given months; fixed months use the path's fixing."""
    months = np.array([m for m in months if 0 <= m < M_ALL])
    if len(months) == 0:
        return np.zeros(N_PATHS), np.zeros(N_PATHS)
    x = np.where((months <= k)[None, :], FIX[:, months], fwd(k, months))
    df = np.exp(-RATE * (t_pay[months] - t_start[k]))
    vf = ((x - K_FRONT) * (q[months] * df)[None, :]).sum(axis=1)
    vb = ((k_back[months][None, :] - x) * (q[months] * df)[None, :]).sum(axis=1)
    return vf, vb


# booked trades: trade j covers months j, j+1  ->  how many live trades reference month i
LAST = N_TRADES                       # last referenced month index (Jan 2034)
count = np.array([(i <= N_TRADES - 1) + (1 <= i <= N_TRADES) for i in range(LAST + 1)])

S = lambda t, s: np.exp(-s / LGD * t)  # survival with flat hazard
grid = range(LAST + 1)                 # month starts Jan27 .. Jan34
prof = {m: {"EEg": [], "EEn": [], "PFEg": [], "PFEn": []} for m in ("fwd", "bwd", "naive", "strip")}
ee_spv, ee_vm, spread_pv = [], [], []
for k in grid:
    vals = {}
    vf, vb = window_values(k, [k, k + 1]) if k < N_TRADES else (np.zeros(N_PATHS), np.zeros(N_PATHS))
    vals["fwd"] = vf
    if k < N_TRADES:
        ee_spv.append(np.maximum(vb - IM_SPV, 0).mean())
        ee_vm.append(np.maximum(-vb, 0).mean())          # VM BMO would post on the 2-month back-end amount
    vals["bwd"] = window_values(k, [k - 1, k])[0]
    live = np.arange(k, LAST + 1)
    x = np.where((live <= k)[None, :], FIX[:, live], fwd(k, live))
    df = np.exp(-RATE * (t_pay[live] - t_start[k]))
    vals["naive"] = ((x - K_FRONT) * (count[live] * q[live] * df)[None, :]).sum(axis=1)
    vals["strip"] = ((x - K_FRONT) * (q[live] * df)[None, :]).sum(axis=1)
    spread_pv.append(float(((k_back[live] - K_FRONT) * q[live] * df).sum()))
    for m, v in vals.items():
        prof[m]["EEg"].append(np.maximum(v, 0).mean())
        prof[m]["EEn"].append(np.maximum(v - CSRA, 0).mean())
        prof[m]["PFEg"].append(np.quantile(np.maximum(v, 0), 0.975))
        prof[m]["PFEn"].append(np.quantile(np.maximum(v - CSRA, 0), 0.975))


def cva(ee, spread, t0=0):
    """LGD x sum DF(0,t_k) EE_k [S(t_k) - S(t_k+1)]: EE at the start of month k covers defaults during month k."""
    ee = np.asarray(ee)
    ks = np.arange(t0, t0 + len(ee))
    return LGD * float((np.exp(-RATE * t_start[ks]) * ee
                        * (S(t_start[ks], spread) - S(t_start[ks + 1], spread))).sum())


print(f"{N_PATHS} paths; flat ${F0}; short vol {SIG_S:.0%} decaying at kappa {KAPPA}, long vol {SIG_L:.0%}; "
      f"CSRA {CSRA}; Issuer spread {SPREAD_ISSUER * 1e4:.0f}bp, LGD {LGD:.0%}\n")
print(f"{'aggregation':34s} {'peak EE':>8s} {'peak PFE':>9s} {'peak EE':>8s} {'CVA Issuer':>11s} {'CVA Issuer':>11s}")
print(f"{'':34s} {'gross':>8s} {'gross':>9s} {'net CSRA':>8s} {'net CSRA':>11s} {'no CSRA':>11s}")
names = {"fwd": "window, months k & k+1 (yours)", "bwd": "window, months k-1 & k",
         "naive": "engine sum of the 84 trades", "strip": "one strip, same months"}
res = {}
for m in ("fwd", "bwd", "naive", "strip"):
    p = prof[m]
    n = N_TRADES if m == "fwd" else LAST + 1
    res[m] = (cva(p["EEn"][:n], SPREAD_ISSUER), cva(p["EEg"][:n], SPREAD_ISSUER))
    print(f"{names[m]:34s} {max(p['EEg']):8.3f} {max(p['PFEg']):9.3f} {max(p['EEn']):8.4f} "
          f"{res[m][0]:11.5f} {res[m][1]:11.5f}")

print(f"\nNaive / strip EE (gross) at Feb27, Jan30, Jun33: "
      + ", ".join(f"{prof['naive']['EEg'][k] / prof['strip']['EEg'][k]:.2f}" for k in (1, 36, 77)))
print(f"SPV side, window net of IM {IM_SPV}: peak EE {max(ee_spv):.5f}, CVA {cva(ee_spv, SPREAD_SPV):.6f}")
fca = FUND_SPREAD * float((np.exp(-RATE * t_start[:N_TRADES]) * np.array(ee_vm)
                           * np.diff(t_start[:N_TRADES + 1])).sum())
print(f"VM BMO posts to SPV on the 2-month amount: peak E[VM] {max(ee_vm):.3f}; FCA at {FUND_SPREAD * 1e4:.0f}bp {fca:.5f}")
lost = float((np.exp(-RATE * t_start[:LAST + 1]) * np.array(spread_pv)
              * (S(t_start[:LAST + 1], SPREAD_ISSUER) - S(t_start[1:LAST + 2], SPREAD_ISSUER)
                 + S(t_start[:LAST + 1], SPREAD_SPV) - S(t_start[1:LAST + 2], SPREAD_SPV))).sum())
print(f"Spread lost on either default (no claim, 100% loss): PV at start {spread_pv[0]:.3f}; CVA term {lost:.5f}")

# contractual horizon: windows to Dec57, weighted by P_CONTINUE after the reset
ee_ext_n, ee_ext_g = [], []
for k in range(EXT_MONTHS):
    vf, _ = window_values(k, [k, k + 1] if k + 1 < EXT_MONTHS else [k])
    w = 1.0 if k < RESET_MONTH else P_CONTINUE
    ee_ext_n.append(w * np.maximum(vf - CSRA, 0).mean())
    ee_ext_g.append(w * np.maximum(vf, 0).mean())
print(f"\nWindow to Dec57 (P_CONTINUE {P_CONTINUE}): CVA Issuer net CSRA {cva(ee_ext_n, SPREAD_ISSUER):.5f}, "
      f"no CSRA {cva(ee_ext_g, SPREAD_ISSUER):.5f}; peak EE net {max(ee_ext_n):.4f} in {label[int(np.argmax(ee_ext_n))]}")

with open("prepay_window_profiles.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["month", "t_years", "EE_window_gross", "EE_window_netCSRA", "PFE975_window_gross",
                "PFE975_window_netCSRA", "EE_window_bwd_gross", "EE_naive84_gross", "EE_strip_gross",
                "PFE975_naive84_gross", "PFE975_strip_gross", "E_VM_posted_SPV", "spread_PV"])
    for k in grid:
        g = lambda m, key: prof[m][key][k]
        w.writerow([label[k], round(t_start[k], 4), round(g("fwd", "EEg"), 5), round(g("fwd", "EEn"), 6),
                    round(g("fwd", "PFEg"), 5), round(g("fwd", "PFEn"), 5), round(g("bwd", "EEg"), 5),
                    round(g("naive", "EEg"), 4), round(g("strip", "EEg"), 4), round(g("naive", "PFEg"), 4),
                    round(g("strip", "PFEg"), 4), round(ee_vm[k], 5) if k < N_TRADES else "",
                    round(spread_pv[k], 5)])
print("\nwrote prepay_window_profiles.csv")
