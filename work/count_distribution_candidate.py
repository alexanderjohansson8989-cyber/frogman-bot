"""Deterministic unbounded count distributions; no inferred empirical parameters."""
import copy
import math

TOLERANCE = 1e-10


def number(value, low, high, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Invalid ' + name)
    return float(value)


def count_distribution(components, overflow_at=10):
    """Return exact model moments and categories 0..overflow_at-1, overflow+.

    Mixture components represent mutually exclusive scenarios, not sums of
    independent counts. Choosing/estimating parameters remains a separate task.
    """
    if type(overflow_at) is not int or not 1 <= overflow_at <= 100:
        raise ValueError('Invalid overflow threshold')
    if not isinstance(components, list) or not 1 <= len(components) <= 5:
        raise ValueError('Expected one to five components')
    validated = []
    for component in components:
        if not isinstance(component, dict):
            raise ValueError('Component object required')
        family = component.get('family')
        expected = {'label', 'family', 'weight', 'mean'}
        if family == 'negative_binomial':
            expected.add('dispersion')
        elif family != 'poisson':
            raise ValueError('Unsupported count family')
        if set(component) != expected:
            raise ValueError('Unexpected or missing component field')
        if not isinstance(component['label'], str) or not 1 <= len(component['label'].strip()) <= 200:
            raise ValueError('Explicit short scenario label required')
        weight = number(component['weight'], 0, 1, 'weight')
        if weight == 0:
            raise ValueError('Remove zero weight component')
        mean = number(component['mean'], 0, 100, 'mean')
        dispersion = (number(component['dispersion'], .01, 1000000, 'dispersion')
                      if family == 'negative_binomial' else None)
        variance = mean if dispersion is None else mean + mean * mean / dispersion
        if mean == 0:
            masses = [1.0] + [0.0] * (overflow_at - 1)
        else:
            if dispersion is None:
                masses = [math.exp(-mean + n * math.log(mean) - math.lgamma(n + 1))
                          for n in range(overflow_at)]
            else:
                # Recurrence avoids subtracting large lgamma values for large k.
                log_p = -dispersion * math.log1p(mean / dispersion)
                log_ratio = math.log(mean) - math.log(dispersion + mean)
                masses = [math.exp(log_p)]
                for n in range(1, overflow_at):
                    log_p += math.log(dispersion + n - 1) - math.log(n) + log_ratio
                    masses.append(math.exp(log_p))
        tail = 1.0 - math.fsum(masses)
        if tail < -TOLERANCE:
            raise ValueError('Probability computation unstable')
        masses.append(max(0.0, tail))
        validated.append((weight, mean, variance, masses))
    if not math.isclose(math.fsum(c[0] for c in validated), 1.0, abs_tol=TOLERANCE, rel_tol=0):
        raise ValueError('Weights must sum to one; no silent normalization')
    probabilities = [math.fsum(c[0] * c[3][i] for c in validated)
                     for i in range(overflow_at + 1)]
    mean = math.fsum(c[0] * c[1] for c in validated)
    variance = math.fsum(c[0] * (c[2] + (c[1] - mean) ** 2) for c in validated)
    minimum_mean = math.fsum(i * p for i, p in enumerate(probabilities))
    if (any(not math.isfinite(p) or not 0 <= p <= 1 + TOLERANCE for p in probabilities)
            or not math.isclose(math.fsum(probabilities), 1, abs_tol=TOLERANCE, rel_tol=0)
            or minimum_mean > mean + TOLERANCE):
        raise ValueError('Distribution invariant failed')
    return {'version': 'count-mixture-1', 'components': copy.deepcopy(components),
            'labels': list(range(overflow_at)) + [str(overflow_at) + '_plus'],
            'probabilities': probabilities, 'expected_count': mean,
            'variance': variance, 'minimum_mean_from_categories': minimum_mean,
            'numeric_tolerance': TOLERANCE, 'empirical_calibration_verified': False}
