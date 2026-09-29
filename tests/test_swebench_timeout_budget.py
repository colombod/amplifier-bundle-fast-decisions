import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'evals/swebench'))
import budget_accounting as b
import forge_swebench as campaign


class TimeoutBudgetTests(unittest.TestCase):
    def test_spawn_failure_heals_once_but_ambiguous_launch_never_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory)
            forge=SimpleNamespace(call=Mock(side_effect=[SystemExit('Error: posix_spawnp failed.'),{'sessionId':'ok'}]))
            with patch.object(campaign.forge_e2e,'forge_self_heal',return_value=True):
                self.assertEqual(campaign._launch_worker(forge,'command',run),{'sessionId':'ok'})
            self.assertEqual(forge.call.call_count,2)
            self.assertTrue((run/'pre-spawn-repair.json').exists())
            for failure in ['observation deadline', 'Error: posix_spawnp failed.']:
                (run/'amplifier-output.json').write_text('')
                forge.call=Mock(side_effect=SystemExit(failure))
                with patch.object(campaign.forge_e2e,'forge_self_heal') as heal:
                    campaign._launch_worker(forge,'command',run)
                self.assertEqual(forge.call.call_count,1);heal.assert_not_called()

    def test_repeated_pre_spawn_failure_stops_after_one_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            forge=SimpleNamespace(call=Mock(side_effect=SystemExit('Error: posix_spawnp failed.')))
            with patch.object(campaign.forge_e2e,'forge_self_heal',return_value=True):
                out=campaign._launch_worker(forge,'command',Path(directory))
            self.assertIn('launch_error',out);self.assertEqual(forge.call.call_count,2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root/'runs/one/events';self.events.mkdir(parents=True)
        self.manifest = {'unknown_jev_timeout_hold_usd':10, 'prior_cost_usd':2,
                         'runs':{'one':{},'next':{}},'run_order':['one','next'],
                         'arms':{'jev-prepared':{'matched':True}}}
        self.result = {'name':'one','session_id':'sid','cost_usd':None,
                       'provider_cost_usd':1,'retrieval_cost_usd':0,'judge_cost_usd':None}
        self.write_events()

    def write_events(self, extra=None):
        rows=[]
        for did,kind,data in [('ok','requested',{}),('ok','scored',{'input_tokens':1000}),
                              ('late','requested',{}),('late','fallback',{'reason_code':'decision_timeout'}),
                              ('late','routed',{'reason_code':'decision_timeout'})]:
            rows.append({'session_id':'sid','decision_id':did,'event_id':did+kind,
                         'event':'fast_decisions:'+kind,'data':{'backend':'jev',**data}})
        if extra: rows.append(extra)
        (self.events/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))

    def test_unknown_stays_unknown_and_timeout_is_reserved_once(self):
        original=copy.deepcopy(self.result)
        ledger=b.ledger(self.root,self.manifest,[self.result])
        self.assertAlmostEqual(ledger['known_cost_usd'],3.000042)
        self.assertEqual(ledger['held_cost_usd'],10)
        self.assertEqual(ledger['unknown_requests'],1)
        self.assertEqual(self.result,original)

    def test_without_policy_or_with_missing_other_usage_still_stops(self):
        for manifest,result in [({**self.manifest,'unknown_jev_timeout_hold_usd':None},self.result),
                                (self.manifest,{**self.result,'provider_cost_usd':None}),
                                (self.manifest,{**self.result,'infrastructure_failure':True})]:
            self.assertIsNone(b.account(self.root,manifest,result))

    def test_unattributed_request_or_backend_error_stops(self):
        for kind,data in [('requested',{}),('fallback',{'reason_code':'backend_error'})]:
            self.write_events({'session_id':'sid','decision_id':'other','event_id':'other',
                               'event':'fast_decisions:'+kind,'data':{'backend':'jev',**data}})
            self.assertIsNone(b.account(self.root,self.manifest,self.result))

    def test_resolved_charge_releases_hold(self):
        result={**self.result,'cost_usd':1.0001}
        entry=b.account(self.root,self.manifest,result)
        self.assertEqual(entry['held_usd'],0)
        self.assertEqual(entry['known_usd'],1.0001)

    def test_budget_guard_counts_hold_before_any_paid_launch(self):
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        (self.events.parent/'result.json').write_text(json.dumps(self.result))
        args=SimpleNamespace(root=str(self.root),parallel=1,max_cost_usd=20,reserve_per_run_usd=10)
        with patch.dict(sys.modules,{'forge':SimpleNamespace()}), patch.object(campaign.forge_e2e,'forge_self_heal'), patch.object(campaign,'_run_one') as run:
            campaign.cmd_run(args)
        run.assert_not_called()
        status=json.loads((self.root/'campaign-status.json').read_text())
        self.assertEqual(status['status'],'budget_exhausted')
        self.assertEqual(status['held_cost_usd'],10)

    def test_resumes_next_run_without_repeating_unknown_run(self):
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        (self.events.parent/'result.json').write_text(json.dumps(self.result))
        args=SimpleNamespace(root=str(self.root),parallel=1,max_cost_usd=100,reserve_per_run_usd=10)
        def finish(root,manifest,name,forge):
            self.assertEqual(name,'next')
            result={'name':name,'cost_usd':2};p=root/'runs'/name;p.mkdir();(p/'result.json').write_text(json.dumps(result));return result
        with patch.dict(sys.modules,{'forge':SimpleNamespace()}), patch.object(campaign.forge_e2e,'forge_self_heal'), patch.object(campaign,'_run_one',side_effect=finish) as run:
            campaign.cmd_run(args)
        self.assertEqual(run.call_count,1)
        status=json.loads((self.root/'campaign-status.json').read_text())
        self.assertAlmostEqual(status['known_cost_usd'],5.000042)
        self.assertEqual(status['held_cost_usd'],10)
