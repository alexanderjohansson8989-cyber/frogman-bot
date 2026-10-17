"""Reproducible descriptive counts, not a selected predictive distribution."""
import copy
import statistics
from bounded_evidence import digest


def count_summary(counts, *, selection, source_sha256, thresholds=(7, 10)):
    if (not isinstance(counts, list) or len(counts) < 2 or
            any(type(x) is not int or x < 0 for x in counts)):
        raise ValueError('At least two complete nonnegative integer windows required')
    if (not isinstance(selection, dict) or not selection or
            not isinstance(source_sha256, str) or len(source_sha256) != 64 or
            any(c not in '0123456789abcdef' for c in source_sha256)):
        raise ValueError('Explicit selection and source hash required')
    if (not isinstance(thresholds, (tuple, list)) or
            any(type(x) is not int or x < 0 for x in thresholds) or
            len(set(thresholds)) != len(thresholds)):
        raise ValueError('Distinct nonnegative thresholds required')
    inputs = dict(counts=copy.deepcopy(counts), selection=copy.deepcopy(selection),
                  source_sha256=source_sha256, thresholds=list(thresholds))
    result = dict(n=len(counts), total=sum(counts), mean=statistics.mean(counts),
                  sample_variance=statistics.variance(counts),
                  zero_windows=counts.count(0),
                  at_least={str(t): sum(v >= t for v in counts) for t in thresholds})
    body = dict(version='descriptive-counts-1', inputs_sha256=digest(inputs),
                inputs=inputs, result=result, establishes_predictive_superiority=False)
    return dict(body=body, sha256=digest(body))


def verify(receipt):
    if not isinstance(receipt, dict) or set(receipt) != {'body', 'sha256'}:
        raise ValueError('Computation receipt required')
    b = receipt['body']
    if digest(b) != receipt['sha256']:
        raise ValueError('Computation receipt changed')
    i = b['inputs']
    rebuilt = count_summary(i['counts'], selection=i['selection'],
                            source_sha256=i['source_sha256'], thresholds=i['thresholds'])
    if rebuilt != receipt:
        raise ValueError('Computation does not reproduce')
    return copy.deepcopy(b)
