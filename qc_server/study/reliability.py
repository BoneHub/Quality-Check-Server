"""Agreement on nominal verdicts: % agreement, Krippendorff's alpha and Gwet's AC1, each with a
95% confidence interval from resampling whole subjects.

The data are *units* -- in a study, one bone of one subject -- each with the values coders
gave it: one rater's readings of it (intra-rater), or several raters' first readings
(inter-rater). A unit may lack values, from a rater who has not read the subject yet. Only a
unit with two values or more shows agreement or disagreement; a unit with one value still
counts towards how common each category is, in AC1.

* **% agreement** -- Gwet's p_a: per unit, the share of pairs of its values that agree;
  averaged over the units with two values or more. With two coders, the share of units on
  which they agree.
* **Krippendorff's alpha** (nominal) -- 1 - D_o / D_e, from the coincidence matrix of the
  values of units with two or more (Krippendorff 2011). Undefined when every value is the
  same, as nothing then tells agreement from chance.
* **Gwet's AC1** -- (p_a - p_e) / (1 - p_e), with p_e = sum_k pi_k (1 - pi_k) / (q - 1), and
  pi_k the share of category k, averaged over the units with a value (Gwet 2008, 2014). Stays
  meaningful when one category is rare, where kappa-like coefficients such as alpha collapse.

Each coefficient is a function of a few sums over the units (:func:`unit_sums`). A subject's
units add up to the subject's sums, a bootstrap resample of subjects adds up the sums of the
subjects drawn, and the point estimate and the interval come from the same formulas.

References:
    Krippendorff, K. (2011). Computing Krippendorff's Alpha-Reliability. Annenberg School for
    Communication, University of Pennsylvania.
    Gwet, K. L. (2008). Computing inter-rater reliability and its variance in the presence of
    high agreement. British Journal of Mathematical and Statistical Psychology, 61, 29-48.
    Gwet, K. L. (2014). Handbook of Inter-Rater Reliability, 4th ed. Advanced Analytics.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np

#: The confidence level of every interval.
CONFIDENCE = 0.95

#: The share of bootstrap resamples in which a coefficient must be defined for its interval.
_MIN_DEFINED_SHARE = 0.5


@dataclass(frozen=True)
class Estimate:
    """A coefficient and its 95% confidence interval; None where undefined."""

    value: float | None
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True)
class Agreement:
    """How far a set of coders agree on a set of units."""

    subjects: int  # subjects with a unit that shows agreement or disagreement
    items: int  # such units: two values or more
    values: int  # the values of those units
    shares: tuple[float, ...] | None  # of each category among those values
    agreement: Estimate  # as a proportion, 0 to 1
    alpha: Estimate
    ac1: Estimate


# --------------------------------------------------------------------- the sums
def _layout(q: int) -> dict[str, slice]:
    """Where each sum sits in a row of :func:`unit_sums`."""
    return {
        "coincidences": slice(0, q * q),
        "pairs_agreeing": slice(q * q, q * q + 1),
        "pairable": slice(q * q + 1, q * q + 2),
        "shares": slice(q * q + 2, q * q + 2 + q),
        "rated": slice(q * q + 2 + q, q * q + 3 + q),
    }


def unit_sums(counts: np.ndarray) -> np.ndarray:
    """Per unit, the sums the coefficients are made of, one row each.

    ``counts`` holds, per unit (row), how many of its values fall in each category (column).
    A row is the unit's contribution to the coincidence matrix, n_c (n_k - [c = k]) / (m - 1);
    its share of agreeing pairs, sum_k n_k (n_k - 1) / (m (m - 1)), and a 1 that counts it,
    when it has m >= 2 values; and its share of each category, n_k / m, and a 1 that counts
    it, when it has a value at all.
    """
    counts = np.asarray(counts, dtype=float)
    if counts.ndim != 2:
        raise ValueError("counts must have one row per unit and one column per category")
    n_units, q = counts.shape
    layout = _layout(q)
    m = counts.sum(axis=1)
    pairable = m >= 2
    rated = m >= 1
    pair_count = np.where(pairable, m - 1, 1.0)

    coincidences = counts[:, :, None] * counts[:, None, :]
    coincidences[:, np.arange(q), np.arange(q)] -= counts
    coincidences /= pair_count[:, None, None]
    coincidences[~pairable] = 0.0

    agreeing = (counts * (counts - 1)).sum(axis=1) / np.where(pairable, m * (m - 1), 1.0)
    agreeing[~pairable] = 0.0
    shares = counts / np.where(rated, m, 1.0)[:, None]
    shares[~rated] = 0.0

    sums = np.zeros((n_units, q * q + q + 3))
    sums[:, layout["coincidences"]] = coincidences.reshape(n_units, q * q)
    sums[:, layout["pairs_agreeing"]] = agreeing[:, None]
    sums[:, layout["pairable"]] = pairable[:, None]
    sums[:, layout["shares"]] = shares
    sums[:, layout["rated"]] = rated[:, None]
    return sums


def coefficients(sums: np.ndarray, q: int) -> dict[str, np.ndarray]:
    """% agreement (as a proportion), alpha and AC1 from unit sums added up: one row of
    :func:`unit_sums` totals, or several (a bootstrap's). NaN where a coefficient is undefined."""
    sums = np.atleast_2d(np.asarray(sums, dtype=float))
    layout = _layout(q)
    coincidences = sums[:, layout["coincidences"]].reshape(-1, q, q)
    agreeing = sums[:, layout["pairs_agreeing"]][:, 0]
    pairable = sums[:, layout["pairable"]][:, 0]
    shares = sums[:, layout["shares"]]
    rated = sums[:, layout["rated"]][:, 0]

    with np.errstate(divide="ignore", invalid="ignore"):
        agreement = np.where(pairable > 0, agreeing / pairable, np.nan)

        # alpha = 1 - D_o / D_e = 1 - (n - 1) * sum_{c != k} o_ck / sum_{c != k} n_c n_k
        marginals = coincidences.sum(axis=2)
        n = marginals.sum(axis=1)
        disagreeing = n - np.trace(coincidences, axis1=1, axis2=2)
        by_chance = n**2 - (marginals**2).sum(axis=1)
        defined = (n >= 2) & (by_chance > 1e-12)
        alpha = np.where(defined, 1.0 - (n - 1.0) * disagreeing / np.where(defined, by_chance, 1.0), np.nan)

        pi = shares / np.where(rated > 0, rated, 1.0)[:, None]
        expected = (pi * (1.0 - pi)).sum(axis=1) / (q - 1)
        ac1 = np.where(pairable > 0, (agreement - expected) / (1.0 - expected), np.nan)
    return {"agreement": agreement, "alpha": alpha, "ac1": ac1}


# --------------------------------------------------------- units as lists of values
def counts_of(units: Sequence[Sequence[Hashable | None]], categories: Sequence[Hashable]) -> np.ndarray:
    """``counts`` for :func:`unit_sums` from units given as their values; None is a missing value."""
    index = {category: column for column, category in enumerate(categories)}
    counts = np.zeros((len(units), len(categories)))
    for row, values in enumerate(units):
        for value in values:
            if value is None:
                continue
            if value not in index:
                raise ValueError(f"{value!r} is not one of the categories {list(categories)}")
            counts[row, index[value]] += 1
    return counts


def _point(units, categories, name: str) -> float | None:
    if len(categories) < 2:
        raise ValueError("agreement needs at least two categories")
    totals = unit_sums(counts_of(units, categories)).sum(axis=0)
    value = coefficients(totals, len(categories))[name][0]
    return None if np.isnan(value) else float(value)


def percent_agreement(units, categories) -> float | None:
    """Gwet's p_a: the mean share of agreeing pairs of values per unit, as a proportion."""
    return _point(units, categories, "agreement")


def krippendorff_alpha(units, categories) -> float | None:
    """Krippendorff's alpha for nominal values; None when every value is the same."""
    return _point(units, categories, "alpha")


def gwet_ac1(units, categories) -> float | None:
    """Gwet's AC1 over ``categories``, all the categories a value could have taken."""
    return _point(units, categories, "ac1")


# ------------------------------------------------------------------ with intervals
def agreement(
    counts: np.ndarray, subject_of_unit: np.ndarray, samples: int, rng: np.random.Generator
) -> Agreement:
    """The coefficients of units, with 95% percentile bootstrap intervals over subjects.

    ``counts`` has a row per unit as for :func:`unit_sums`; ``subject_of_unit`` says which
    subject each unit belongs to. The subjects with a value are resampled ``samples`` times,
    with replacement, each resample as many subjects as there are; a coefficient's interval is
    left out when it is undefined in more than half of them.
    """
    counts = np.asarray(counts, dtype=float)
    q = counts.shape[1]
    subject_of_unit = np.asarray(subject_of_unit)
    sums = unit_sums(counts)
    layout = _layout(q)

    rated = sums[:, layout["rated"]][:, 0] > 0
    subjects, which = np.unique(subject_of_unit[rated], return_inverse=True)
    per_subject = np.zeros((len(subjects), sums.shape[1]))
    np.add.at(per_subject, which, sums[rated])

    totals = per_subject.sum(axis=0)
    point = {name: values[0] for name, values in coefficients(totals, q).items()}
    pairable = sums[:, layout["pairable"]][:, 0] > 0
    marginals = totals[layout["coincidences"]].reshape(q, q).sum(axis=1)
    values = float(marginals.sum())

    intervals = _bootstrap(per_subject, q, samples, rng)
    return Agreement(
        subjects=int(len(np.unique(subject_of_unit[pairable]))),
        items=int(pairable.sum()),
        values=int(round(values)),
        shares=tuple(float(share) for share in marginals / values) if values else None,
        **{
            name: Estimate(
                None if np.isnan(point[name]) else float(point[name]),
                *(intervals[name] if not np.isnan(point[name]) else (None, None)),
            )
            for name in ("agreement", "alpha", "ac1")
        },
    )


def _bootstrap(per_subject: np.ndarray, q: int, samples: int, rng: np.random.Generator) -> dict[str, tuple]:
    """Percentile intervals of each coefficient over ``samples`` resamples of the subjects."""
    n = len(per_subject)
    empty = {name: (None, None) for name in ("agreement", "alpha", "ac1")}
    if n < 2 or samples < 1:
        return empty
    draws = rng.integers(0, n, size=(samples, n))
    # How often each subject is drawn in each resample; a resample's sums are then a product.
    times = np.zeros((samples, n))
    np.add.at(times, (np.repeat(np.arange(samples), n), draws.ravel()), 1.0)
    resampled = coefficients(times @ per_subject, q)
    tail = 100 * (1 - CONFIDENCE) / 2
    intervals = {}
    for name, values in resampled.items():
        defined = values[~np.isnan(values)]
        if len(defined) < _MIN_DEFINED_SHARE * samples:
            intervals[name] = (None, None)
            continue
        low, high = np.percentile(defined, [tail, 100 - tail])
        intervals[name] = (float(low), float(high))
    return intervals
