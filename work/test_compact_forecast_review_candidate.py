import copy
import json
import unittest
from compact_forecast_review_candidate import check,payload,quote_positions
from independent_runner import Stop


class CompactReviewTests(unittest.TestCase):
    def setUp(self):
        self.q={'criteria':'Series X, monthly observed value, not smoothed.'}
        self.answer={'facts':[{'claim':'Two observations imply a difference of 3.', 'url':'https://a.example'}],
                     'assumptions':['Subjective predictive sd is 20.']}
        self.docs=[{'url':'https://a.example','text':'First observation was 12. Second observation was 15.'}]
        self.body={'checks':[{'fact_index':0,'supported':True,'evidence':[
            {'url':'https://a.example','quote':'First observation was 12.'},
            {'url':'https://a.example','quote':'Second observation was 15.'}]}],
            'unsupported':[],'subjective':['Predictive sd20 is a modelling choice, not a measured historical sd.']}

    def call(self,body):
        return check({'stop_reason':'end_turn','content':[{'type':'text','text':'<compact_review>'+json.dumps(body)+'</compact_review>'}]},self.q,self.answer,self.docs)

    def test_compound_evidence_and_judgement_preserve_original(self):
        before=copy.deepcopy(self.answer);r=self.call(self.body)
        self.assertEqual(r['decision'],'pass');self.assertEqual(self.answer,before)
        p=payload(self.q,self.answer,self.docs)
        self.assertEqual(p['max_tokens'],2500);self.assertNotIn('tools',p)
        self.assertEqual(json.loads(p['messages'][0]['content'])['documents'],self.docs)

    def test_historical_premise_blocks_but_retains_raw_answer(self):
        b=copy.deepcopy(self.body);b['unsupported']=['Claimed historical event frequency55% has no support.']
        r=self.call(b);self.assertEqual(r['decision'],'reject');self.assertIsNone(r['usable_answer']);self.assertEqual(r['original_answer'],self.answer)

    def test_quotes_cannot_use_unseen_original_or_change_source(self):
        for value in ['An unseen full-original quotation.','Made up historical data quote.']:
            b=copy.deepcopy(self.body);b['checks'][0]['evidence'][0]['quote']=value
            with self.assertRaises(Stop):self.call(b)
        b=copy.deepcopy(self.body);b['checks'][0]['evidence'][0]['url']='https://other.example'
        with self.assertRaises(Stop):self.call(b)

    def test_partial_output_or_duplicate_index_cannot_pass(self):
        with self.assertRaises(Stop):check({'content':[{'type':'text','text':'<compact_review>{'}]},self.q,self.answer,self.docs)
        b=copy.deepcopy(self.body);b['checks'][0]['fact_index']=True
        with self.assertRaises(Stop):self.call(b)
        b=copy.deepcopy(self.body);b['checks'][0]['evidence']=[]
        with self.assertRaises(Stop):self.call(b)

    def test_repeated_quotes_preserve_all_actual_positions(self):
        quote='Exact repeated statement.'
        docs=[{'original_sha256':'frozen','pages':[{'page':1,'text':quote+' gap '+quote},{'page':2,'text':quote}]}]
        before=copy.deepcopy(docs);positions=quote_positions(docs,'frozen',quote)
        self.assertEqual(len(positions),3)
        for p in positions:
            self.assertEqual(docs[p['document_index']]['pages'][p['page_index']]['text'][p['start']:p['end']],quote)
        self.assertEqual(docs,before)
        with self.assertRaises(Stop):quote_positions(docs,'wrong',quote)

    def test_cross_excerpt_quote_rejected_but_separate_quotes_work(self):
        self.docs[0]['text']='First observation was 12.\n[EXCERPT BOUNDARY]\nSecond observation was 15.'
        self.assertEqual(self.call(self.body)['decision'],'pass')
        b=copy.deepcopy(self.body)
        b['checks'][0]['evidence']=[{'url':'https://a.example','quote':self.docs[0]['text']}]
        with self.assertRaises(Stop):self.call(b)

    def test_valid_json_from_unfinished_response_cannot_pass(self):
        content=[{'type':'text','text':'<compact_review>'+json.dumps(self.body)+'</compact_review>'}]
        for reason in ('max_tokens','pause_turn',None):
            with self.assertRaises(Stop):
                check({'stop_reason':reason,'content':content},self.q,self.answer,self.docs)


if __name__=='__main__':unittest.main()
