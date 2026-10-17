"""Offline full-distribution losses. Exact zeros remain infinity, not a floor.

Normal tails use log-space Mills' continued fraction to avoid erfc underflow.
Component weights are normalized only within the existing 1e-6 schema tolerance;
the adjustment is reported. Original forecast objects are never rewritten.
"""
import math
from decimal import Decimal
from autonomous_runner import distribution_check, finite

LOG_ROOT_2PI = .5*math.log(2*math.pi)


class NumericalError(ValueError):
    pass


def logsum(values):
    top = max(values)
    if top == -math.inf: return top
    if not math.isfinite(top): raise NumericalError('Nonfinite log mass')
    return top + math.log(math.fsum(math.exp(x-top) for x in values))


def logcdf(x):
    if x == -math.inf: return -math.inf
    if x == math.inf: return 0.
    if not math.isfinite(x): raise NumericalError('Nonfinite standardized value')
    if x < -10:
        z = -x
        denominator = z
        for n in range(100, 0, -1): denominator = z + n/denominator
        result = -.5*z*z - LOG_ROOT_2PI - math.log(denominator)
        if not math.isfinite(result): raise NumericalError('Normal log-tail overflow')
        return result
    if x <= 0: return math.log(.5*math.erfc(-x/math.sqrt(2)))
    return math.log1p(-math.exp(logcdf(-x)))


def logdiff(a, b):
    if b == -math.inf: return a
    if not a > b: raise NumericalError('Interval lost to finite precision')
    return a + math.log(-math.expm1(b-a))


def loginterval(lo, hi):
    if lo >= hi: return -math.inf
    if lo >= 0: return logdiff(logcdf(-lo), logcdf(-hi))
    return logdiff(logcdf(hi), logcdf(lo))


def numeric_log_value(distribution, observed, *, rounding_step=None):
    """Truncated mixture: each component normalized over its own shared support."""
    y = finite(observed)
    lower = distribution['lower_bound']
    upper = distribution['upper_bound']
    lower = -math.inf if lower is None else lower
    upper = math.inf if upper is None else upper
    lo = hi = None
    if rounding_step is not None:
        step = finite(rounding_step)
        if step <= 0: raise ValueError('Positive rounding step required')
        units = Decimal(str(y))/Decimal(str(step))
        if units != units.to_integral_value(): raise ValueError('Outcome not on locked rounding grid')
        if y-step/2 == y+step/2: raise NumericalError('Rounding interval below floating precision')
        lo, hi = max(lower, y-step/2), min(upper, y+step/2)
        if lo >= hi: return -math.inf
    elif not lower <= y <= upper:
        return -math.inf
    total = math.fsum(c['weight'] for c in distribution['components'])
    values = []
    for c in distribution['components']:
        if c['weight'] == 0: continue
        mu, sd = c['mean'], c['sd']
        normalizer = loginterval((lower-mu)/sd, (upper-mu)/sd)
        if not math.isfinite(normalizer): raise NumericalError('Degenerate truncation')
        if rounding_step is not None:
            standardized_lo, standardized_hi = (lo-mu)/sd, (hi-mu)/sd
            if standardized_lo>=standardized_hi:
                raise NumericalError('Standardized interval lost to finite precision')
            value = loginterval(standardized_lo, standardized_hi)
        else:
            z = (y-mu)/sd
            if not math.isfinite(z) or abs(z)>1e154: raise NumericalError('Density exponent overflow')
            value = -.5*z*z - math.log(sd) - LOG_ROOT_2PI
        if value != -math.inf and not math.isfinite(value): raise NumericalError('Density overflow')
        # Outside support has already been handled. Infinite density exponent is
        # numerical overflow, not evidence of an exact zero-density forecast.
        if value == -math.inf and rounding_step is None: raise NumericalError('Density exponent overflow')
        values.append(math.log(c['weight']/total)+value-normalizer)
    result = logsum(values)
    if rounding_step is not None and result > 1e-10: raise NumericalError('Interval probability exceeds one')
    return min(0.,result) if rounding_step is not None else result


def score(question, distribution, observed, contract):
    distribution_check(distribution, question)
    if question['type'] == 'numeric':
        if contract == {'kind':'continuous_density'}:
            log_value = numeric_log_value(distribution, observed)
        elif (set(contract) == {'kind','step','interval'} and contract['kind'] == 'rounded_mass'
              and contract['interval'] == 'half_open_centered'):
            log_value = numeric_log_value(distribution, observed, rounding_step=contract['step'])
        else: raise ValueError('Explicit numeric scoring contract required')
        return dict(nll=-log_value, secondary=None,
            weight_sum_before_normalization=math.fsum(c['weight'] for c in distribution['components']))
    if contract != {'kind':'category_mass'}: raise ValueError('Category scoring contract required')
    if question['type'] == 'binary':
        if type(observed) not in (bool,int) or observed not in (0,1): raise ValueError('Binary outcome required')
        p = distribution['probability']
        mass = p if observed else 1-p
        secondary = dict(kind='brier', value=(p-int(observed))**2)
        total = 1.
    else:
        choices = question['choices']
        if observed not in choices: raise ValueError('Unknown outcome category')
        total = math.fsum(distribution['probabilities'].values())
        probabilities = [distribution['probabilities'][x]/total for x in choices]
        index = choices.index(observed)
        mass = probabilities[index]
        if question['type'] == 'discrete':
            cumulative = 0.; terms = []
            for j,p in enumerate(probabilities[:-1]):
                cumulative += p
                terms.append((cumulative-int(index<=j))**2)
            secondary = dict(kind='normalized_rps', value=math.fsum(terms)/(len(choices)-1))
        else:
            secondary = dict(kind='half_multiclass_brier', value=.5*math.fsum(
                (p-int(index==j))**2 for j,p in enumerate(probabilities)))
    return dict(nll=-math.log(mass) if mass else math.inf, secondary=secondary,
                weight_sum_before_normalization=total)


def difference(a, b):
    """B minus A, preserving undefined double-infinity explicitly."""
    if math.isnan(a) or math.isnan(b): raise NumericalError('NaN score')
    return None if a == b == math.inf else b-a


def serializable(value):
    if isinstance(value,float) and not math.isfinite(value):
        if math.isnan(value): raise NumericalError('NaN cannot be archived')
        return 'positive_infinity' if value>0 else 'negative_infinity'
    if isinstance(value,dict): return {k:serializable(v) for k,v in value.items()}
    if isinstance(value,list): return [serializable(v) for v in value]
    return value
