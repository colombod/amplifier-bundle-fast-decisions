"""Packaging/config checks plus opt-in checks against actual installed envelopes.

Skipped upstream checks are reported as skipped, never counted as proven runtime tests.
"""
import asyncio
import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from amplifier_fast_decisions.cli import configure, export_html
from amplifier_fast_decisions.contracts import Candidate, EVENT_NAMES, Policy
from amplifier_fast_decisions.candidates import parse_candidate
from amplifier_fast_decisions.privacy import safe_data
from amplifier_fast_decisions.runtime import get_runtime
from amplifier_fast_decisions.demo import DemoCoordinator, DemoTool, DemoProvider
from amplifier_fast_decisions.orchestrator import RoutedProvider
from test_decisions import setup_service, request

ROOT = Path(__file__).resolve().parents[1]
HAS_CORE = importlib.util.find_spec("amplifier_core") is not None
HAS_LOOP = importlib.util.find_spec("amplifier_module_loop_streaming") is not None

class BuildTests(unittest.TestCase):
    def test_active_profile_pins_the_package_and_loop_together(self):
        import yaml
        data = yaml.safe_load((ROOT/'bundles/active.yaml').read_text())
        package = data['includes'][0]['bundle']
        self.assertRegex(package, r'^git\+https://github.com/michaeljabbour/amplifier-bundle-fast-decisions@[0-9a-f]{40}$')
        self.assertEqual(data['session']['orchestrator']['source'], package + '#subdirectory=modules/loop-fast-decisions')
        self.assertEqual(data['session']['orchestrator']['config']['mode'], 'active')

    def test_active_loop_installs_its_wrapped_upstream(self):
        data = tomllib.loads((ROOT/'modules/loop-fast-decisions/pyproject.toml').read_text())
        self.assertIn(
            'amplifier-module-loop-streaming @ git+https://github.com/microsoft/amplifier-module-loop-streaming@4cc86dd4eae36b40af38b4e2e70b9045649d2903',
            data['project']['dependencies'],
        )

    def test_module_shims_declare_the_shared_package(self):
        """Each module is a thin shim importing amplifier_fast_decisions; it must declare
        that package as a dependency, or a fresh install (e.g. after `amplifier update`
        rebuilds the tool environment) fails with 'No module named amplifier_fast_decisions'."""
        for name in ('loop-fast-decisions', 'hooks-fast-decisions', 'tool-fast-workspace', 'tool-jevgrep'):
            deps = tomllib.loads((ROOT/f'modules/{name}/pyproject.toml').read_text())['project']['dependencies']
            self.assertTrue(any(d.startswith('amplifier-fast-decisions @ ') for d in deps), name)

    def test_module_entrypoints(self):
        for name in ('loop-fast-decisions','hooks-fast-decisions','tool-fast-workspace','tool-jevgrep'):
            data=tomllib.loads((ROOT/'modules'/name/'pyproject.toml').read_text())
            self.assertIn(name,data['project']['entry-points']['amplifier.modules'])
    def test_root_package_assets(self):
        data=tomllib.loads((ROOT/'pyproject.toml').read_text())
        self.assertEqual(data['project']['requires-python'], '>=3.11')
        self.assertTrue((ROOT/'README.md').is_file())
        self.assertIn('static/*',data['tool']['setuptools']['package-data']['amplifier_fast_decisions'])
    def test_profile_is_valid_frontmatter_and_not_global(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'profile.md'
            args=SimpleNamespace(bundle_root=str(ROOT),workspace=temp,mode='shadow',
                allow_external_state=True,events=str(Path(temp)/'events'),timeout_ms=750,output=str(output))
            configure(args)
            data=json.loads(output.read_text().split('---')[1])
            self.assertEqual(data['bundle']['name'],'fast-decisions-shadow')
            self.assertTrue(data['includes'][-1]['bundle'].startswith('file:///'))
            self.assertEqual(data['tools'][0]['config']['root'],str(Path(temp).resolve(strict=True)))
    def test_active_requires_external_opt_in(self):
        with self.assertRaises(ValueError):
            configure(SimpleNamespace(bundle_root=str(ROOT),mode='active',allow_external_state=False))
    def test_dict_candidate_is_copied(self):
        data={'id':'x','label':'label','tool':'tool','arguments':{'nested':{'a':1}}}
        candidate=parse_candidate(data);data['arguments']['nested']['a']=2
        self.assertEqual(candidate.arguments['nested']['a'],1)
    def test_nonfinite_metadata_removed(self):
        self.assertIsNone(safe_data({'duration_ms':float('nan')})['duration_ms'])
    def test_bounded_candidate_count(self):
        with self.assertRaises(ValueError):Policy(max_candidates=100)
    def test_workspace_truncated_unicode(self):
        from amplifier_fast_decisions.workspace import WorkspaceTool
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp)/"unicode.txt").write_text("a"*1023+"é"*20)
            result=WorkspaceTool(temp,max_bytes=1024)._read({"operation":"read","path":"unicode.txt"})
            self.assertTrue(result["truncated"])
            self.assertEqual(result["text"],"a"*1023)
    def test_event_schema_names(self):
        schema=json.loads((ROOT/'schemas/event.schema.json').read_text())
        self.assertEqual(set(schema['properties']['event']['enum']),set(EVENT_NAMES))

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_service_capability_and_idempotent_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            coordinator=DemoCoordinator()
            runtime,owner=get_runtime(coordinator,{'backend':'unavailable','events_dir':temp})
            again,second_owner=get_runtime(coordinator,{})
            self.assertIs(again,runtime);self.assertTrue(owner);self.assertFalse(second_owner)
            self.assertIs(coordinator.get_capability('fast_decisions.service'),runtime.service)
            await runtime.close();await runtime.close()
            self.assertTrue(runtime.closed)
    async def test_invalid_envelope_does_not_claim_fast_submission(self):
        service,runtime,events,_=setup_service()
        def invalid(candidate,call_id):raise ValueError('schema drift')
        await RoutedProvider(DemoProvider(delay_ms=0),runtime,{'demo_inspect':DemoTool()},invalid).complete(request())
        self.assertEqual(service.turn.fast_total,0)
        self.assertFalse(any(e['event'].endswith('routed') and e['data'].get('route')=='fast' for e in events))
    async def test_deadline_shared_across_candidate_and_model(self):
        # This used a 25ms deadline raced against an ~18ms candidate-collection
        # sleep and a 20ms backend delay. On Python 3.11 CI runners the shared
        # deadline sometimes expires *during* candidate collection instead of
        # during the backend call: service.py's collection-phase `except
        # Exception` handler (unlike the backend-phase handler) never checks
        # `isinstance(exc, TimeoutError)` and unconditionally reports
        # reason_code="candidate_source_error" -- so the same 25ms deadline
        # expiring a few milliseconds earlier flips the observed reason code
        # and the assertion misses. Confirmed by forcing candidate collection
        # to overrun the deadline on its own (30ms sleep vs a 25ms deadline),
        # which reliably reproduces reason_code="candidate_source_error"
        # instead of "decision_timeout".
        #
        # Fix: stop racing real time. Use a generous, unambiguous margin (a
        # 200ms deadline, a 20ms candidate sleep, and a backend that never
        # returns on its own inside 200ms) so the timeout is guaranteed to
        # fire during the backend phase regardless of scheduler jitter. Then
        # prove budget *sharing* directly and deterministically: patch
        # asyncio.timeout_at to record the deadline passed to each phase and
        # assert both phases were given the exact same deadline -- i.e.
        # candidate collection does not reset the model call's budget.
        deadlines: list[float] = []
        real_timeout_at = asyncio.timeout_at

        def recording_timeout_at(when):
            deadlines.append(when)
            return real_timeout_at(when)

        service,_,events,coord=setup_service(policy=Policy(mode='active',timeout_ms=200,
            allowed_tools=('demo_inspect',),allow_synthetic_active=True))
        service.backend.delay_ms=2000  # 10x timeout_ms: never completes before the shared deadline
        async def slow_candidates(req):await asyncio.sleep(.02);return []  # small next to the 200ms budget
        coord.capabilities['fast_decisions.candidates']=slow_candidates
        with patch('amplifier_fast_decisions.service.asyncio.timeout_at',side_effect=recording_timeout_at):
            result=await service.choose(request(),{'demo_inspect':DemoTool()})
        self.assertIsNone(result)
        self.assertTrue(any(e['data'].get('reason_code')=='decision_timeout' for e in events))
        # Budget sharing: candidate collection and the backend call must be
        # timed against the identical deadline (not a fresh one per phase).
        self.assertGreaterEqual(len(deadlines),2)
        self.assertEqual(deadlines[0],deadlines[1])

    async def test_candidate_collection_timeout_reports_decision_timeout(self):
        # ISSUE A: phase 1 (candidate collection) used to catch the TimeoutError
        # raised by asyncio.timeout_at in a blanket `except Exception` and report
        # reason_code="candidate_source_error" instead of "decision_timeout".
        # A candidate source that never returns inside the deadline must still be
        # classified as a timeout, and must still fail closed to the slow path.
        service,_,events,coord=setup_service(policy=Policy(mode='active',timeout_ms=100,
            allowed_tools=('demo_inspect',),allow_synthetic_active=True))
        async def hanging_candidates(req):
            await asyncio.sleep(10)
            return []
        coord.capabilities['fast_decisions.candidates']=hanging_candidates
        result=await service.choose(request(),{'demo_inspect':DemoTool()})
        self.assertIsNone(result)
        self.assertEqual(events[-1]['data']['reason_code'],'decision_timeout')
        self.assertTrue(any(e['event'].endswith('fallback') and e['data'].get('reason_code')=='decision_timeout'
                             for e in events))

