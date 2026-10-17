"""Separate eight-question manifest, frozen pair routing and outcome assembly.

No API, search or submission. Caller owns live preflight, cost/time guards and
source archives. Signature checks bind inputs, not semantic eligibility or truth.
"""
import copy
import datetime as dt
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from bounded_evidence import digest, forecast_payload, text_answer_check
from data_forecast_candidate import count_payload, resolve_count_reply
from reference_bridge import prepare as ref_prepare, propose as ref_propose, select as ref_select
from general_scoring import score, difference, serializable

POLICY_SHA = 'cb87aa93ccc6b4cd5fc60f9a5f1c962043cac7ccd1a0594f32c0b2a715356649'
STATUSES = {'forecast','abstain','format_error','transport_error','budget_stop','time_stop','not_started'}


def verify_design(record, document_bytes):
    if record.get('sha256')!=POLICY_SHA or digest(record['policy'])!=POLICY_SHA:
        raise ValueError('Design policy changed')
    if digest(document_bytes)!=record['policy']['design_document_sha256']:
        raise ValueError('Design document changed')
    return True


def instant(value):
    if not isinstance(value,str): raise ValueError('Timestamp string required')
    t=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.tzinfo is None: raise ValueError('Timestamp zone required')
    return t.astimezone(dt.timezone.utc)


