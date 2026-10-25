"""Persisted forecast+comment intent, with no automatic reuse of effect keys.

Recovery only reads possible effects. This separate opt-in continuation may
send a never-started frozen plan under the ordinary permit-before-HTTP path.
Cancelled attempts are terminal in v1; their keys/permits are never reset.
"""
import json
from bounded_evidence import digest
from competition_adapter_v1 import check_plan,submit_once
from candidate_dispatch_v1 import forecast_transport
from candidate_comments_v1 import submit as submit_comment
from general_evaluation import instant
from independent_runner import Stop
from operations_ledger_v1 import GlobalStop

VERSION='publication-intent-v1'
TERMINAL=('complete','not_submitted','forecast_only')


def key_for(plan):
    b=plan['binding']
    return f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"


def register(ledger,key,plan):
    """Only inside finish_session's transaction, before complete is committed."""
    check_plan(plan)
    if not ledger.db.in_transaction or key!=key_for(plan):
        raise GlobalStop('Atomic publication identity required')
    if plan['binding'].get('mode')=='test_area_diagnostic':
        raise GlobalStop('Diagnostic plan cannot acquire publication intent')
    row=ledger.db.execute('SELECT contract_sha,status FROM sessions WHERE key=?',(key,)).fetchone()
    if row!=(digest(plan['binding']),'active'):
        raise GlobalStop('Publication must match the active session')
    ledger.db.execute('INSERT INTO publications(key,version,plan_sha,plan,state) VALUES(?,?,?,?,?)',
        (key,VERSION,digest(plan),json.dumps(plan,ensure_ascii=False,allow_nan=False),'pending'))


def pending(ledger):
    return [r[0] for r in ledger.db.execute("SELECT key FROM publications WHERE state='pending' ORDER BY key")]


def load(ledger,key):
    row=ledger.db.execute('SELECT version,plan_sha,plan,state FROM publications WHERE key=?',(key,)).fetchone()
    if not row or row[0]!=VERSION or row[3]!='pending':raise GlobalStop('Pending publication required')
    try:
        plan=json.loads(row[2]);check_plan(plan)
        valid=digest(plan)==row[1] and key_for(plan)==key
    except (Stop,ValueError,TypeError,KeyError):valid=False
    if not valid:raise GlobalStop('Frozen publication changed')
    session=ledger.db.execute('SELECT contract_sha,status FROM sessions WHERE key=?',(key,)).fetchone()
    if session!=(digest(plan['binding']),'complete'):raise GlobalStop('Publication session changed')
    return plan


def effects(ledger,key,plan):
    """Validate both linked physical rows before deciding what may be done."""
    states=[]
    for k in (key,key+'/comment'):
        row=ledger.db.execute('SELECT plan_sha,plan,state,receipt FROM submissions WHERE key=?',(k,)).fetchone()
        if row is None:states.append(None);continue
        try:
            saved=json.loads(row[1])
            valid=digest(saved)==row[0]
            if k==key:valid=valid and digest(saved)==digest(plan)
            else:valid=valid and saved['key']==k and saved['forecast_plan']==plan and saved['payload']==plan['comment']
            if row[2]=='verified':valid=valid and json.loads(row[3]).get('verified') is True
        except (ValueError,TypeError,KeyError,AttributeError):valid=False
        if not valid or row[2] not in ('prepared','uncertain','verified','cancelled_unsent'):
            raise GlobalStop('Publication effect identity/state changed')
        states.append(row[2])
    if states[1] is not None and states[0]!='verified':raise GlobalStop('Comment without verified forecast')
    return tuple(states)


def finish(ledger,key,plan,reason):
    """Terminal accounting only, never clears an uncertain effect or permit."""
    old=getattr(ledger,'detail',None)
    if hasattr(ledger,'detail'):ledger.detail=('publication-finish',dict(key=key,reason=reason))
    try:
        with ledger.tx():
            ledger.guard();load(ledger,key)
            forecast,comment=effects(ledger,key,plan)
            if 'uncertain' in (forecast,comment):raise GlobalStop('Uncertain publication requires readback')
            for k,state in ((key,forecast),(key+'/comment',comment)):
                if state=='prepared':
                    # No dispatch was checkpointed; still reject any inconsistent permit.
                    exists=ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='effect_permits'").fetchone()
                    if exists and ledger.db.execute('SELECT 1 FROM effect_permits WHERE key IN (?,?)',('forecast/'+k,'comment/'+k)).fetchone():
                        raise GlobalStop('Prepared effect has a consumed permit')
                    if ledger.db.execute('SELECT receipt FROM submissions WHERE key=?',(k,)).fetchone()!=(None,):
                        raise GlobalStop('Prepared effect has a receipt')
                    ledger.db.execute("UPDATE submissions SET state='cancelled_unsent' WHERE key=?",(k,))
            state='complete' if comment=='verified' else 'forecast_only' if forecast=='verified' else 'not_submitted'
            ledger.db.execute('UPDATE publications SET state=?,reason=?,forecast_verified=?,comment_verified=? WHERE key=?',
                (state,reason,int(forecast=='verified'),int(comment=='verified'),key))
    finally:
        if hasattr(ledger,'detail'):ledger.detail=old
    return dict(key=key,status='submitted_verified' if state=='complete' else state,reason=reason)


def advance(host,client,key,*,enabled=False):
    if enabled is not True:raise GlobalStop('Publication continuation disabled')
    ledger=host.ledger;ledger.guard();plan=load(ledger,key)
    forecast,comment=effects(ledger,key,plan)
    if 'uncertain' in (forecast,comment):raise GlobalStop('Readback must precede continuation')
    if comment=='verified':return finish(ledger,key,plan,'verified')
    if 'cancelled_unsent' in (forecast,comment) or 'prepared' in (forecast,comment):
        return finish(ledger,key,plan,'attempt_closed_without_resend')
    if host.clock()>=instant(plan['binding']['cutoff_utc']):return finish(ledger,key,plan,'cutoff')
    host.effect_deadline=min(host.expiry,instant(plan['binding']['cutoff_utc']))
    try:
        host.before_effect()
        if forecast is None:
            submit_once(plan,ledger,fetch=client.fetch,
                post_forecast=forecast_transport(host,client,plan),clock=host.clock,enabled=True)
        submit_comment(plan,host,client,enabled=True)
    except GlobalStop:
        raise  # Keep intent pending, and possible effects for readback only.
    except Stop as exc:
        return finish(ledger,key,plan,str(exc)[:180])
    return finish(ledger,key,plan,'verified')