class MountOrderTests(unittest.IsolatedAsyncioTestCase):
    # Mount order across module types IS a documented, guaranteed kernel
    # contract: amplifier_core 1.6.1's initialize_session() (_session_init.py)
    # loads orchestrator (:74) before context (:104), providers (:154), tools
    # (:222) and hooks (:248) -- see @core:CONTRACTS.md Module Lifecycle. So
    # within a real session, loop-fast-decisions (the orchestrator) always
    # mounts before hooks-fast-decisions (the hook), and no race exists for
    # get_runtime()'s owner re-apply to resolve.
    #
    # This test is therefore defensive, not a race resolution: it covers
    # OUT-OF-SESSION construction paths where that ordering guarantee does not
    # apply -- unit tests, `afast demo`, or a future non-kernel host that
    # mounts modules in whatever order it likes. It mounts the hook FIRST
    # against the real production entrypoints (observer.mount,
    # orchestrator.mount) to prove the orchestrator's config still wins even
    # under that adversarial, out-of-session order.
    @unittest.skipUnless(HAS_CORE, "Actual amplifier_core is not installed")
    @unittest.skipUnless(HAS_LOOP, "Actual loop-streaming is not installed")
    async def test_owner_policy_defensive_reapply(self):
        from amplifier_fast_decisions import observer, orchestrator as orchestrator_module

        coord = DemoCoordinator()
        hook_cleanup = await observer.mount(coord, {})
        real_config = {"mode": "active", "timeout_ms": 5000,
                        "allowed_tools": ("demo_inspect",), "allow_synthetic_active": True}
        try:
            orchestrator_cleanup = await orchestrator_module.mount(coord, real_config)
        finally:
            pass
        try:
            runtime = coord.get_capability("fast_decisions.runtime")
            self.assertEqual(runtime.service.policy.mode, "active")
            self.assertEqual(runtime.service.policy.allowed_tools, ("demo_inspect",))
        finally:
            await orchestrator_cleanup()
            await hook_cleanup()


class InstalledUpstreamTests(unittest.TestCase):
    @unittest.skipUnless(HAS_CORE,'Actual amplifier_core is not installed')
    def test_actual_core_envelope(self):
        from amplifier_fast_decisions.orchestrator import action_response
        from amplifier_core.message_models import ChatResponse
        response=action_response(Candidate('read','Read','fast_workspace',{'operation':'read','path':'README.md'}),'call')
        self.assertIsInstance(response,ChatResponse)
        self.assertEqual(response.tool_calls[0].arguments['path'],'README.md')
        self.assertEqual(response.usage.total_tokens,0)
    @unittest.skipUnless(HAS_CORE,'Actual amplifier_core is not installed')
    def test_actual_hook_result(self):
        from amplifier_core.models import HookResult
        self.assertEqual(HookResult(action='continue').action,'continue')
    @unittest.skipUnless(HAS_LOOP,'Actual loop-streaming is not installed')
    def test_actual_loop_signature(self):
        from amplifier_module_loop_streaming import StreamingOrchestrator
        signature=inspect.signature(StreamingOrchestrator({}).execute)
        self.assertTrue({'prompt','context','providers','tools','hooks'}<=set(signature.parameters))
