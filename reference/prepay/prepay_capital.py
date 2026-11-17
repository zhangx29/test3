"""Gas prepay commodity swaps - SA-CCR, BA-CVA and simulated exposure.

All figures USD mm unless noted. Illustrative: price = strike at inception,
OSFI CAR 2027 ch.7 SA-CCR (alpha 1.4, gas SF 18%) and CAR ch.8 BA-CVA (reduced).
"""
import math
import json
import numpy as np

VOL_D = 30_000                 # MMBtu/d
K_FRONT, K_BACK = 4.96, 5.00   # fixed prices
P0 = 5.00                      # current price assumption ($/MMBtu)
CSRA, IM = 12.0, 9.3           # $mm
ALPHA, SF = 1.4, 0.18
R = 0.04                       # discount rate for V on long readings
UNITS_2M = VOL_D * 62 / 1e6    # mm MMBtu, two 31-day periods (deck basis)


def units(years):
    return VOL_D * 365.25 * years / 1e6


def annuity_units(years, r=R):
    # discounted volume (mm MMBtu) for continuous delivery
    return VOL_D * 365.25 * (1 - math.exp(-r * years)) / r / 1e6


def saccr(units_mm, M_years, V, C, P=P0, alpha=ALPHA):
    d = P * units_mm
    MF = math.sqrt(min(max(M_years, 10 / 250), 1.0))
    addon = SF * d * MF
    mult = min(1.0, 0.05 + 0.95 * math.exp((V - C) / (2 * 0.95 * addon)))
    rc = max(V - C, 0.0)
    pfe = mult * addon
    return dict(EAD=alpha * (rc + pfe), RC=rc, PFE=pfe, AddOn=addon, mult=mult, notional=d, MF=MF)


def ba_cva(ead, M, rw):
    M = max(M, 1.0)  # Ch.5 one-year floor (no short-term exemption assumed)
    DF = (1 - math.exp(-0.05 * M)) / (0.05 * M)
    return 0.65 * (1 / ALPHA) * rw * M * ead * DF


RW_ISSUER_IG, RW_ISSUER_NR = 0.01, 0.04      # local government IG / NR
RW_SPV_IG, RW_SPV_NR = 0.05, 0.12            # financials/other IG / NR

rows = []

# Reading A - legal form: 2-month units, M = 2 months
vF = (P0 - K_FRONT) * UNITS_2M
vB = (K_BACK - P0) * UNITS_2M
for label, u, M, vf, vb, Mcva in [
    ("A  Legal form: 2-month units, M = 2 months", UNITS_2M, 2 / 12, vF, vB, 1.0),
    ("B7 Hybrid: 2-month units, M = 7y", UNITS_2M, 7, vF, vB, 7.0),
    ("B30 Hybrid: 2-month units, M = 30y", UNITS_2M, 30, vF, vB, 30.0),
    ("C7 Full: units and M to 7y reset", units(7), 7,
     (P0 - K_FRONT) * annuity_units(7), (K_BACK - P0) * annuity_units(7), 7.0),
    ("C30 Full: units and M to 30y final", units(30), 30,
     (P0 - K_FRONT) * annuity_units(30), (K_BACK - P0) * annuity_units(30), 30.0),
]:
    fe = saccr(u, M, vf, CSRA)            # issuer, CSRA recognised
    fe_nc = saccr(u, M, vf, 0.0)          # issuer, CSRA not recognised
    be = saccr(u, M, vb, IM)              # SPV, IM recognised
    rows.append(dict(
        reading=label, M_saccr=M, M_cva=Mcva,
        notional=fe["notional"],
        EAD_issuer=fe["EAD"], EAD_issuer_noCSRA=fe_nc["EAD"], EAD_spv=be["EAD"],
        CVA_issuer_IG=ba_cva(fe["EAD"], Mcva, RW_ISSUER_IG),
        CVA_issuer_NR_noCSRA=ba_cva(fe_nc["EAD"], Mcva, RW_ISSUER_NR),
        CVA_spv_IG=ba_cva(be["EAD"], Mcva, RW_SPV_IG),
        CVA_spv_NR=ba_cva(be["EAD"], Mcva, RW_SPV_NR),
        LR_issuer=ALPHA * (max(vf, 0) + fe["AddOn"]),   # leverage: mult=1, no collateral
        LR_spv=ALPHA * (max(vb, 0) + be["AddOn"]),
    ))

