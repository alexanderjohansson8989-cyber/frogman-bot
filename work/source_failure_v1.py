"""Allowlisted source diagnostics, never arbitrary transport exception text."""
from urllib.error import HTTPError
from independent_runner import Stop

SAFE_REASONS=frozenset(('Redirect refused','Public HTTPS URL required','Non-public source address',
    'Source already captured; no silent overwrite','Unexpected redirected source',
    'Compressed body not supported','Source byte limit; no partial evidence',
    'Unsupported source encoding; original archived','Unsupported media type; original archived'))

def describe(error):
    result=dict(error_type=type(error).__name__)
    if isinstance(error,HTTPError):result['http_status']=error.code
    elif isinstance(error,Stop) and str(error) in SAFE_REASONS:result['reason']=str(error)
    return result
