import gzip,hashlib,io,tempfile,unittest
from pathlib import Path
from email.message import Message
from urllib.error import HTTPError
from bounded_evidence import MAX_BYTES
from independent_runner import Stop
from operations_ledger_v1 import GlobalStop
from source_http_v1 import fetch
from text_source_capture import capture,public_https
from research_quality import linked_candidates

URL='https://example.org/old'
class Response(io.BytesIO):
    def __init__(self,body,url=URL,encoding='identity'):
        super().__init__(body);self.url=url;self.headers=Message();self.headers['Content-Type']='text/html';self.headers['Content-Encoding']=encoding
    def geturl(self):return self.url

def resolver(host,port,**kw):return [(2,1,6,'',('127.0.0.1' if host=='private.example' else '93.184.216.34',443))]
def validate(url):return public_https(url,resolver)

class SourceHTTPTests(unittest.TestCase):
    def test_relative_redirect_and_original_address_are_recorded(self):
        calls=[]
        def open_request(req,timeout):
            calls.append(req.full_url)
            if len(calls)==1:raise HTTPError(URL,302,'move',{'Location':'/new/index'},None)
            return Response(b'<p>Readable original source.</p>',req.full_url)
        r=fetch(URL,open_request=open_request,validate_url=validate)
        self.assertEqual(calls,[URL,'https://example.org/new/index']);self.assertEqual(r['final_url'],calls[-1])
        self.assertEqual(len(r['redirects']),1)
    def test_private_http_and_credential_redirects_never_get_requested(self):
        for target in ('https://private.example/x','http://example.org/x','https://user:pass@example.org/x'):
            calls=[]
            def open_request(req,timeout):calls.append(req.full_url);raise HTTPError(URL,302,'move',{'Location':target},None)
            with self.assertRaises(Stop):fetch(URL,open_request=open_request,validate_url=validate)
            self.assertEqual(calls,[URL])
    def test_redirect_limit_and_loop(self):
        for loop in (False,True):
            calls=[]
            def open_request(req,timeout):
                calls.append(req.full_url);raise HTTPError(req.full_url,302,'move',{'Location':URL if loop else '/step'+str(len(calls))},None)
            with self.assertRaises(Stop):fetch(URL,open_request=open_request,validate_url=validate)
            self.assertEqual(len(calls),1 if loop else 3)
    def test_gzip_original_wire_and_decoded_payload_archived(self):
        body=b'<p>Observed value and context.</p>';wire=gzip.compress(body)
        with tempfile.TemporaryDirectory() as tmp:
            d=capture(URL,tmp,open_request=lambda req,timeout:Response(wire,encoding='gzip'),resolver=resolver)
            self.assertEqual(Path(tmp,d['archive_name']).read_bytes(),body)
            self.assertEqual(Path(tmp,d['wire_archive_name']).read_bytes(),wire)
            self.assertEqual(d['network_wire_sha256'],hashlib.sha256(wire).hexdigest())
            self.assertEqual(d['network_original_sha256'],hashlib.sha256(body).hexdigest())
    def test_expansion_truncation_concatenation_and_wire_limit(self):
        cases=[(gzip.compress(b'x'*(MAX_BYTES+1)),'gzip'),(gzip.compress(b'x')[:-2],'gzip'),
               (gzip.compress(b'x')+gzip.compress(b'y'),'gzip'),(b'x'*(MAX_BYTES+1),'identity')]
        for body,encoding in cases:
            with self.assertRaises(Stop):fetch(URL,open_request=lambda req,timeout:Response(body,encoding=encoding),validate_url=validate)
    def test_access_denial_and_global_stop_are_not_retried(self):
        for error in (HTTPError(URL,403,'denied',{},None),GlobalStop('deadline')):
            calls=[]
            def open_request(req,timeout):calls.append(req.full_url);raise error
            with self.assertRaises(type(error)):fetch(URL,open_request=open_request,validate_url=validate)
            self.assertEqual(calls,[URL])
    def test_uncontrolled_redirect_and_unknown_encoding_rejected(self):
        for response in (Response(b'x','https://example.org/uncontrolled'),Response(b'x',encoding='br')):
            with self.assertRaises(Stop):fetch(URL,open_request=lambda req,timeout:response,validate_url=validate)
    def test_child_relative_links_use_verified_final_url(self):
        raw=b'<a href="observed.csv">actual data</a>'
        doc=dict(url=URL,network_final_url='https://example.org/new/index',network_media_type='text/html',network_original_sha256=hashlib.sha256(raw).hexdigest())
        self.assertEqual(linked_candidates(doc,raw)[0]['url'],'https://example.org/new/observed.csv')

if __name__=='__main__':unittest.main()
