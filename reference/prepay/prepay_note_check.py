"""Check the 13 Nov booking note: literal booking vs the proposed PFE/capital proxy, 20,000 MMBtu/d.
OSFI CAR 2027 ch.7 SA-CCR, ch.5 IRB (corporate), ch.8 BA-CVA (reduced). USD mm. Illustrative PD/LGD/RW."""
import math
from datetime import date
from statistics import NormalDist

N = NormalDist()
VAL = date(2026, 11, 13)          # valuation date
VOL = 20_000                      # MMBtu/d
PRICE = 5.00                      # flat curve
RATE = 0.04
ALPHA, SF = 1.4, 0.18
PD = {"Issuer": 0.0010, "SPV": 0.0030}
LGD = 0.45
RW = {"Issuer": 0.01, "SPV": 0.05}


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def yf(d):
    return (d - VAL).days / 365.25


def months(start, end_incl):
    """[(start, end_excl)] for each month from start to end_incl (both month-start dates)."""
    out, d = [], start
    while d <= end_incl:
        out.append((d, add_months(d, 1)))
        d = add_months(d, 1)
    return out


class Leg:
    def __init__(self, side, pieces):           # pieces: [(strike, [months])]
        self.side = side
        self.units = sum((e - s).days for _, ms in pieces for s, e in ms) * VOL / 1e6
        self.V = sum((e - s).days * VOL / 1e6 * (PRICE - k) * side * math.exp(-RATE * yf(e))
                     for k, ms in pieces for s, e in ms)
        self.M = max(yf(e) for _, ms in pieces for s, e in ms)

    def saccr(self, C):
        d = PRICE * self.units
        mf = math.sqrt(min(max(self.M, 10 / 250), 1.0))
        addon = SF * d * mf
        mult = min(1.0, 0.05 + 0.95 * math.exp((self.V - C) / (2 * 0.95 * addon)))
        rc = max(self.V - C, 0.0)
        return dict(units=self.units, M=self.M, V=self.V, addon=addon, mult=mult,
                    EAD=ALPHA * (rc + mult * addon), LR=ALPHA * (max(self.V, 0) + addon))


def irb_k(pd, M):
    pd = max(pd, 0.0005)
    w = (1 - math.exp(-50 * pd)) / (1 - math.exp(-50))
    R = 0.12 * w + 0.24 * (1 - w)
    b = (0.11852 - 0.05478 * math.log(pd)) ** 2
    M = min(max(M, 1.0), 5.0)
    k = LGD * N.cdf((N.inv_cdf(pd) + math.sqrt(R) * N.inv_cdf(0.999)) / math.sqrt(1 - R)) - pd * LGD
    return k * (1 + (M - 2.5) * b) / (1 - 1.5 * b)


def scva(c, ead, M):
    M = max(M, 1.0)
    return RW[c] * M * ead * (1 - math.exp(-0.05 * M)) / (0.05 * M) / ALPHA


def run(label, front, back, csra, im):
    fe, be = front.saccr(csra), back.saccr(im)
    ccr = 12.5 * (irb_k(PD["Issuer"], fe["M"]) * fe["EAD"] + irb_k(PD["SPV"], be["M"]) * be["EAD"])
    s = [scva("Issuer", fe["EAD"], fe["M"]), scva("SPV", be["EAD"], be["M"])]
    kcva = 0.65 * math.sqrt((0.5 * sum(s)) ** 2 + 0.75 * sum(x * x for x in s))
    cva = 12.5 * kcva
    print(f"{label:46s} M {fe['M']:5.1f}  EAD iss {fe['EAD']:7.2f} spv {be['EAD']:7.2f}  "
          f"CCR RWA {ccr:8.2f}  CVA RWA {cva:8.2f}  TOTAL {ccr + cva:8.2f}  LR exp {fe['LR'] + be['LR']:7.1f}")
    return ccr + cva


J27, D33, J34, D57 = date(2027, 1, 1), date(2033, 12, 1), date(2034, 1, 1), date(2057, 12, 1)
N33, N57 = date(2033, 11, 1), date(2057, 11, 1)

for csra, im, tag in ((8.0, 6.2, "CSRA 8.0 (pro rata), IM 6.2"), (12.0, 6.2, "CSRA 12.0 (unchanged), IM 6.2"),
                      (0.0, 6.2, "CSRA not recognised, IM 6.2")):
    print(f"\n--- {tag}, flat ${PRICE} ---")
    run("Literal booking, Jan27-Dec57 (31y)",
        Leg(+1, [(5.00, months(J27, D57))]),
        Leg(-1, [(5.035, months(J27, D33)), (5.00, months(J34, D57))]), csra, im)
    run("Full notional, hard stop Dec33 (7y)",
        Leg(+1, [(5.00, months(J27, D33))]), Leg(-1, [(5.035, months(J27, D33))]), csra, im)
    run("Note's proxy: Nov33-Dec33 @5.00",
        Leg(+1, [(5.00, months(N33, D33))]), Leg(-1, [(5.00, months(N33, D33))]), csra, im)
    run("Same proxy at final date: Nov57-Dec57 @5.00",
        Leg(+1, [(5.00, months(N57, D57))]), Leg(-1, [(5.00, months(N57, D57))]), csra, im)

# revenue: 3.5c x 20,000/d Jan27-Dec33, PV at 4%
pv = sum((e - s).days * VOL * 0.035 * math.exp(-RATE * yf(e)) for s, e in months(J27, D33)) / 1e6
und = sum((e - s).days * VOL * 0.035 for s, e in months(J27, D33)) / 1e6
print(f"\nSpread revenue Jan27-Dec33: undiscounted {und:.2f}, PV {pv:.2f}, per year {VOL * 365 * 0.035 / 1e6:.3f}")
two = 2 * 31 * VOL / 1e6
print(f"Two 31-day periods = {two:.2f}mm MMBtu; back-end max at $0 = {two * 5.035:.2f}; "
      f"CSRA 8 covers to ${5 + 8 / two:.2f}, CSRA 12 to ${5 + 12 / two:.2f}")
for T in (7, 31):
    ann = (1 - math.exp(-RATE * T)) / RATE
    print(f"MTM per $1/MMBtu, one leg, {T}y: {VOL * 365.25 * ann / 1e6:.1f}")
print(f"MTM per $1/MMBtu, two unpaid periods: {two:.2f}")
