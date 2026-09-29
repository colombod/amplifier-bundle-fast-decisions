import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('laya_quality',Path(__file__).resolve().parents[1]/'evals/laya_quality.py')
study=importlib.util.module_from_spec(spec);spec.loader.exec_module(study)


class QualityStudyTests(unittest.TestCase):
    def test_frozen_case_labels_and_hosted_bounds(self):
        from amplifier_fast_decisions.laya_hosted import validate
        cases=study.fixtures()
        self.assertEqual(len(cases),60)
        self.assertEqual(len({c['id'] for c in cases}),60)
        for kind in ('select','search','cua'):
            self.assertEqual(sum(c['kind']==kind for c in cases),20)
        self.assertEqual(sum(c['expected'] is True for c in cases),10)
        self.assertEqual(sum(c['expected'] is False for c in cases),10)
        for case in cases:validate(case['payload'])

    def test_reason_and_uncertainty_are_not_automatic_successes(self):
        case=study.fixtures()[0]
        def result(choice,probs):return {'answers':{'decision':{'choice':choice,'probabilities':probs}}}
        wrong=study.score(case,result('b',{'a':.05,'b':.9,'reason':.05}))
        self.assertTrue(wrong['automatic_error'])
        unsure=study.score(case,result('a',{'a':.4,'b':.3,'reason':.3}))
        self.assertTrue(unsure['correct']);self.assertTrue(unsure['abstained'])
        fallback=study.score(case,result('reason',{'a':.05,'b':.05,'reason':.9}))
        self.assertFalse(fallback['automatic']);self.assertFalse(fallback['correct'])
        with self.assertRaises(ValueError):study.score(case,result('a',{'a':float('nan'),'b':0,'reason':0}))
