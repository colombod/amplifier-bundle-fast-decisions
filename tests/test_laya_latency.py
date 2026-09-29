import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('laya_latency',Path(__file__).resolve().parents[1]/'evals/laya_latency.py')
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


class LatencyStudyTests(unittest.TestCase):
    def test_percentiles_keep_tail_and_failures_separate(self):
        rows=[{'status':200,'valid':True,'elapsed_ms':x} for x in [10,20,30,40]]
        rows.append({'status':503,'elapsed_ms':1})
        result=study.stats(rows)
        self.assertEqual((result['n'],result['ok'],result['errors']),(5,4,1))
        self.assertEqual(result['p50_ms'],25)
        self.assertEqual(result['p95_ms'],38.5)
        self.assertEqual(result['status_counts'],{'200':4,'503':1})

    def test_pairs_exclude_failures_and_preserve_disagreement(self):
        rows=[]
        for repeat, status in [(0,200),(1,503)]:
            for endpoint, elapsed, decision in [('local',10,'a'),('hosted',30,'b')]:
                rows.append({'endpoint':endpoint,'phase':'serial','transport':'fresh','concurrency':1,
                             'fixture':'select','repeat':repeat,'status':status if endpoint=='hosted' else 200,
                             'valid':True,'elapsed_ms':elapsed,'decision':decision})
        result=study.summarize(rows)['paired']['fresh']
        self.assertEqual(result['matched_pairs'],1)
        self.assertEqual(result['hosted_minus_local_p50_ms'],20)
        self.assertEqual(result['answer_agreement'],0)

    def test_fixture_sizes_fit_hosted_contract(self):
        from amplifier_fast_decisions.laya_hosted import validate
        cases=study.fixtures()
        self.assertEqual(len(cases),12)
        for case in cases:
            validate(case['payload'])
