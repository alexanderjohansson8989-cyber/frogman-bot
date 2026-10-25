"""Explicit constant-rate assumption and Gamma-Poisson predictive reference."""
import math
from count_distribution_candidate import count_distribution


def reference(counts, *, window_seconds, target_seconds, prior_shape=.5):
    if not isinstance(counts,list) or not 1 <= len(counts) <= 20000:
        raise ValueError('Complete observed counts required')
    if any(type(x) is not int or not 0 <= x <= 1000000 for x in counts):
        raise ValueError('Counts must be nonnegative integers, no missing windows')
    for value in (window_seconds,target_seconds):
        if type(value) is not int or not 1 <= value <= 31*86400:
            raise ValueError('Exposure must be explicit positive seconds')
    if type(prior_shape) not in (int,float) or prior_shape not in (.5,1):
        raise ValueError('Only locked prior sensitivity choices supported')
    total=sum(counts)
    exposure=len(counts)*window_seconds
    a=total+prior_shape
    mean=a*target_seconds/exposure
    point_mean=total*target_seconds/exposure
    predictive=count_distribution([dict(label='constant_rate_parameter_uncertainty',
        family='negative_binomial',weight=1,mean=mean,dispersion=a)])
    point=count_distribution([dict(label='observed_rate_poisson',family='poisson',weight=1,mean=point_mean)])
    sample_mean=total/len(counts)
    sample_variance=(math.fsum((x-sample_mean)**2 for x in counts)/(len(counts)-1)
                     if len(counts)>1 else None)
    return {'version':'rate-reference-1','observed':{'windows':len(counts),'total_count':total,
        'window_seconds':window_seconds,'total_exposure_seconds':exposure,
        'rate_per_second':total/exposure,'zero_fraction':counts.count(0)/len(counts),
        'mean_per_window':sample_mean,'sample_variance':sample_variance,'maximum':max(counts)},
        'target_seconds':target_seconds,'prior':{'shape':prior_shape,'rate':0,
        'improper':True,'method_choice_not_observed_data':True},
        'posterior':{'shape':a,'rate_seconds':exposure},
        'poisson_point_reference':point,'parameter_uncertainty_reference':predictive,
        'assumptions':['constant intensity','conditionally independent Poisson counts'],
        'clustering_modelled':False,'source_coverage_verified':False,
        'historical_vintage_verified':False,'calibration_verified':False,'publication_allowed':False}
