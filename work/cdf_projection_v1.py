"""Deterministic CDF adaptation; original probabilities and distances are retained.

Project all bucket probabilities, including tails, onto a bounded simplex.
Euclidean projection uses clip(p-lambda, lower, upper). Integer rounding then
enforces an exact sum on a 1e-9 probability lattice. Final API validation is
mandatory. Original CDF, PMF L1 distance and maximum CDF change are retained.
"""
import copy,hashlib,json,math
from decimal import Decimal,ROUND_FLOOR
from numeric_cdf import validate_numeric_cdf
from general_scoring import loginterval

SCALE=1_000_000_000
VERSION='cdf-projection-v1'

def masses(cdf):
    return [cdf[0]]+[b-a for a,b in zip(cdf,cdf[1:])]+[1-cdf[-1]]

def raw_mixture_cdf(distribution,grid):
    """Same mathematical CDF as numeric_payload, before API validation."""
    lo=-math.inf if distribution['lower_bound'] is None else distribution['lower_bound']
    hi=math.inf if distribution['upper_bound'] is None else distribution['upper_bound']
    total=math.fsum(c['weight'] for c in distribution['components']);out=[]
    for x in grid:
        if x<=lo:value=0.
        elif x>=hi:value=1.
        else:value=math.fsum(c['weight']/total*math.exp(
            loginterval((lo-c['mean'])/c['sd'],(x-c['mean'])/c['sd'])-
            loginterval((lo-c['mean'])/c['sd'],(hi-c['mean'])/c['sd']))
            for c in distribution['components'] if c['weight'])
        out.append(value)
    return out

def project_cdf(original,fields):
    before=copy.deepcopy(original);n=fields.get('inbound_outcome_count')
    grid=fields.get('scaling',{}).get('continuous_range')
    lower=fields.get('open_lower_bound');upper=fields.get('open_upper_bound')
    if (type(n) is not int or not 1<=n<=2000 or type(lower) is not bool or type(upper) is not bool
        or not isinstance(grid,list) or len(grid)!=n+1
        or any(type(x) not in (int,float) or not math.isfinite(x) for x in grid)
        or any(a>=b for a,b in zip(grid,grid[1:]))):raise ValueError('Invalid grid contract')
    if (not isinstance(before,list) or len(before)!=n+1
        or any(type(x) not in (int,float) or not math.isfinite(x) or not 0<=x<=1 for x in before)
        or any(a>b for a,b in zip(before,before[1:]))):raise ValueError('Invalid original CDF')
    p=masses(before)
    try:
        cdf=validate_numeric_cdf(before,fields);strategy='already_valid_api_rounding_only'
    except ValueError:
        minimum=int(Decimal(str(round(.01/n,9)))*SCALE)
        maximum=min(SCALE,int((Decimal(40)*SCALE/Decimal(n)).to_integral_value(rounding=ROUND_FLOOR)))
        lows=[1_000_000 if lower else 0]+[minimum]*n+[1_000_000 if upper else 0]
        highs=[SCALE if lower else 0]+[maximum]*n+[SCALE if upper else 0]
        if any(l>u for l,u in zip(lows,highs)) or not sum(lows)<=SCALE<=sum(highs):
            raise ValueError('Infeasible probability constraints')
        lo=min(x-u/SCALE for x,u in zip(p,highs))
        hi=max(x-l/SCALE for x,l in zip(p,lows))
        def values(lam):return [max(l/SCALE,min(u/SCALE,x-lam)) for x,l,u in zip(p,lows,highs)]
        for _ in range(120):
            mid=(lo+hi)/2
            if math.fsum(values(mid))>1:lo=mid
            else:hi=mid
        projected=values((lo+hi)/2)
        ticks=[max(l,min(u,math.floor(x*SCALE))) for x,l,u in zip(projected,lows,highs)]
        remaining=SCALE-sum(ticks)
        if abs(remaining)>len(ticks)+2:raise ValueError('Projection numerical remainder too large')
        if remaining>0:
            order=sorted(range(len(p)),key=lambda i:(-(projected[i]*SCALE-ticks[i]),i))
            for i in order:
                if remaining and ticks[i]<highs[i]:ticks[i]+=1;remaining-=1
        elif remaining<0:
            order=sorted(range(len(p)),key=lambda i:(projected[i]*SCALE-ticks[i],i))
            for i in order:
                if remaining and ticks[i]>lows[i]:ticks[i]-=1;remaining+=1
        if remaining or sum(ticks)!=SCALE:raise ValueError('Integer simplex rounding failed')
        cumulative=ticks[0];cdf=[cumulative/SCALE]
        for value in ticks[1:-1]:cumulative+=value;cdf.append(cumulative/SCALE)
        cdf=validate_numeric_cdf(cdf,fields)
        strategy='bounded_simplex_l2_plus_1e9_integer_rounding'
    pmf=masses(cdf);l1=math.fsum(abs(a-b) for a,b in zip(p,pmf))
    assert original==before
    return dict(version=VERSION,strategy=strategy,original_cdf=before,continuous_cdf=cdf,
                pmf_l1=l1,total_variation=l1/2,cdf_l1=math.fsum(abs(a-b) for a,b in zip(before,cdf)),
                maximum_cdf_change=max(abs(a-b) for a,b in zip(before,cdf)),
                squared_pmf_change=math.fsum((a-b)**2 for a,b in zip(p,pmf)),
                original_sha256=hashlib.sha256(json.dumps(before,separators=(',',':'),allow_nan=False).encode()).hexdigest())
