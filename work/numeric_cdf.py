"""Local numeric format check, based on Metaculus's public serializer.

No transport, submission or automatic alteration of forecast beliefs.
"""
import math

SOURCE = "https://github.com/Metaculus/metaculus/blob/main/questions/serializers/common.py"


def validate_numeric_cdf(cdf, fields):
    count = fields.get("inbound_outcome_count")
    lower = fields.get("open_lower_bound")
    upper = fields.get("open_upper_bound")
    grid = fields.get("scaling", {}).get("continuous_range")
    if (type(count) is not int or count <= 0 or type(lower) is not bool or type(upper) is not bool
            or not isinstance(grid, list) or len(grid) != count + 1
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in grid)
            or any(a >= b for a, b in zip(grid, grid[1:]))):
        raise ValueError("Invalid numeric question grid or bounds")
    if not isinstance(cdf, list) or len(cdf) != count + 1:
        raise ValueError("CDF length does not match question")
    if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1 for x in cdf):
        raise ValueError("CDF contains invalid probability")
    rounded = [round(x, 10) for x in cdf]  # The public backend rounds to ten decimals.
    differences = [round(b - a, 9) for a, b in zip(rounded, rounded[1:])]
    minimum, maximum = round(0.01 / count, 9), 0.2 * 200 / count
    if any(d < minimum or d > maximum for d in differences):
        raise ValueError("CDF violates minimum bucket mass or maximum concentration")
    if (lower and rounded[0] < 0.001) or (not lower and rounded[0] != 0):
        raise ValueError("Invalid lower tail")
    if (upper and rounded[-1] > 0.999) or (not upper and rounded[-1] != 1):
        raise ValueError("Invalid upper tail")
    return rounded
