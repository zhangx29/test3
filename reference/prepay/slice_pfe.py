# 97.5% PFE of the two-unpaid-period slice to the Issuer, above the CSRA, 20,000 MMBtu/d
# log-price OU (kappa 0.5, sigma 0.45), median-anchored at $5, flat or +2.5%/yr drift; same model as the review doc
import math, numpy as np
rng = np.random.default_rng(7)
N, steps, years = 20000, 12, 31
two = 2 * 31 * 20000 / 1e6
K, kappa, sigma = 5.00, 0.5, 0.45
for csra in (8.0, 12.0):
    for case, g in (("flat", 0.0), ("drift", 0.025)):
        x = np.full(N, math.log(5.0)); out = {}
        for k in range(1, years * steps + 1):
            t = k / steps
            th = math.log(5.0) + g * t - 0.5 * sigma**2 / (2 * kappa)
            x += kappa * (th - x) / steps + sigma * math.sqrt(1 / steps) * rng.standard_normal(N)
            if k % steps == 0 and k // steps in (1, 7, 15, 31):
                P = np.exp(x)
                out[k // steps] = np.quantile(np.maximum(two * (P - K) - csra, 0), 0.975)
        print(f"CSRA {csra:4.1f} {case:5s} 97.5% PFE above CSRA: " + "  ".join(f"y{y}: {v:5.2f}" for y, v in out.items()))
