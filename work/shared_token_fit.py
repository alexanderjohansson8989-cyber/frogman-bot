"""Count and shrink shared source selections BEFORE either arm is frozen."""
import copy
import json
from bounded_evidence import digest, select_excerpt
from independent_runner import Stop


def fit(question, documents, build_pair, count_tokens, *, limits=(12000, 8000, 5000, 3000),
        max_tokens=30000):
    """build_pair(list[document]) -> sealed pair with request_A/prepared_B.

    Reduction keeps original page offsets and selects the same spans for both
    arms. It never edits an already prepared pair or consumes a model response.
    The caller owns full archives and time/budget guards on count transport.
    """
    if (not isinstance(limits, (tuple, list)) or not limits or
            any(type(v) is not int or v < 1000 for v in limits) or
            any(a <= b for a, b in zip(limits, limits[1:])) or
            type(max_tokens) is not int or not 1 <= max_tokens <= 30000):
        raise Stop('Explicit decreasing shared selection policy required')
    history = []
    for limit in limits:
        chosen = copy.deepcopy(documents)
        for d in chosen:
            selected = select_excerpt(d, question, limit)
            d['frozen_shared_excerpt'] = dict(limit=limit, question_sha256=digest(question), excerpt=selected)
        prepared = build_pair(chosen)
        b = prepared['body']; requests = {'A': b['request_A']}
        if b['prepared_B'] is not None: requests['B'] = b['prepared_B']['body']['request']
        sources = [json.loads(p['messages'][0]['content'])['sources'] for p in requests.values()]
        if any(s != sources[0] for s in sources): raise Stop('Shared selected evidence differs')
        counts = {}
        for arm, request in requests.items():
            response = count_tokens({k: copy.deepcopy(v) for k, v in request.items()
                                     if k not in ('max_tokens', 'output_config', 'tools')})
            value = response.get('input_tokens')
            if type(value) is not int or value < 1: raise Stop('Invalid full token count')
            counts[arm] = value
        history.append(dict(per_source_limit=limit, counts=counts,
                            prepared_sha256=prepared['sha256']))
        if all(v <= max_tokens for v in counts.values()):
            return dict(prepared=prepared, documents=chosen, attempts=history,
                        original_documents_sha256=digest(documents),
                        frozen_documents_sha256=digest(chosen),
                        before_any_prediction=True, semantic_review_pending=True)
    raise Stop('Shared selections still exceed full-input token limit')
