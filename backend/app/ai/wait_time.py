from collections.abc import Sequence
from math import ceil


def predict_wait_time(active_orders: int, cooking_orders: int, completed_prep_minutes: Sequence[int]) -> dict[str, int | str]:
    """Estimate queue delay using recent prep times and the live kitchen queue."""
    recent = [max(1, min(int(value), 90)) for value in completed_prep_minutes[-30:]]
    if recent:
        # Newer completed orders receive more weight than older observations.
        weights = range(1, len(recent) + 1)
        average = round(sum(value * weight for value, weight in zip(recent, weights)) / sum(weights))
    else:
        average = 6

    crowd = "High" if active_orders >= 13 else "Moderate" if active_orders >= 6 else "Low"
    eta = average if active_orders == 0 else max(average, ceil(active_orders * average / (cooking_orders + 1)))
    return {
        "estimated_minutes": eta,
        "active_orders": active_orders,
        "cooking_orders": cooking_orders,
        "average_prep_minutes": average,
        "crowd_level": crowd,
        "sample_size": len(recent),
    }