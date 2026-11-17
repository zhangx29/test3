"""
Gas prepay swaps - SA-CCR EAD, IRB CCR RWA and BA-CVA RWA for each horizon.

Replicates the review doc and adds the 84-trade "as booked" view.
Rules: OSFI CAR 2027 ch.7 (SA-CCR), CAR ch.5 (IRB), CAR ch.8 (BA-CVA, reduced).
All amounts USD mm. Every input is at the top so it can be changed.

Run:  python3 prepay_ead_rwa.py            (prints tables, writes prepay_ead_rwa.csv)
"""
import math
import csv
from datetime import date
from statistics import NormalDist

N = NormalDist()

# ----------------------------------------------------------------------------- inputs
VOL_D     = 30_000      # MMBtu per day
K_FRONT   = 4.96        # front-end fixed price: BMO pays fixed, receives index (vs Issuer)
K_BACK    = 5.00        # back-end fixed price: BMO receives fixed, pays index (vs SPV)
PRICE     = 5.00        # current gas price used for adjusted notional and MTM ($/MMBtu)
RATE      = 0.04        # continuous discount rate for MTM
START     = date(2026, 12, 1)   # first delivery month
ALPHA     = 1.4         # CAR ch.7 alpha
SF_GAS    = 0.18        # supervisory factor, oil/gas (CAR ch.7)
IM_SPV    = 9.3         # SPV initial margin held at custodian, pledged to BMO
CSRA      = 12.0        # Commodity Swap Reserve Account
RECOGNISE_CSRA = True   # set False if Legal cannot confirm BMO's claim on the CSRA

# IRB (illustrative - replace with BMO's internal ratings)
PD  = {"Issuer": 0.0010, "SPV": 0.0030}
LGD = {"Issuer": 0.45,   "SPV": 0.45}
# BA-CVA risk weights (CAR ch.8 table): local government IG 1%/HY-NR 4%; financials or other IG 5%/NR 12%
RW_CVA = {"Issuer": 0.01, "SPV": 0.05}
RHO_CVA, DS_CVA = 0.50, 0.65


# ----------------------------------------------------------------------------- calendar
def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def yf(d):                      # year fraction from today (START used as "today")
    return (d - START).days / 365.25


def month_periods(n_months):
    """[(start, end_exclusive)] for each delivery month."""
    return [(add_months(START, i), add_months(START, i + 1)) for i in range(n_months)]


# ----------------------------------------------------------------------------- a trade
class Trade:
    """One commodity swap from BMO's side.
    side +1 = BMO receives index (long gas: front-end vs Issuer)
    side -1 = BMO pays index    (short gas: back-end vs SPV)
    periods = delivery months it references; M = years to its last day (CAR ch.7 para 124(a))."""

    def __init__(self, cpty, side, strike, periods, maturity_override=None):
        self.cpty, self.side, self.strike, self.periods = cpty, side, strike, periods
        self.units = sum((e - s).days for s, e in periods) * VOL_D / 1e6          # mm MMBtu
        self.M = maturity_override if maturity_override is not None else yf(periods[-1][1])
        # MTM: undiscounted settlement per month = units_month * (P - K) * side, discounted to month end
        self.V = sum(((e - s).days * VOL_D / 1e6) * (PRICE - strike) * side
                     * math.exp(-RATE * yf(e)) for s, e in periods)

    @property
    def notional(self):          # adjusted notional d = price x units (para 129)
        return PRICE * self.units

    @property
    def MF(self):                # unmargined maturity factor (para 139), M floored at 10 business days
        return math.sqrt(min(max(self.M, 10 / 250), 1.0))