def hash_string(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('SHA256 required')


def seal(body):
    return dict(body=copy.deepcopy(body),sha256=digest(body))


def read(record):
    if set(record)!= {'body','sha256'} or digest(record['body'])!=record['sha256']:
        raise ValueError('Evaluation record changed')
    return copy.deepcopy(record['body'])


def validate_manifest(pack, now):
    if set(pack)!={'version','policy_sha256','publication','questions','window','code_hashes','excluded_target_keys','budget_plan'}:
        raise ValueError('Manifest fields')
    if pack['version']!='general-evaluation-1' or pack['policy_sha256']!=POLICY_SHA or pack['publication'] is not False:
        raise ValueError('Locked design/development scope')
    if pack['budget_plan']!='outputs/BUDGET.md': raise ValueError('Budget source')
    if set(pack['window'])!={'opens_utc','closes_utc'}: raise ValueError('Window fields')
    opening,closing=map(instant,(pack['window']['opens_utc'],pack['window']['closes_utc']))
    if now.tzinfo is None or not now<opening<closing: raise ValueError('Lock before source/forecast window')
    if not isinstance(pack['code_hashes'],dict) or not pack['code_hashes']: raise ValueError('Code lock required')
    for name,sha in pack['code_hashes'].items():
        if not isinstance(name,str) or not name.strip(): raise ValueError('Code identity')
        hash_string(sha)
    excluded=pack['excluded_target_keys']
    if not isinstance(excluded,list) or any(not isinstance(x,str) or not x for x in excluded) or len(set(excluded))!=len(excluded):
        raise ValueError('Explicit prior target exclusions required')
    questions=pack['questions']
    if not isinstance(questions,list) or len(questions)!=8: raise ValueError('Eight questions required')
    ids=set();targets=set();types=Counter();domains=set();clusters=set()
    for q in questions:
        common={'id','question','criteria','type','domain','cluster','target_key','outcome_not_before_utc','resolution_source','scoring','resolution'}
        extra={'binary':set(),'categorical':{'choices'},'discrete':{'choices'},'numeric':{'lower_bound','upper_bound','unit'}}.get(q.get('type'))
        if extra is None or set(q)!=common|extra: raise ValueError('Question shape; no hidden factual context')
        for key in ('id','question','criteria','domain','cluster','target_key','resolution_source'):
            if not isinstance(q[key],str) or not q[key].strip(): raise ValueError('Question identity required')
        if q['id'] in ids or q['target_key'] in targets or q['target_key'] in excluded: raise ValueError('Duplicate or previously used target')
        if not q['resolution_source'].startswith('https://'): raise ValueError('Resolution URL')
        if instant(q['outcome_not_before_utc'])<=closing: raise ValueError('Outcome must remain future')
        r=q['resolution']
        if set(r)!={'vintage','release_expected_utc','deadline_utc','boundary_rule'} or r['vintage'] not in ('first_release','specified_revision','catalog_snapshot'):
            raise ValueError('Explicit outcome vintage required')
        if not isinstance(r['boundary_rule'],str) or not r['boundary_rule'].strip(): raise ValueError('Boundary rule')
        if not instant(q['outcome_not_before_utc'])<=instant(r['release_expected_utc'])<=instant(r['deadline_utc']): raise ValueError('Outcome collection dates')
        c=q['scoring']
        if q['type']=='numeric':
            for k in ('lower_bound','upper_bound'):
                x=q[k]
                if x is not None and (type(x) not in (int,float) or not math.isfinite(x)): raise ValueError('Finite numeric bounds')
            if q['lower_bound'] is not None and q['upper_bound'] is not None and q['lower_bound']>=q['upper_bound']: raise ValueError('Numeric support')
            if not isinstance(q['unit'],str) or not q['unit'].strip(): raise ValueError('Numeric unit')
            if c!={'kind':'continuous_density'}:
                if (set(c)!={'kind','step','interval'} or c['kind']!='rounded_mass' or c['interval']!='half_open_centered'
                    or type(c['step']) not in (int,float) or not math.isfinite(c['step']) or c['step']<=0): raise ValueError('Numeric score contract')
        else:
            if c!={'kind':'category_mass'}: raise ValueError('Category score contract')
            if q['type']!='binary' and (not isinstance(q['choices'],list) or len(q['choices'])<2 or
                any(not isinstance(x,str) or not x for x in q['choices']) or len(set(q['choices']))!=len(q['choices'])): raise ValueError('Choice labels/order')
        ids.add(q['id']);targets.add(q['target_key']);types[q['type']]+=1;domains.add(q['domain']);clusters.add(q['cluster'])
    if types!=Counter(dict(binary=2,categorical=2,discrete=2,numeric=2)) or len(domains)<4 or len(clusters)<6: raise ValueError('Locked type/domain/cluster coverage')
    analysis=max(instant(q['resolution']['release_expected_utc']) for q in questions)+dt.timedelta(days=30)
    if any(instant(q['resolution']['deadline_utc'])>analysis for q in questions): raise ValueError('Outcome deadline after initial analysis')
    return dict(analysis_deadline_utc=analysis.isoformat(), novelty_semantic_review_pending=True)


def lock_manifest(pack, path, now):
    checked=validate_manifest(pack,now)
    record=seal(dict(manifest=pack,locked_at_utc=now.isoformat(),checks=checked,trusted_external_timestamp=False))
    with Path(path).open('x',encoding='utf-8') as f: json.dump(record,f,ensure_ascii=False,allow_nan=False)
    return record


def planned_cells(locked):
    body=read(locked);pack=body['manifest']
    validate_manifest(pack,instant(body['locked_at_utc']))
    return [dict(question_id=q['id'],arm=arm,status='not_started',cluster=q['cluster'])
            for i,q in enumerate(pack['questions']) for arm in (('A','B') if i%2==0 else ('B','A'))]


def prepare_pair(question, documents, research_time, *, reference_package=None, count_target=None):
    """Build both blind requests before either reply, or explicit shared-A bypass."""
    instant(research_time)
    a=(count_payload(question,documents,research_time,count_target) if count_target
       else forecast_payload(question,documents,research_time,12000))
    prepared_b=None;route='bypass_shared_A';reason='No supported eligible reference'
    if reference_package is not None:
        if set(reference_package)!={'answer','method','eligibility','reference_document_indices','receipt'}: raise ValueError('Reference package fields')
        receipt=reference_package['receipt']
        expected={'question_sha256','documents_sha256','method_sha256','answer_sha256','review_id','checks'}
        if set(receipt)!=expected or receipt['question_sha256']!=digest(question) or receipt['documents_sha256']!=digest(documents) or receipt['method_sha256']!=digest(reference_package['method']) or receipt['answer_sha256']!=digest(reference_package['answer']) or receipt['review_id']!=reference_package['eligibility']['review_id']:
            raise ValueError('Reference eligibility binding')
        checks=receipt['checks']
        if set(checks)!={'target_identity','source_windows','method_reproducible','limitations_explicit'} or any(type(v) is not bool for v in checks.values()): raise ValueError('Reference eligibility checks')
        status=reference_package['eligibility']['status']
        if status not in ('eligible','unverified','invalid'): raise ValueError('Reference eligibility status')
        if status=='eligible':
            if not all(checks.values()): raise ValueError('Eligible reference failed explicit checks')
            indices=reference_package['reference_document_indices']
            if (not isinstance(indices,list) or not indices or any(type(i) is not int or not 0<=i<len(documents) for i in indices) or len(set(indices))!=len(indices)): raise ValueError('Reference source partition')
            # Preserve order in A and B. The reference bridge joins prior then new.
            if indices!=list(range(len(indices))): raise ValueError('Reference documents must be frozen prefix')
            prepared_b=ref_prepare(question,reference_package['answer'],documents[:len(indices)],documents[len(indices):],research_time,
                method=reference_package['method'],eligibility=reference_package['eligibility'],count_target=count_target)
            source_a=json.loads(a['messages'][0]['content'])['sources']
            source_b=json.loads(prepared_b['body']['request']['messages'][0]['content'])['sources']
            if source_a!=source_b: raise ValueError('A/B evidence differs')
            route='reference_audit';reason='Pre-generative bound eligibility receipt'
        else: reason='Reference '+status
    return seal(dict(question=question,request_A=a,prepared_B=prepared_b,route=route,reason=reason,
        count_target=count_target,documents_sha256=digest(documents),research_time_utc=research_time,publication=False))


def resolve_a(prepared, reply, actual_request):
    p=read(prepared)
    if p['request_A']!=actual_request: raise ValueError('Actual A request changed')
    answer=(resolve_count_reply(reply,p['question'],actual_request,p['count_target'])['answer'] if p['count_target']
            else text_answer_check(reply,p['question'],actual_request))
    return dict(status=answer['decision'],answer=answer,raw_response_sha256=digest(reply))


def bypass_b(prepared, result_a):
    p=read(prepared)
    if p['route']!='bypass_shared_A': raise ValueError('Eligible route cannot bypass after seeing answers')
    if result_a['status'] not in STATUSES: raise ValueError('A status')
    return dict(**copy.deepcopy(result_a),route='bypass_shared_A',copied_A_sha256=digest(result_a),additional_generations=0)


def proposal_b(prepared, reply, actual_request):
    p=read(prepared)
    if p['route']!='reference_audit': raise ValueError('No B generation on bypass')
    return ref_propose(p['prepared_B'],reply,actual_request)


def finish_b(prepared, proposal, audit_reply=None, actual_audit_request=None, *, late_reference_invalid=False):
    p=read(prepared)
    if p['prepared_B'] is None or proposal['body']['prepared_sha256']!=p['prepared_B']['sha256']: raise ValueError('B proposal identity')
    selected=ref_select(proposal,audit_reply,actual_audit_request)
    if type(late_reference_invalid) is not bool: raise ValueError('Late invalidity flag')
    body=selected['body'];answer=body['selected_answer']
    return dict(status='abstain' if late_reference_invalid or answer is None else 'forecast',
        answer=None if late_reference_invalid else answer,route='reference_audit',selection_record=selected,
        late_reference_invalid=late_reference_invalid,shared_source_review_A_required=late_reference_invalid)


def extended_mean(values):
    if not values or any(x is None for x in values): return None
    if math.inf in values and -math.inf in values: return None
    if math.inf in values: return math.inf
    if -math.inf in values: return -math.inf
    return math.fsum(values)/len(values)


def evaluate(locked, cells, outcomes, *, material_errors, as_of):
    planned=planned_cells(locked);pack=read(locked)['manifest']
    if type(material_errors) is not bool: raise ValueError('Explicit material-error assessment required')
    now=instant(as_of)
    expected={(c['question_id'],c['arm']) for c in planned};seen={}
    for c in cells:
        key=(c['question_id'],c['arm'])
        if key not in expected or key in seen or c['status'] not in STATUSES: raise ValueError('Unknown/duplicate cell or status')
        if c['status']=='forecast' and (not isinstance(c.get('answer'),dict) or c['answer'].get('question_id')!=c['question_id'] or c['answer'].get('decision')!='forecast'): raise ValueError('Forecast cell identity')
        seen[key]=c
    questions={q['id']:q for q in pack['questions']}
    if not set(outcomes)<=set(questions): raise ValueError('Unknown outcome')
    for identity,o in outcomes.items():
        q=questions[identity]
        if set(o)!={'observed','source_sha256','captured_utc','vintage'}: raise ValueError('Outcome receipt fields')
        hash_string(o['source_sha256'])
        lower = (q['resolution']['release_expected_utc'] if o['vintage']=='catalog_snapshot'
                 else q['outcome_not_before_utc'])
        captured=instant(o['captured_utc'])
        if (o['vintage']!=q['resolution']['vintage'] or
            not instant(lower)<=captured<=now or
            captured>=instant(q['resolution']['deadline_utc'])): raise ValueError('Outcome vintage/time')
    rows=[];coverage={a:Counter() for a in ('A','B')};all_scoreable=True
    for q in pack['questions']:
        pair={a:seen.get((q['id'],a),dict(status='not_started')) for a in ('A','B')}
        for a,c in pair.items(): coverage[a][c['status']]+=1
        b=pair['B'];route=b.get('route','not_recorded')
        if b.get('late_reference_invalid') and not material_errors: raise ValueError('Late invalidity requires material-error assessment')
        if route=='bypass_shared_A':
            copied={k:v for k,v in b.items() if k not in ('route','copied_A_sha256','additional_generations','arm','question_id')}
            original={k:v for k,v in pair['A'].items() if k not in ('arm','question_id')}
            if copied!=original or b.get('copied_A_sha256')!=digest(original) or b.get('additional_generations')!=0: raise ValueError('Bypass must exactly copy A')
        if route not in ('bypass_shared_A','reference_audit','not_recorded'): raise ValueError('Unknown route')
        losses={};error=None
        if q['id'] in outcomes:
            for a,c in pair.items():
                if c['status']=='forecast':
                    try: losses[a]=score(q,c['answer']['distribution'],outcomes[q['id']]['observed'],q['scoring'])
                    except ValueError as exc: error=str(exc)
        paired=len(losses)==2 and error is None
        delta=difference(losses['A']['nll'],losses['B']['nll']) if paired else None
        if not paired or delta is None or route=='not_recorded': all_scoreable=False
        rows.append(dict(question_id=q['id'],domain=q['domain'],type=q['type'],cluster=q['cluster'],
            route=route,statuses={a:c['status'] for a,c in pair.items()},outcome_available=q['id'] in outcomes,
            losses=losses,paired=paired,delta=delta,scoring_error=error))
    def summary(subset):
        groups=defaultdict(list)
        for row in subset: groups[row['cluster']].append(row['delta'])
        cluster={k:extended_mean(v) for k,v in groups.items()}
        return dict(question_count=len(subset),cluster_means=cluster,mean_delta=extended_mean(list(cluster.values())))
    successful=[r for r in rows if r['paired']]
    overall=summary(successful);eligible=summary([r for r in successful if r['route']=='reference_audit'])
    gates=dict(all_planned_targets_resolved_and_scoreable=all_scoreable,
        B_coverage_not_below_A=coverage['B']['forecast']>=coverage['A']['forecast'],
        eligible_pairs_in_at_least_two_event_clusters=len(eligible['cluster_means'])>=2,
        no_unresolved_material_factual_target_or_leakage_errors=not material_errors,
        negative_cluster_weighted_mean_delta_overall=overall['mean_delta'] is not None and overall['mean_delta']<0,
        negative_cluster_weighted_mean_delta_on_eligible_pairs=eligible['mean_delta'] is not None and eligible['mean_delta']<0)
    return serializable(dict(version='general-evaluation-result-1',manifest_sha256=locked['sha256'],
        rows=rows,coverage={a:dict(v) for a,v in coverage.items()},overall=overall,eligible=eligible,
        by_domain={d:summary([r for r in successful if r['domain']==d]) for d in {r['domain'] for r in rows}},
        by_type={t:summary([r for r in successful if r['type']==t]) for t in {r['type'] for r in rows}},
        gates=gates,decision='provisional_continuation_signal' if all(gates.values()) else 'insufficient_or_mixed',
        quality_winner=None,complete_case_bias_warning=True,as_of_utc=as_of,publication=False))
