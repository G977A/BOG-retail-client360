"""
Sampling primitives shared across the generator. Everything random flows
through here so behaviour is reproducible (single seeded Generator) and the
drawing logic lives in one reviewed place.
"""

from __future__ import annotations

import numpy as np


def make_rng(seed: int) -> np.random.Generator:
    """One seeded NumPy Generator for the whole run — pass it down, don't
    create new ones, or reproducibility breaks."""
    return np.random.default_rng(seed)


def truncated_normal(rng, mean, std, low=None, high=None, size=None):
    """Draw Normal(mean, std) then clip to [low, high].

    mean/std/low/high may be scalars or arrays broadcastable to `size`; pass
    per-customer arrays to draw a whole column at once. This clips rather than
    rejection-samples — simple and fast; it piles a little mass at the bounds,
    which is fine for the quantities here (counts, amounts, balances) where we
    only need positivity and a sane ceiling.
    """
    draws = rng.normal(mean, std, size=size)
    if low is not None:
        draws = np.maximum(draws, low)
    if high is not None:
        draws = np.minimum(draws, high)
    return draws


def weighted_choice(rng, options, weights, size):
    """Draw `size` items from `options` with probabilities `weights`.
    Returns (values, indices) — indices are handy for mapping per-persona
    parameter arrays without a Python loop."""
    idx = rng.choice(len(options), p=weights, size=size)
    return np.asarray(options, dtype=object)[idx], idx


def standardize(x):
    """Z-score a 1-D array; guards against zero variance."""
    x = np.asarray(x, dtype=float)
    std = x.std()
    if std == 0:
        return np.zeros_like(x)
    return (x - x.mean()) / std