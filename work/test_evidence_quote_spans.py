import copy,json,unittest
from evidence_quote_spans import locate,bind,BOUNDARY
from compact_review_host_4 import check
from independent_runner import Stop

class QuoteSpansTests(unittest.TestCase):
    def test_layout_changes_retain_original_positions(self):
        t='prefix\nValue\u00a0 2026\r\n\twas 19.0 percent.\nsuffix'
        p=locate(t,'Value 2026 was 19.0 percent.')[0]
        self.assertEqual(t[p['start']:p['end']],p['original_quote'])
        self.assertEqual(p['original_quote'],'Value\u00a0 2026\r\n\twas 19.0 percent.')
        self.assertEqual(p['match'],'whitespace_only')
    def test_no_number_sign_case_or_punctuation_repair(self):
        t='The observed change was -19.0 percent.'
        for q in ('The observed change was 19.0 percent.','The observed change was -18.0 percent.',
                  'The Observed change was -19.0 percent.','The observed change was −19.0 percent.'):
            self.assertEqual(locate(t,q),[])
    def test_no_joining_separate_spans_or_removed_spaces(self):
        self.assertEqual(locate('First observed '+BOUNDARY+' value is 19.0 percent.','First observed value is 19.0 percent.'),[])
        self.assertEqual(locate('The observed change was 1 9.0 percent.','The observed change was 19.0 percent.'),[])
        with self.assertRaises(Stop):locate('anything',BOUNDARY+' more text')
    def test_all_duplicates_preserved_but_other_url_excluded(self):
        q='The observed value is 19.0.'
        docs=[{'url':'a','text':q+' '+q},{'url':'b','text':q}]
        before=copy.deepcopy(docs);p=bind(docs,'a',q)
        self.assertEqual(len(p),2);self.assertEqual(docs,before)
        self.assertEqual({x['document_index'] for x in p},{0})
        with self.assertRaises(Stop):bind(docs,'c',q)
        with self.assertRaises(Stop):locate((q+' ')*257,q)
    def test_host_preserves_rejection_and_unfinished_status(self):
        docs=[{'url':'a','text':'Observed value\nwas 19.0 percent.'}]
        answer={'facts':[{'claim':'Observed value was 19 percent.','url':'a'}]}
        body={'checks':[{'fact_index':0,'supported':True,'evidence':[{'url':'a','quote':'Observed value was 19.0 percent.'}]}],
              'unsupported':['The historical frequency is unsupported.'],'subjective':[]}
        raw={'stop_reason':'end_turn','content':[{'type':'text','text':'<compact_review>'+json.dumps(body)+'</compact_review>'}]}
        before=copy.deepcopy((raw,answer,docs));result=check(raw,{},answer,docs)
        self.assertEqual(result['decision'],'reject');self.assertIsNone(result['usable_answer'])
        self.assertEqual(before,(raw,answer,docs));self.assertEqual(result['review'],body)
        raw['stop_reason']='max_tokens'
        with self.assertRaises(Stop):check(raw,{},answer,docs)
    def test_host_never_accepts_changed_fact_source_or_fabricated_quote(self):
        docs=[{'url':'a','text':'Observed value was 19.0 percent.'},{'url':'b','text':'Observed value was 18.0 percent.'}]
        answer={'facts':[{'claim':'Observed value was 19 percent.','url':'a'}]}
        for url,quote in [('b','Observed value was 18.0 percent.'),('a','Observed value was 18.0 percent.')]:
            body={'checks':[{'fact_index':0,'supported':True,'evidence':[{'url':url,'quote':quote}]}], 'unsupported':[],'subjective':[]}
            raw={'stop_reason':'end_turn','content':[{'type':'text','text':'<compact_review>'+json.dumps(body)+'</compact_review>'}]}
            with self.assertRaises(Stop):check(raw,{},answer,docs)

if __name__=='__main__':unittest.main()
