"""Read-only numeric diagnostics. Never adjust probabilities or reject a forecast."""
import hashlib
import json
import math
from cdf_projection_v1 import raw_mixture_cdf, masses

VERSION = 'numeric-diagnostics-v1'


def raw_quantiles(d):
    """P10/P50/P90 of independently truncated components, before API projection.

    This is a latent continuous summary even for discrete API questions.
    A bounded solver returns explicit unavailability if float precision is lost.
    """
    base = dict(version=VERSION, representation='raw_continuous_latent_before_api_projection')
    try:
        if d['kind'] != 'normal_mixture': raise ValueError('Not a mixture')
        low = -math.inf if d['lower_bound'] is None else d['lower_bound']
        high = math.inf if d['upper_bound'] is None else d['upper_bound']
        if not low < high: raise ValueError('Bounds')
        if any(v is not None and (type(v) not in (int,float) or not math.isfinite(v))
               for v in (d['lower_bound'],d['upper_bound'])): raise ValueError('Bounds')
        cs = d['components']
        if not isinstance(cs,list) or not 1 <= len(cs) <= 3: raise ValueError('Components')
        for c in cs:
            if any(type(c[k]) not in (int,float) or not math.isfinite(c[k]) for k in ('weight','mean','sd')):
                raise ValueError('Component number')
            if not 0 <= c['weight'] <= 1 or c['sd'] <= 0: raise ValueError('Component bounds')
        if abs(math.fsum(c['weight'] for c in cs)-1) > 1e-6: raise ValueError('Weight sum')
        cs = [c for c in cs if c['weight']]
        centers = [max(low,min(high,c['mean'])) for c in cs]
        span = max(c['sd'] for c in cs)
        left = low if math.isfinite(low) else min(centers)-8*span
        right = high if math.isfinite(high) else max(centers)+8*span
        if not math.isfinite(left) or not math.isfinite(right) or not left < right:
            raise ValueError('Unrepresentable bracket')
        def cdf(x):
            value = raw_mixture_cdf(d,[x])[0]
            if not math.isfinite(value) or not -1e-10 <= value <= 1+1e-10:
                raise ValueError('Unstable CDF')
            return min(1.,max(0.,value))
        fl,fr = cdf(left),cdf(right)
        if fl > .1 or fr < .9: raise ValueError('Quantiles outside bounded bracket')
        values = {}; residual = 0.
        for label,p in [('p10',.1),('p50',.5),('p90',.9)]:
            a,b = left,right
            for _ in range(160):
                mid = a/2+b/2
                fm = cdf(mid)
                if abs(fm-p) <= 1e-9: break
                if mid == a or mid == b: break
                if fm < p: a = mid
                else: b = mid
            if abs(fm-p) > 1e-7: raise ValueError('Quantile precision not reached')
            values[label] = mid; residual = max(residual,abs(fm-p))
        return dict(**base,status='available',quantiles=values,maximum_cdf_residual=residual,
                    method='bounded_bisection_log_interval_cdf')
    except (ValueError,ArithmeticError,TypeError,KeyError,IndexError):
        return dict(**base,status='unavailable',reason='invalid_or_numerically_unstable_distribution')


def projection_diagnostics(adaptation, fields):
    """Describe raw/final API buckets; no outcome score or tail broadening."""
    base = dict(version=VERSION, representation='api_grid_buckets', diagnostic_only=True)
    try:
        n = fields['inbound_outcome_count']
        grid = fields['scaling']['continuous_range']
        before = masses(adaptation['original_cdf']); after = masses(adaptation['continuous_cdf'])
        if type(n) is not int or n < 1 or len(before) != n+2 or len(after) != n+2:
            raise ValueError('Bucket count')
        floor = round(.01/n,9)
        # Same nine-decimal bucket comparison as the existing API validator.
        raw = [round(v,9) for v in before[1:-1]]
        final = [round(v,9) for v in after[1:-1]]
        count = sum(v == floor for v in final)
        return dict(**base,status='available',interior_bucket_count=n,minimum_bucket_mass=floor,
                    raw_below_floor_count=sum(v < floor for v in raw),final_at_floor_count=count,
                    final_at_floor_fraction=count/n,raw_tail_masses=dict(lower=before[0],upper=before[-1]),
                    final_tail_masses=dict(lower=after[0],upper=after[-1]),
                    lower_tail_open=fields['open_lower_bound'],upper_tail_open=fields['open_upper_bound'],
                    grid_sha256=hashlib.sha256(json.dumps(grid,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
                    pmf_l1=adaptation['pmf_l1'],maximum_cdf_change=adaptation['maximum_cdf_change'])
    except (ValueError,ArithmeticError,TypeError,KeyError,IndexError):
        return dict(**base,status='unavailable',reason='invalid_projection_diagnostic_input')
