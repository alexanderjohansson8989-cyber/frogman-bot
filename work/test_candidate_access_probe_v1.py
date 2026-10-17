import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from probe_candidate_access_v1 import main,get,PATHS,page_path
from independent_runner import Stop
from test_competition_adapter_v1 import post_fixture,ME,NOW
from competition_adapter_v1 import bind_post

class ProbeTests(unittest.TestCase):
    def run_probe(self,fetch):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'FROGMAN_PROBE_OUTPUT':tmp}),contextlib.redirect_stdout(io.StringIO()):
            r=main(fetch);stored=json.loads((Path(tmp)/'result.json').read_bytes())
            self.assertEqual(stored,r);return r
    def test_only_public_actor_fields_survive_and_calls_are_bounded(self):
        calls=[]
        def fetch(path):
            calls.append(path)
            return dict(id=500,username='Frogman-bot',email='private@example.org',token='do-not-save') if path==PATHS[0] else dict(results=[],next=None)
        r=self.run_probe(fetch);self.assertEqual(calls,list(PATHS));self.assertEqual(r['actor'],dict(id=500,username='Frogman-bot'))
        self.assertNotIn('do-not-save',json.dumps(r));self.assertNotIn('private@example.org',json.dumps(r))
    def test_wrong_actor_stops_before_post_data(self):
        calls=[]
        def fetch(path):calls.append(path);return dict(id=1,username='Other')
        r=self.run_probe(fetch);self.assertEqual(r['status'],'probe_error');self.assertEqual(len(calls),1)
    def test_write_endpoint_is_impossible(self):
        with self.assertRaises(Stop):get('questions/forecast/')
    def test_empty_page_does_not_mean_empty_tournament_and_filters_cannot_change(self):
        calls=[];url='https://www.metaculus.com/api/'+PATHS[1]+'&offset=25'
        def fetch(path):
            calls.append(path)
            if path==PATHS[0]:return dict(id=500,username='Frogman-bot')
            return dict(results=[],next=url if path==PATHS[1] else None)
        r=self.run_probe(fetch);self.assertEqual(r['page_attempts'],2);self.assertEqual(len(calls),3)
        for bad in (url.replace('www.metaculus.com','evil.example'),url.replace('statuses=open','statuses=closed'),url+'&limit=1'):
            with self.assertRaises(Stop):page_path(bad)
    def test_bind_post_is_pure_validation_with_network_forbidden(self):
        with patch('urllib.request.build_opener',side_effect=AssertionError('Network forbidden')):
            b=bind_post(post_fixture(),ME,tournament_id=33121,expected_username='Frogman-bot',now=NOW)
        self.assertEqual(b['question_id'],102)
    def test_probe_transport_only_constructs_get_requests(self):
        seen=[]
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,n):return b'{}'
        class Opener:
            def open(self,req,timeout):seen.append((req.get_method(),req.full_url,req.data));return Reply()
        with patch.dict(os.environ,{'METACULUS_TOKEN':'synthetic-test-secret'}),patch('urllib.request.build_opener',return_value=Opener()):
            get(PATHS[0]);get(PATHS[1]);get(page_path('https://www.metaculus.com/api/'+PATHS[1]+'&offset=25'))
        self.assertEqual([s[0] for s in seen],['GET','GET','GET']);self.assertTrue(all(s[2] is None for s in seen))

if __name__=='__main__':unittest.main()