# ----------------------------------------------------------------------------- SA-CCR
def saccr(trades, C):
    """One unmargined netting set, one commodity hedging set (energy), one commodity type (gas).
    Same type -> full offset within the type, so effective notional = signed sum."""
    eff = sum(t.side * t.notional * t.MF for t in trades)     # delta = +1/-1 for linear swaps
    addon = SF_GAS * abs(eff)
    V = sum(t.V for t in trades)
    rc = max(V - C, 0.0)
    mult = 1.0 if addon == 0 else min(1.0, 0.05 + 0.95 * math.exp((V - C) / (2 * 0.95 * addon)))
    pfe = mult * addon
    return dict(V=V, C=C, RC=rc, AddOn=addon, mult=mult, PFE=pfe, EAD=ALPHA * (rc + pfe))


# ----------------------------------------------------------------------------- IRB (corporate)
def irb_k(pd, lgd, M):
    pd = max(pd, 0.0005)                                       # corporate PD floor 5 bp
    w = (1 - math.exp(-50 * pd)) / (1 - math.exp(-50))
    R = 0.12 * w + 0.24 * (1 - w)
    b = (0.11852 - 0.05478 * math.log(pd)) ** 2
    M = min(max(M, 1.0), 5.0)                                  # IRB M: floor 1y, cap 5y
    k = lgd * N.cdf((N.inv_cdf(pd) + math.sqrt(R) * N.inv_cdf(0.999)) / math.sqrt(1 - R)) - pd * lgd
    return k * (1 + (M - 2.5) * b) / (1 - 1.5 * b)


def eff_maturity(trades):
    """Notional-weighted average remaining maturity of the netting set (IRB effective maturity)."""
    w = sum(t.notional for t in trades)
    return sum(t.notional * t.M for t in trades) / w


# ----------------------------------------------------------------------------- BA-CVA (reduced)
def scva(cpty, ead, M):
    M = max(M, 1.0)                                            # no 5y cap for CVA; 1y floor kept
    df = (1 - math.exp(-0.05 * M)) / (0.05 * M)
    return RW_CVA[cpty] * M * ead * df / ALPHA


def ba_cva(scvas):
    s = sum(scvas)
    return DS_CVA * math.sqrt((RHO_CVA * s) ** 2 + (1 - RHO_CVA ** 2) * sum(x * x for x in scvas))


# ----------------------------------------------------------------------------- the variants
def variant_trades(name):
    if name == "1. 2-month (legal form, one live trade)":
        p = month_periods(2)
        return [Trade("Issuer", +1, K_FRONT, p)], [Trade("SPV", -1, K_BACK, p)]
    if name == "2a. 7-year AS BOOKED (84 overlapping 2-month trades)":
        fe, be = [], []
        for k in range(84):                                    # trade k covers months k and k+1
            p = month_periods(k + 2)[k:k + 2]
            fe.append(Trade("Issuer", +1, K_FRONT, p))
            be.append(Trade("SPV", -1, K_BACK, p))
        return fe, be
    if name == "2b. 7-year (one contract, months 1-84, M = 7y)":
        p = month_periods(84)
        return [Trade("Issuer", +1, K_FRONT, p)], [Trade("SPV", -1, K_BACK, p)]
    if name == "3. 30-year (one contract, months 1-360, M = 30y)":
        p = month_periods(360)
        return [Trade("Issuer", +1, K_FRONT, p)], [Trade("SPV", -1, K_BACK, p)]
    if name.startswith("Ref. two-period notional"):
        T = 7.0 if "7y" in name else 30.0
        p = month_periods(2)
        return ([Trade("Issuer", +1, K_FRONT, p, maturity_override=T)],
                [Trade("SPV", -1, K_BACK, p, maturity_override=T)])
    raise ValueError(name)


VARIANTS = [
    "1. 2-month (legal form, one live trade)",
    "2a. 7-year AS BOOKED (84 overlapping 2-month trades)",
    "2b. 7-year (one contract, months 1-84, M = 7y)",
    "3. 30-year (one contract, months 1-360, M = 30y)",
    "Ref. two-period notional, M = 7y",
    "Ref. two-period notional, M = 30y",
]

