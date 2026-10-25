"""Bounded short domain vocabulary; whole tokens, never substring matches."""
import re

SHORT=set('ai un eu uk us gdp cpi pce fed ecb imf who oil gas war ssn kp m2 co2'.split())

def words(text):
    return {t for t in re.findall(r'\b[a-z0-9]+\b',text.lower())
            if len(t)>=4 or t in SHORT}
