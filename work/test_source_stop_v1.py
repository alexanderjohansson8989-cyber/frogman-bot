import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from operations_ledger_v1 import GlobalStop
from independent_runner import Stop
from text_source_capture import capture_selected
from research_quality import capture_children
from source_failure_v1 import describe

class SourceStopTests(unittest.TestCase):
    def test_initial_global_stop_prevents_second_url(self):
        urls=['https://example.org/a','https://example.org/b']
        with patch('text_source_capture.capture',side_effect=GlobalStop('ownership lost')) as capture:
            with self.assertRaises(GlobalStop):capture_selected({},urls,urls,'unused')
            self.assertEqual(capture.call_count,1)
    def test_child_global_stop_prevents_second_url(self):
        urls=['https://example.org/a','https://example.org/b'];offered=[dict(url=u) for u in urls]
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'source').write_bytes(b'original html')
            doc=dict(network_media_type='text/html',archive_name='source',url='https://example.org/root')
            with patch('research_quality.linked_candidates',return_value=offered),patch('text_source_capture.capture',side_effect=GlobalStop('expired')) as capture:
                with self.assertRaises(GlobalStop):capture_children(offered,urls,[doc],tmp)
                self.assertEqual(capture.call_count,1)
    def test_redirect_and_http_failures_are_local_and_do_not_leak_messages(self):
        urls=['https://example.org/a','https://example.org/b']
        errors=[Stop('Redirect refused'),HTTPError(urls[1],403,'private secret',{},None)]
        with patch('text_source_capture.capture',side_effect=errors):r=capture_selected({},urls,urls,'unused')
        self.assertEqual(r['source_attempts'],2)
        self.assertEqual(r['failures'][0]['reason'],'Redirect refused')
        self.assertEqual(r['failures'][1]['http_status'],403)
        self.assertNotIn('private secret',str(r))
        self.assertEqual(describe(ValueError('secret')),{'error_type':'ValueError'})

if __name__=='__main__':unittest.main()