rows = []
for v in VARIANTS:
    fe, be = variant_trades(v)
    out = {}
    for cpty, trades, C in (("Issuer", fe, CSRA if RECOGNISE_CSRA else 0.0), ("SPV", be, IM_SPV)):
        r = saccr(trades, C)
        M_cva = max(t.M for t in trades) if len(trades) == 1 else eff_maturity(trades)
        M_irb = M_cva
        k = irb_k(PD[cpty], LGD[cpty], M_irb)
        r.update(trades=len(trades), units=sum(t.units for t in trades),
                 notional=sum(t.notional for t in trades), M=M_cva,
                 K_irb=k, RWA_ccr=12.5 * k * r["EAD"], SCVA=scva(cpty, r["EAD"], M_cva))
        out[cpty] = r
    k_cva = ba_cva([out["Issuer"]["SCVA"], out["SPV"]["SCVA"]])
    rwa_ccr = out["Issuer"]["RWA_ccr"] + out["SPV"]["RWA_ccr"]
    rows.append(dict(variant=v, out=out, K_CVA=k_cva, RWA_CVA=12.5 * k_cva, RWA_CCR=rwa_ccr,
                     RWA_total=rwa_ccr + 12.5 * k_cva))

# ----------------------------------------------------------------------------- print
print(f"Price ${PRICE}, CSRA recognised: {RECOGNISE_CSRA}\n")
hdr = f"{'variant':52s} {'cpty':6s} {'#tr':>4s} {'units':>7s} {'notnl':>8s} {'M':>5s} {'V':>7s} {'C':>5s} {'AddOn':>8s} {'mult':>5s} {'EAD':>8s} {'CCR RWA':>8s}"
print(hdr)
for r in rows:
    for cpty in ("Issuer", "SPV"):
        o = r["out"][cpty]
        print(f"{r['variant']:52s} {cpty:6s} {o['trades']:4d} {o['units']:7.2f} {o['notional']:8.2f} {o['M']:5.2f} "
              f"{o['V']:7.3f} {o['C']:5.1f} {o['AddOn']:8.3f} {o['mult']:5.3f} {o['EAD']:8.3f} {o['RWA_ccr']:8.3f}")
    print(f"{'':52s} BA-CVA K {r['K_CVA']:.3f}  CVA RWA {r['RWA_CVA']:.2f}  CCR RWA {r['RWA_CCR']:.2f}  TOTAL RWA {r['RWA_total']:.2f}\n")

with open("prepay_ead_rwa.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["variant", "cpty", "trades", "units_mmMMBtu", "adj_notional", "M_years", "V", "C", "RC",
                "AddOn", "multiplier", "PFE", "EAD", "IRB_K", "CCR_RWA", "SCVA", "BA_CVA_K", "CVA_RWA", "TOTAL_RWA"])
    for r in rows:
        for cpty in ("Issuer", "SPV"):
            o = r["out"][cpty]
            w.writerow([r["variant"], cpty, o["trades"], round(o["units"], 4), round(o["notional"], 4),
                        round(o["M"], 3), round(o["V"], 4), o["C"], round(o["RC"], 4), round(o["AddOn"], 4),
                        round(o["mult"], 4), round(o["PFE"], 4), round(o["EAD"], 4), round(o["K_irb"], 5),
                        round(o["RWA_ccr"], 4), round(o["SCVA"], 4), round(r["K_CVA"], 4),
                        round(r["RWA_CVA"], 4), round(r["RWA_total"], 4)])

# ----------------------------------------------------------------------------- deck charts
U2 = VOL_D * 62 / 1e6
print("Deck chart replication (two 31-day periods = 1.86mm MMBtu)")
for P in (0, 4.96, 5, 11.41, 17.86, 20):
    fe = U2 * (P - K_FRONT); be = U2 * (K_BACK - P)
    print(f"P={P:5.2f}  front-end MTM {fe:7.2f}  CSRA headroom {CSRA - max(fe, 0):7.2f}  "
          f"back-end MTM {be:7.2f}  SPV IM headroom {IM_SPV - max(be, 0):5.2f}  BMO VM posted {max(-be, 0):6.2f}")
