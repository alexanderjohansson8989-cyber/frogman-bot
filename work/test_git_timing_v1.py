import unittest
from git_timing_v1 import TimedGit

class TimingTests(unittest.TestCase):
    def test_timing_records_success_and_failure_without_sensitive_arguments(self):
        class Transport:
            def ok(self,credential):return credential
            def fail(self,credential):raise TimeoutError(credential)
        values=iter([1.,1.25,2.,2.75]);events=[];g=TimedGit(Transport(),events,clock=lambda:next(values))
        self.assertEqual(g.ok('synthetic-sensitive-value'),'synthetic-sensitive-value')
        with self.assertRaises(TimeoutError):g.fail('synthetic-sensitive-value')
        self.assertEqual([x['seconds'] for x in events],[.25,.75])
        self.assertEqual(events[1]['error_type'],'TimeoutError')
        self.assertNotIn('synthetic-sensitive-value',str(events))

if __name__=='__main__':unittest.main()
