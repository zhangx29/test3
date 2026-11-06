"""Commodity asset class: N-factor futures model, bullet swaps, SA-CCR hedging-set inputs.

Each futures contract is a named underlying, so the shared engine (grid, collateral, statistics,
bucketing, SA-CCR RC / multiplier / cap) treats its simulated path like any other underlying.
"""
