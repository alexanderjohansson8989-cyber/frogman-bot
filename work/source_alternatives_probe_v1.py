"""One source-only replay of saved failed selection. No model/client/secrets."""
import copy,json,time,urllib.request
from pathlib import Path
from bounded_evidence import digest
from independent_runner import Stop,NoRedirect
from source_fallback_v1 import capture_with_alternatives,alternatives
from text_source_capture import capture_selected

def run(p,folder,*,open_request=None,resolver=None):
    if (p.get('version')!='source-alternatives-read-v1' or p.get('source_run_id')!=39241399017
            or p.get('publication') is not False or p['initial_capture']['documents']!=[]
            or p['initial_capture']['source_attempts']!=len(p['selected_urls'])):raise Stop('Frozen source-only replay contract')
    if not 1<=len(p['selected_urls'])<=3 or not 1<=len(p['searched_urls'])<=100:raise Stop('Source-only replay bounds')
    deadline=time.monotonic()+180;calls=[];read= open_request or urllib.request.build_opener(NoRedirect()).open
    def guard():
        if time.monotonic()>=deadline:raise Stop('Source-only probe deadline')
    def bounded(req,timeout):
        guard()
        if len(calls)>=6:raise Stop('Source-only HTTP call bound')
        calls.append(req.full_url);return read(req,timeout=min(timeout,max(1,deadline-time.monotonic())))
    first=True
    def capture(q,urls,searched,dest):
        nonlocal first
        if first:
            first=False
            if urls!=p['selected_urls']:raise Stop('Recorded selection changed')
            return copy.deepcopy(p['initial_capture'])
        kw=dict(open_request=bounded)
        if resolver is not None:kw['resolver']=resolver
        return capture_selected(q,urls,searched,dest,**kw)
    r=capture_with_alternatives({},p['selected_urls'],p['searched_urls'],p['search_urls_response'],folder,capture=capture,guard=guard)
    selected=alternatives(p['search_urls_response'],p['selected_urls'],p['searched_urls'])
    docs=[{k:d.get(k) for k in ('url','network_final_url','network_redirects','network_content_encoding','network_original_sha256',
        'network_original_bytes','network_wire_sha256','network_wire_bytes','network_media_type')} for d in r['documents']]
    return dict(version=p['version'],input_sha256=digest(p),source_run_id=p['source_run_id'],
        source_search_receipt_sha256=p['source_search_receipt_sha256'],selected_alternatives=selected,
        http_requests=calls,readable_documents=docs,failures=r['failures'][len(p['initial_capture']['failures']):],
        recorded_initial_sources_not_refetched=True,new_model_calls=0,publication=False,
        semantic_relevance_reviewed=False,raw_network_bodies_retained_in_job_only=True)

def main():
    import os
    p=json.loads(Path(os.environ['FROGMAN_SOURCE_INPUT']).read_bytes())
    if digest(p)!=os.environ['FROGMAN_SOURCE_INPUT_SHA']:raise Stop('Source-only input hash')
    result=run(p,Path(os.environ['RUNNER_TEMP'])/'source-alternatives')
    print('SOURCE_PROBE_RESULT '+json.dumps(result,separators=(',',':')))
if __name__=='__main__':main()
