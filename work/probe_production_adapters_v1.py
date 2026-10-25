"""Read-only live adapter probe. No models, state branch access or writes."""
import datetime as dt,json,os
from candidate_api_v1 import MetaculusClient
from github_git_transport_v1 import GitHubGit
from operations_ledger_v1 import GlobalStop
from production_adapters_v1 import list_posts
from production_entry_v1 import REPO
from general_evaluation import instant


def run(git,mc,*,clock):
    deadline=clock()+dt.timedelta(minutes=3);git_reads=0
    def observe():
        nonlocal git_reads
        if clock()>=deadline:raise GlobalStop('Probe deadline')
        if git_reads>=4:raise GlobalStop('Probe Git read bound')
        info=git.repository();git_reads+=1
        if info.get('full_name')!=REPO or info.get('private') is not True:raise GlobalStop('Private repository differs')
    observe()
    def guard():
        if clock()>=deadline:raise GlobalStop('Probe deadline')
        q=git.quota_observation()
        if clock()>=instant(q['observed_utc'])+dt.timedelta(seconds=45):observe();q=git.quota_observation()
        if (not instant(q['observed_utc'])<=clock()<instant(q['observed_utc'])+dt.timedelta(seconds=60)
                or q['remaining']<10 or clock()>=instant(q['reset_utc'])):raise GlobalStop('Probe quota unavailable')
    guard();me=mc.me();guard()
    if me!=dict(id=310015,username='Frogman-bot'):raise GlobalStop('Probe actor differs')
    posts=list_posts(mc,33121,guard=guard)
    return dict(version='production-adapters-probe-v1',status='complete',actor=me,
        observed_utc=clock().isoformat(),rows=[dict(post_id=p['id'],question_id=(p.get('question') or {}).get('id'),
            api_type=(p.get('question') or {}).get('type'),status=p.get('status')) for p in posts],
        quota=git.quota_observation(),git_repository_reads=git_reads,max_listing_pages=3,
        model_calls=0,metaculus_writes=0,state_reads=0,state_writes=0,
        scope='Bounded open feed only; no forecast or source content exported')


def main():
    if os.environ.get('GITHUB_REPOSITORY')!=REPO:raise GlobalStop('Probe repository differs')
    clock=lambda:dt.datetime.now(dt.timezone.utc)
    result=run(GitHubGit(REPO,os.environ['GITHUB_TOKEN'],clock=clock),
        MetaculusClient(os.environ['METACULUS_TOKEN']),clock=clock)
    print('PRODUCTION_ADAPTERS_PROBE_RESULT '+json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
