"""Offline evidence needs and provenance-bound child links. No network or AI."""
import copy
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from bounded_evidence import digest
from independent_runner import Stop


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            for key, value in attrs:
                if key == 'href' and value:
                    self.links.append(value)


def linked_candidates(document, raw_bytes):
    if (document.get('network_media_type') != 'text/html' or
            digest(raw_bytes) != document.get('network_original_sha256')):
        raise Stop('Original HTML identity required')
    parser = Links(); parser.feed(raw_bytes.decode('utf-8-sig'))
    result = []; seen = set()
    for href in parser.links:
        url = urljoin(document['url'], href)
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.fragment or
                parsed.username or parsed.password or parsed.port not in (None, 443)):
            continue
        if url in seen or url == document['url']:
            continue
        seen.add(url)
        result.append(dict(url=url, parent_url=document['url'], href=href,
            parent_source_sha256=digest(raw_bytes), origin='captured_html_link'))
    return result


def needs_record(question, needs, documents, findings):
    """Validate exact quoted support; do not pretend semantic matches are proven.

    needs are frozen before findings, each {id, description, required}.
    Each finding identifies need, document hash, page index, offsets, and kind.
    Navigation/method alone never fills an observation need. Caller/AI still
    has to assess series, unit, geography and vintage in the quoted context.
    """
    if (not isinstance(needs, list) or not 1 <= len(needs) <= 5 or
            any(set(n) != {'id', 'description', 'required'} or
                not isinstance(n['id'], str) or not n['id'] or
                not isinstance(n['description'], str) or not n['description'].strip() or
                type(n['required']) is not bool for n in needs) or
            len({n['id'] for n in needs}) != len(needs)):
        raise Stop('Explicit distinct evidence needs required')
    ids = {n['id'] for n in needs}; checked = []; covered = set()
    if not isinstance(findings, list) or len(findings) > 15:
        raise Stop('Bounded findings required')
    for f in findings:
        if (set(f) != {'need_id', 'source_sha256', 'page_index', 'start', 'end', 'quote',
                       'kind', 'series', 'unit', 'period', 'target_relevance', 'vintage_status'} or
                f['need_id'] not in ids or f['kind'] not in
                ('observation', 'external_forecast', 'method', 'navigation') or
                f['vintage_status'] not in ('consistent', 'uncertain', 'conflicting')):
            raise Stop('Finding shape')
        for k in ('series', 'unit', 'period', 'target_relevance'):
            if not isinstance(f[k], str) or not f[k].strip():
                raise Stop('Finding context required')
        matches = [d for d in documents if d['original_sha256'] == f['source_sha256']]
        if len(matches) != 1 or any(type(f[k]) is not int for k in ('page_index', 'start', 'end')):
            raise Stop('Unique document and integer offsets required')
        d = matches[0]
        if not 0 <= f['page_index'] < len(d['pages']): raise Stop('Finding page')
        text = d['pages'][f['page_index']]['text']
        if (not 0 <= f['start'] < f['end'] <= len(text) or
                not isinstance(f['quote'], str) or len(f['quote']) < 15 or
                text[f['start']:f['end']] != f['quote']):
            raise Stop('Finding quote not at original offsets')
        checked.append(copy.deepcopy(f))
        if f['kind'] == 'observation' and f['vintage_status'] == 'consistent':
            covered.add(f['need_id'])
    missing = [n['id'] for n in needs if n['required'] and n['id'] not in covered]
    body = dict(version='research-needs-1', question_sha256=digest(question),
        documents_sha256=digest(documents), needs=copy.deepcopy(needs), findings=checked,
        required_observation_needs_missing=missing,
        semantic_review_pending=True, quote_integrity_verified=True,
        publication=False)
    return dict(body=body, sha256=digest(body))


def capture_children(candidates, selected, documents, folder, *, max_reads=2, **options):
    """Read only original HTML links; retain existing DNS/redirect/body guards."""
    from pathlib import Path
    from text_source_capture import capture
    if type(max_reads) is not int or not 0 <= max_reads <= 2:
        raise Stop('At most two additional reads')
    if not isinstance(selected, list) or len(selected) > max_reads or len(set(selected)) != len(selected):
        raise Stop('Child read limit')
    allowed = {}
    for d in documents:
        if d.get('network_media_type') == 'text/html':
            raw = (Path(folder) / d['archive_name']).read_bytes()
            for c in linked_candidates(d, raw): allowed.setdefault(c['url'], c)
    # Ignore any caller-created provenance claims; rebuild them from raw HTML.
    if any(c not in list(allowed.values()) for c in candidates):
        raise Stop('Child candidate provenance changed')
    offered = {c['url']: c for c in candidates}
    if any(url not in offered or url in {d['url'] for d in documents} for url in selected):
        raise Stop('Child was not offered or already read')
    result = dict(documents=[], failures=[], source_attempts=0)
    for url in selected:
        result['source_attempts'] += 1
        try:
            d = capture(url, folder, **options)
            d['candidate_origin'] = copy.deepcopy(offered[url])
            result['documents'].append(d)
        except Exception as exc:
            result['failures'].append(dict(url=url, error_type=type(exc).__name__))
    return result
