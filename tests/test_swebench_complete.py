"""Matched arms, official-grader cache keys and conservative cost accounting."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'evals/swebench'))
import forge_swebench as s


class CompleteCampaignTests(unittest.TestCase):
    def test_update_check_time_does_not_change_effective_settings_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.yaml'
            with patch.object(s,'SETTINGS',path):
                path.write_text('updates:\n  last_check: first\nconfig:\n  model: a\n')
                first=s._settings_sha256()
                path.write_text('config: {model: a}\nupdates: {last_check: second}\n')
                self.assertEqual(first,s._settings_sha256())
                path.write_text('config: {model: b}\nupdates: {last_check: second}\n')
                self.assertNotEqual(first,s._settings_sha256())

    def test_incremental_grading_preserves_prior_issue_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'grading').mkdir();runs={}
            for iid in ['a__b-1','a__b-2']:
                run=root/'runs'/iid;run.mkdir(parents=True)
                (run/'result.json').write_text('{}');(run/'patch.diff').write_text('patch')
                runs[iid]={'instance_id':iid,'arm':'plain-matched','rep':1}
            (root/'manifest.json').write_text(json.dumps({'runs':runs}))
            args=SimpleNamespace(root=str(root),swe_python=sys.executable,max_workers=1,
                timeout=10,per_instance=True,instances=['a__b-1'])
            with patch.object(s.subprocess,'run'),contextlib.redirect_stdout(io.StringIO()):
                s.cmd_grade(args)
                args.instances=['a__b-2'];s.cmd_grade(args)
            index=json.loads((root/'grading/active-reports.json').read_text())
            self.assertEqual(len(index),2)

    def test_matched_profiles_have_one_model_and_retrieval_only_in_its_arm(self):
        source = Path('/tmp/frozen-source')
        config = {'limits': {'max_iterations': 100, 'extended_thinking': True}, 'events_dir': '/tmp/events'}
        for arm in ['plain-matched', 'jev-prepared', 'laya-prepared', 'jevgrep']:
            spec = s.ARMS[arm]
            profile, side = s._arm_profile('test', spec, source, source,
                {'instance_id': 'a__b-1'}, Path('/tmp/work'), config)
            self.assertEqual(spec['model'], 'claude-sonnet-5')
            self.assertEqual(side['source_root'], str(source))
            self.assertNotIn('fast-decisions', profile['includes'][0]['bundle'])
            tools = [t['module'] for t in profile['tools']]
            self.assertEqual('tool-jevgrep' in tools, arm == 'jevgrep')
            self.assertEqual(side['mode'], 'active' if arm in {'jev-prepared', 'laya-prepared'} else 'off')
            if side['mode'] == 'active':
                self.assertIsNone(side['decision_overrides']['model_routing'])
                self.assertEqual(side['decision_overrides']['effort_routing'], {})

    def test_legacy_plain_no_longer_inherits_default_retrieval(self):
        profile, _ = s._arm_profile('x', s.ARMS['plain'], Path('/tmp/source'), Path('/tmp/base'),
            {'instance_id':'x'}, Path('/tmp/work'),
            {'limits':{'max_iterations':5,'extended_thinking':True},'events_dir':'/tmp/events'})
        self.assertEqual(profile['includes'], [{'bundle':'git+https://github.com/microsoft/amplifier-foundation@main'}])
        self.assertFalse(any(t['module']=='tool-jevgrep' for t in profile['tools']))

    def test_retrieval_usage_unknown_is_not_free(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'usage.jsonl'
            self.assertIsNone(s._retrieval_cost(path))
            path.write_text('{"input_tokens":1000}\n')
            self.assertAlmostEqual(s._retrieval_cost(path), .000042)
            path.write_text('{"input_tokens":1000}\n{"input_tokens":null}\n')
            self.assertIsNone(s._retrieval_cost(path))

    def test_grader_uses_new_run_id_when_patch_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); run=root/'runs/r';run.mkdir(parents=True)
            (root/'manifest.json').write_text(json.dumps({'dataset':'princeton-nlp/SWE-bench',
                'runs':{'r':{'arm':'plain-matched','rep':1,'instance_id':'a__b-1'}}}))
            (run/'result.json').write_text('{}');(run/'patch.diff').write_text('first')
            args=SimpleNamespace(root=str(root),swe_python=sys.executable,max_workers=1,timeout=10)
            with patch.object(s.subprocess,'run') as execute, contextlib.redirect_stdout(io.StringIO()):
                s.cmd_grade(args);first=execute.call_args.args[0]
                (run/'patch.diff').write_text('second')
                s.cmd_grade(args);second=execute.call_args.args[0]
            self.assertNotEqual(first[first.index('--run_id')+1],second[second.index('--run_id')+1])
            self.assertEqual(second[second.index('--dataset_name')+1],'princeton-nlp/SWE-bench')

    def test_grading_error_does_not_count_as_completed_grading(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'grading').mkdir();(root/'runs/r').mkdir(parents=True)
            (root/'grading/plain.plain-r1.json').write_text(json.dumps({
                'submitted_ids':['a__b-1'],'completed_ids':[], 'resolved_ids':[], 'error_ids':['a__b-1']}))
            (root/'runs/r/result.json').write_text('{}')
            rows=s._report_rows(root,{'runs':{'r':{'instance_id':'a__b-1','arm':'plain','rep':1}}})
            self.assertIsNone(rows[0]['resolved'])

    def test_empty_patch_is_unresolved_but_corrupt_completed_report_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'grading').mkdir();runs={}
            for name in ['empty','corrupt']:
                (root/'runs'/name).mkdir(parents=True)
                (root/'runs'/name/'result.json').write_text('{}')
                runs[name]={'instance_id':name,'arm':'plain','rep':1}
            (root/'grading/plain.plain-r1.json').write_text(json.dumps({
                'completed_ids':['corrupt'],'error_ids':['corrupt'],
                'empty_patch_ids':['empty'],'resolved_ids':[]}))
            rows=s._report_rows(root,{'runs':runs})
            self.assertIs(rows[0]['resolved'],False)
            self.assertIsNone(rows[1]['resolved'])

    def test_docker_endpoint_overrides_inherited_context(self):
        with patch.dict(s.os.environ,{'DOCKER_CONTEXT':'desktop-linux','DOCKER_HOST':'old'}):
            env=s._docker_env({'docker_host':'unix:///benchmark.sock'})
        self.assertNotIn('DOCKER_CONTEXT',env)
        self.assertEqual(env['DOCKER_HOST'],'unix:///benchmark.sock')

    def test_controller_lock_prevents_duplicate_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with (root/'controller.lock').open('a') as lock:
                s.fcntl.flock(lock,s.fcntl.LOCK_EX|s.fcntl.LOCK_NB)
                with self.assertRaisesRegex(SystemExit,'Another controller'):
                    s.cmd_run(SimpleNamespace(root=str(root)))

    def test_budget_guard_refuses_unknown_prior_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'runs/old').mkdir(parents=True)
            (root/'runs/old/result.json').write_text('{"cost_usd":null}')
            (root/'manifest.json').write_text(json.dumps({'arms':{'plain-matched':{'matched':True}},
                'run_order':['old','next']}))
            args=SimpleNamespace(root=str(root),parallel=1,max_cost_usd=100,reserve_per_run_usd=10)
            with patch.object(s.forge_e2e,'forge_self_heal'), patch.object(s,'_run_one') as run, \
                 self.assertRaisesRegex(SystemExit,'Unknown prior cost'):
                s.cmd_run(args)
            run.assert_not_called()

    @unittest.skipUnless(shutil.which('node'),'Node required for benchmark-only CLI metering')
    def test_node_meter_records_only_usage_without_altering_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'usage.jsonl'
            meter=Path(s.__file__).with_name('jevgrep_usage.mjs').as_uri()
            program='''
process.env.FD_JEVGREP_USAGE_FILE=DESTINATION;
globalThis.fetch=async()=>new Response(JSON.stringify({usage:{input_tokens:123,output_tokens:7},answers:{secret:'DO_NOT_RECORD'}}));
await import(METER);
const response=await fetch('https://api.typesafe.ai/v1/systemone',{headers:{Authorization:'DO_NOT_RECORD'}});
if ((await response.json()).answers.secret!=='DO_NOT_RECORD') process.exit(2);
'''.replace('DESTINATION',json.dumps(str(path))).replace('METER',json.dumps(meter))
            subprocess.run(['node','--input-type=module','-e',program],check=True,capture_output=True)
            text=path.read_text(); self.assertNotIn('DO_NOT_RECORD',text)
            self.assertEqual(json.loads(text)['input_tokens'],123)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)


if __name__ == '__main__': unittest.main()
