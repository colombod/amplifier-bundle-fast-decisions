"""The supervisor rejects receipts that cannot support a matched comparison."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'evals/swebench'))
import supervise


class ReceiptAuditTests(unittest.TestCase):
    def test_valid_receipts_pass_but_delegation_and_model_drift_stop_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);run=root/'runs/one';(run/'events').mkdir(parents=True)
            manifest={'source_tree_sha256':'frozen','arms':{'plain-matched':{}},
                'runs':{'one':{'arm':'plain-matched','model':'fixed'}}}
            result={'cost_usd':1,'session_id':'sid','native':{'tool_names':{}},
                'models_served':[{'model':'fixed','thinking_enabled':True}]}
            (run/'result.json').write_text(json.dumps(result))
            (run/'events/source.jsonl').write_text(json.dumps({'session_id':'sid',
                'event':'fast_decisions:source','data':{'source_tree_sha256':'frozen','mode':'off'}})+'\n')
            self.assertEqual(supervise.audit(root,manifest,['one']),[])
            result['native']['tool_names']['delegate']=1
            result['models_served'][0]['model']='different'
            (run/'result.json').write_text(json.dumps(result))
            reasons={f['reason'] for f in supervise.audit(root,manifest,['one'])}
            self.assertIn('delegation_would_escape_parent_cost_accounting',reasons)
            self.assertIn('served_model_or_thinking_mismatch',reasons)


if __name__=='__main__':unittest.main()
