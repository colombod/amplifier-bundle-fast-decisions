import copy
import unittest

import experiment as e


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.case = e.cases()[0]

    def atomic_response(self, first=.96, second=.1):
        return {'model': e.MODEL, 'answers': {
            f'{c}_{signal}': {'type': 'noul', 'noul': value}
            for c, value in [('c0', first), ('c1', second)] for signal in e.SIGNALS}}

    def test_answer_key_cannot_influence_either_request(self):
        changed = copy.deepcopy(self.case)
        changed.update(expected='wrong', family='wrong', split='wrong', label_source='wrong')
        for arm in ['direct', 'atomic']:
            self.assertEqual(e.payload(self.case, arm), e.payload(changed, arm))

    def test_shared_state_and_single_batched_atomic_request(self):
        direct, atomic = [e.payload(self.case, arm) for arm in ['direct', 'atomic']]
        self.assertEqual(direct['state'], atomic['state'])
        self.assertEqual(len(atomic['questions']), 6)
        self.assertTrue(all(q['type'] == 'noul' for q in atomic['questions'].values()))

    def test_one_negative_signal_prevents_action(self):
        response = self.atomic_response()
        response['answers']['c0_pending']['noul'] = .01
        decision = e.choose(self.case, 'atomic', response)
        self.assertEqual(decision['choice'], 'reason')
        self.assertEqual(decision['score_kind'], 'minimum_atomic_signal_not_calibrated')

    def test_close_or_tied_candidates_abstain(self):
        for first, second in [(.96, .94), (.95, .95)]:
            self.assertEqual(e.choose(self.case, 'atomic', self.atomic_response(first, second))['choice'], 'reason')
        self.assertEqual(e.choose(self.case, 'atomic', self.atomic_response())['choice'], 'c0')

    def test_invalid_response_and_version_are_rejected(self):
        response = self.atomic_response()
        response['model'] = 'jev-latest'
        with self.assertRaises(ValueError):
            e.choose(self.case, 'atomic', response)
        response['model'] = e.MODEL
        response['answers']['c0_target']['noul'] = float('nan')
        with self.assertRaises(ValueError):
            e.choose(self.case, 'atomic', response)

    def test_direct_uses_same_gate_with_rounded_distribution(self):
        response = {'model': e.MODEL, 'answers': {'next_action': {
            'choice': 'c0', 'probabilities': {'c0': .94, 'c1': .03, 'reason': .02}, 'confidence': .81}}}
        self.assertEqual(e.choose(self.case, 'direct', response)['choice'], 'c0')
        response['answers']['next_action']['probabilities']['c0'] = .84
        response['answers']['next_action']['probabilities']['reason'] = .12
        self.assertEqual(e.choose(self.case, 'direct', response)['choice'], 'reason')

    def test_family_split_prevents_variant_leakage(self):
        families = {}
        data = e.cases()
        self.assertEqual(len(data), 32)
        self.assertEqual(len({c['id'] for c in data}), 32)
        for c in data:
            families.setdefault(c['family'], set()).add(c['split'])
        self.assertTrue(all(len(splits) == 1 for splits in families.values()))
        self.assertEqual(sum(c['split'] == 'evaluation' for c in data), 16)


if __name__ == '__main__':
    unittest.main()