print(f"{'reading':45s} {'notional':>9s} {'EAD_iss':>8s} {'EAD_iss_noC':>11s} {'EAD_spv':>8s} "
      f"{'CVA_iss':>8s} {'CVA_issNR':>9s} {'CVA_spvIG':>9s} {'CVA_spvNR':>9s} {'LR_iss':>8s} {'LR_spv':>8s}")
for r in rows:
    print(f"{r['reading']:45s} {r['notional']:9.1f} {r['EAD_issuer']:8.2f} {r['EAD_issuer_noCSRA']:11.2f} "
          f"{r['EAD_spv']:8.2f} {r['CVA_issuer_IG']:8.3f} {r['CVA_issuer_NR_noCSRA']:9.3f} "
          f"{r['CVA_spv_IG']:9.3f} {r['CVA_spv_NR']:9.3f} {r['LR_issuer']:8.1f} {r['LR_spv']:8.1f}")

# Deck arithmetic checks
print("\nChecks")
print("two periods MMBtu (mm):", UNITS_2M)
print("back-end max at $0 ($mm):", UNITS_2M * K_BACK)
print("front-end at $0 ($mm):", -UNITS_2M * K_FRONT)
print("CSRA covers two periods up to $", round(K_FRONT + CSRA / UNITS_2M, 2))
print("CSRA covers one 31-day period up to $", round(K_FRONT + CSRA / (UNITS_2M / 2), 2))
print("front-end at $20 ($mm):", round(UNITS_2M * (20 - K_FRONT), 2))
print("spread $mm/yr:", round(0.04 * VOL_D * 365 / 1e6, 3))
print("spread $mm over 7y / 30y:", round(0.04 * VOL_D * 365 * 7 / 1e6, 2), round(0.04 * VOL_D * 365 * 30 / 1e6, 2))

# ---------------------------------------------------------------------------
# Simulated two-unpaid-amount exposure to the Issuer (after CSRA) over 30y
# log-price OU around a long-run level; kappa 0.5, sigma 0.45 (stationary sd 0.45)
rng = np.random.default_rng(7)
N, years, steps_per_year = 20_000, 30, 12
dt = 1 / steps_per_year
T = years * steps_per_year
kappa, sigma = 0.5, 0.45
out = {}
for case, g in [("flat", 0.0), ("drift", 0.025)]:
    x = np.full(N, math.log(P0))
    ee, pfe, pexc, ee_gross = [], [], [], []
    for k in range(1, T + 1):
        t = k * dt
        theta = math.log(P0) + g * t - 0.5 * sigma**2 / (2 * kappa)  # median-anchored mean
        x = x + kappa * (theta - x) * dt + sigma * math.sqrt(dt) * rng.standard_normal(N)
        if k % steps_per_year == 0:
            P = np.exp(x)
            gross = UNITS_2M * np.maximum(P - K_FRONT, 0)
            net = np.maximum(gross - CSRA, 0)
            ee.append(net.mean()); pfe.append(np.quantile(net, 0.975))
            pexc.append((P > K_FRONT + CSRA / UNITS_2M).mean()); ee_gross.append(gross.mean())
    out[case] = dict(EE=ee, PFE975=pfe, P_exceed=pexc, EE_gross=ee_gross)

# Economic CVA on the issuer slice: hazard 0.5%/yr, LGD 60%, r 4%
lam, lgd = 0.005, 0.6
for case in out:
    ee = out[case]["EE"]
    cva7 = sum(lgd * ee[i] * (math.exp(-lam * i) - math.exp(-lam * (i + 1))) * math.exp(-R * (i + 1)) for i in range(7))
    cva30 = sum(lgd * ee[i] * (math.exp(-lam * i) - math.exp(-lam * (i + 1))) * math.exp(-R * (i + 1)) for i in range(30))
    out[case]["econ_CVA_7y"] = cva7
    out[case]["econ_CVA_30y"] = cva30

print("\nSimulated issuer slice after $12mm CSRA (USD mm)")
for case in out:
    o = out[case]
    print(case, "PFE97.5 y1,y7,y15,y30:", [round(o["PFE975"][i], 1) for i in (0, 6, 14, 29)],
          "EE y1,y7,y30:", [round(o["EE"][i], 2) for i in (0, 6, 29)],
          "P(>11.41) y7,y30:", [round(o["P_exceed"][i], 3) for i in (6, 29)],
          "econ CVA 7y/30y:", round(o["econ_CVA_7y"], 3), round(o["econ_CVA_30y"], 3))

json.dump(dict(rows=rows, sim={c: {k: (v if not isinstance(v, list) else [round(z, 3) for z in v])
                                   for k, v in out[c].items()} for c in out}),
          open("prepay_results.json", "w"), indent=1)
