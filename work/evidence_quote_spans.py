"""Whitespace-tolerant, position-preserving matching within SENT excerpts only.

This checks text identity, never the truth or semantic relevance of a quote.
No case folding, punctuation repair, number changes, or boundary joining.
"""
import re
from independent_runner import Stop
BOUNDARY='[EXCERPT BOUNDARY]'

def _fold(text):
    chars=[];starts=[];ends=[]
    for m in re.finditer(r'\s+|\S',text):
        chars.append(' ' if m[0].isspace() else m[0])
        starts.append(m.start());ends.append(m.end())
    return ''.join(chars),starts,ends

def locate(text,quote,*,limit=256):
    if not isinstance(text,str) or not isinstance(quote,str) or len(quote.strip())<15:
        raise Stop('Quote shape')
    if BOUNDARY in quote:raise Stop('Quote crosses excerpt boundary')
    if type(limit) is not int or not 1<=limit<=256:raise Stop('Quote position limit')
    needle=_fold(quote.strip())[0];positions=[];base=0
    for segment in text.split(BOUNDARY):
        normalized,starts,ends=_fold(segment)
        at=normalized.find(needle)
        while at>=0:
            if len(positions)>=limit:raise Stop('Quote position limit; use more context')
            lo=base+starts[at];hi=base+ends[at+len(needle)-1]
            original=text[lo:hi]
            positions.append(dict(start=lo,end=hi,original_quote=original,
                                  match='exact' if original==quote else 'whitespace_only'))
            at=normalized.find(needle,at+1)
        base+=len(segment)+len(BOUNDARY)
    return positions

def bind(documents,url,quote):
    positions=[]
    for i,d in enumerate(documents):
        if d['url']!=url:continue
        for p in locate(d['text'],quote):
            if len(positions)>=256:raise Stop('Quote position limit; use more context')
            positions.append(dict(document_index=i,url=url,**p))
    if not positions:raise Stop('Quote absent from actual input')
    return positions
