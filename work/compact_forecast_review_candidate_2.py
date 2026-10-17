"""Versioned prompt correction; schema and host checks identical to candidate1."""
from compact_forecast_review_candidate import SYSTEM as BASE, payload as old_payload, check

VERSION='compact-review-2'
SYSTEM=BASE+'''
Additional classification rules:
supported=true means every material part of a fact is supported, including
period, units, vintage and the source's status category. A matching number does
not verify a different category. If one material qualification is wrong or
unsupported, mark that fact false and briefly explain the qualification in
unsupported; do not imply the entire numerical observation was fabricated.
The subjective list contains legitimate predictive choices only. Never put a
correction to a source label or a factual discrepancy in subjective.
Classify the actual claim, not its section heading or a 'judgement' prefix.
Do not call a number measured or historical unless the answer claims that.
If an essential parameter's wording is genuinely ambiguous between a chosen
future uncertainty and an empirical description, identify the ambiguity in
unsupported rather than inventing a measured claim. Explicit future predictive
parameters may be judgemental without historical calibration. Claims of typical
historical movements, empirical rates, method properties or calculated sample
statistics still require evidence and correct calculations. Use all components
when judging mixture dispersion; descriptive variability and forecast error
are different quantities. Missing future observations alone are not a defect.
'''

def payload(question,answer,documents):
    p=old_payload(question,answer,documents);p['system']=SYSTEM;return p
