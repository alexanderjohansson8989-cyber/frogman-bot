import tempfile,unittest
from source_alternatives_probe_v1 import run
import test_source_fallback_v1 as fixture
import test_source_http_v1 as http
from independent_runner import Stop

class SourceProbeTests(unittest.TestCase):
    def input(self):return dict(version='source-alternatives-read-v1',source_run_id=39241399017,publication=False,
        initial_capture=fixture.empty([fixture.URL]),selected_urls=[fixture.URL],searched_urls=fixture.HITS,
        search_urls_response=fixture.reply(fixture.HITS),source_search_receipt_sha256='a'*64)
    def test_two_actual_reads_no_initial_retry_or_model(self):
        calls=[]
        def read(req,timeout):calls.append(req.full_url);return http.Response(b'<p>Readable test source content.</p>',req.full_url)
        with tempfile.TemporaryDirectory() as tmp:r=run(self.input(),tmp,open_request=read,resolver=http.resolver)
        self.assertEqual(calls,[fixture.HITS[1],fixture.HITS[3]]);self.assertEqual(len(r['readable_documents']),2)
        self.assertEqual(r['new_model_calls'],0);self.assertFalse(r['semantic_relevance_reviewed'])
    def test_successful_initial_read_cannot_be_replayed_as_failure(self):
        p=self.input();p['initial_capture']['documents']=[{'url':fixture.URL}]
        with self.assertRaises(Stop):run(p,'unused',open_request=lambda *a:self.fail('No request allowed'))
if __name__=='__main__':unittest.main()
