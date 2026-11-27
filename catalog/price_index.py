"""
Jevons price index over matched (product, store) pairs.

Pure module: no database, no Flask and no model imports. Observations are
dicts with ``product_id``, ``store_id`` and ``price``.

Only pairs present in both periods are compared, so products or stores
entering or leaving the basket do not move the index.
"""

import math
import statistics
from collections import defaultdict

RATIO_MIN = 0.2
RATIO_MAX = 5.0
MIN_SAMPLE = 10


def _pair_medians(observations):
    """Return {(product_id, store_id): median price}, ignoring non-positive prices."""
    grouped = defaultdict(list)
    for obs in observations:
        price = obs.get("price")
        if price is None or price <= 0:
            continue
        grouped[(obs["product_id"], obs["store_id"])].append(float(price))
    return {key: statistics.median(prices) for key, prices in grouped.items()}


def matched_pairs(base, current):
    """Match base and current observations by (product_id, store_id).

    Within each period the price of a pair is the median of its observations.
    Only pairs present in both periods, with a price > 0 in both, are returned
    as dicts with ``product_id``, ``store_id``, ``base_price`` and
    ``current_price``.
    """
    base_medians = _pair_medians(base)
    current_medians = _pair_medians(current)
    return [
        {
            "product_id": product_id,
            "store_id": store_id,
            "base_price": base_medians[(product_id, store_id)],
            "current_price": current_medians[(product_id, store_id)],
        }
        for (product_id, store_id) in base_medians
        if (product_id, store_id) in current_medians
    ]


def jevons_index(pairs, base_value=100.0, min_sample=MIN_SAMPLE):
    """Compute a Jevons index: base_value times the geometric mean of current/base ratios.

    Ratios outside [RATIO_MIN, RATIO_MAX] are dropped as outliers and counted
    in ``outliers_removed``. Pairs with a non-positive price are ignored.
    With fewer than ``min_sample`` remaining pairs, ``index`` and
    ``change_pct`` are None and ``warning`` explains why.

    Returns a dict with keys: index, change_pct, sample_size, products,
    stores, outliers_removed and, when the sample is too small, warning.
    """
    ratios = []
    products = set()
    stores = set()
    outliers_removed = 0

    for pair in pairs:
        base_price = pair["base_price"]
        current_price = pair["current_price"]
        if base_price <= 0 or current_price <= 0:
            continue
        ratio = current_price / base_price
        if not (RATIO_MIN <= ratio <= RATIO_MAX):
            outliers_removed += 1
            continue
        ratios.append(ratio)
        products.add(pair["product_id"])
        stores.add(pair["store_id"])

    sample_size = len(ratios)
    result = {
        "index": None,
        "change_pct": None,
        "sample_size": sample_size,
        "products": len(products),
        "stores": len(stores),
        "outliers_removed": outliers_removed,
    }

    if sample_size == 0 or sample_size < min_sample:
        result["warning"] = (
            f"only {sample_size} matched pairs after outlier cut "
            f"(minimum {min_sample}); index not published"
        )
        return result

    geo_mean = math.exp(sum(math.log(r) for r in ratios) / sample_size)
    result["index"] = base_value * geo_mean
    result["change_pct"] = (geo_mean - 1.0) * 100.0
    return result


def chain_index(series, base_value=100.0, min_sample=MIN_SAMPLE):
    """Chain Jevons indexes period by period.

    ``series`` is an ordered list of periods, each a list of observations.
    The first period is the base and gets ``base_value`` with sample_size 0.
    Each later period is linked to the previous one through jevons_index on
    matched pairs, and the chained index is previous * link ratio.

    If a link has too few pairs, its index and every later index are None,
    because the chain cannot continue past it.

    Returns a list of dicts with keys: period (position in ``series``),
    index and sample_size.
    """
    if not series:
        return []

    rows = [{"period": 0, "index": base_value, "sample_size": 0}]
    chained = base_value
    for position in range(1, len(series)):
        pairs = matched_pairs(series[position - 1], series[position])
        link = jevons_index(pairs, base_value=1.0, min_sample=min_sample)
        if chained is not None and link["index"] is not None:
            chained = chained * link["index"]
        else:
            chained = None
        rows.append({
            "period": position,
            "index": chained,
            "sample_size": link["sample_size"],
        })
    return rows
