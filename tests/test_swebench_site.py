"""Public study export must preserve unknowns and omit private agent content."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'evals/swebench'))
import build_site


class StudyExportTests(unittest.TestCase):
    def test_pending_is_not_a_failure_or_a_free_completed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'manifest.json').write_text(json.dumps({'arms':{'plain-matched':{'model':'fixed'}},
                'runs':{'one':{'arm':'plain-matched','rep':1,'instance_id':'repo__one-1'}}}))
            data=build_site.payload(root)
            self.assertFalse(data['complete'])
            self.assertEqual(data['arms'][0]['completed'],0)
            self.assertIsNone(data['arms'][0]['total_cost'])
            self.assertEqual(data['issues'][0]['arms']['plain-matched'][0]['state'],'pending')

    def test_private_fields_never_export_and_unknown_cost_remains_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);run=root/'runs/one';run.mkdir(parents=True)
            spec={'arm':'plain-matched','rep':1,'instance_id':'repo__one-1'}
            (root/'manifest.json').write_text(json.dumps({'arms':{'plain-matched':{}},'runs':{'one':spec},
                'baseline_source':'PRIVATE_PATH','settings':'PRIVATE_SECRET'}))
            (run/'result.json').write_text(json.dumps({'name':'one',**spec,'cost_usd':None,
                'wall_time_ms':12000,'infrastructure_failure':True,'notes':['PRIVATE_PROMPT'],
                'session_id':'PRIVATE_SESSION','native':{'provider_responses':2,'reasoning':'PRIVATE_REASONING'}}))
            data=build_site.payload(root);encoded=json.dumps(data)
            self.assertNotIn('PRIVATE_',encoded)
            self.assertEqual(data['arms'][0]['unknown_cost'],1)
            self.assertEqual(data['arms'][0]['errors'],1)
            self.assertIsNone(data['arms'][0]['total_cost'])
            self.assertEqual(data['issues'][0]['arms']['plain-matched'][0]['seconds'],12)

    def test_old_retrieval_example_does_not_claim_known_total_cost(self):
        example=build_site.example_data()[2]
        self.assertIsNone(example['groups'][1]['cost'])
        self.assertGreater(example['groups'][1]['seconds'],0)


if __name__=='__main__':unittest.main()
