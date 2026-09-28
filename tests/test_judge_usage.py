"""Real-shaped usage receipts retain cost evidence without judged content."""
import asyncio
import unittest
from dataclasses import replace
from test_judged_decisions import FakeJudgeBackend, setup_service
from amplifier_fast_decisions.contracts import Policy, Question
from amplifier_fast_decisions.orchestrator import _ask_judge_choice, _ask_judges_many


class UsageTests(unittest.IsolatedAsyncioTestCase):
    def service(self, backend):
        return setup_service(policy=Policy(allow_external_state=True), backend=backend)

    async def ask(self, service):
        return await _ask_judge_choice(service, question_name='route', instructions='private',
                                       criteria={'cheap': 'private', 'strong': 'private'}, state={'secret': 'private'})

    async def test_real_usage_and_missing_answer_are_recorded(self):
        class Backend(FakeJudgeBackend):
            async def ask(self, request):
                return replace(await super().ask(request), model='jev-1.13.0',
                               input_tokens=123, output_tokens=4, synthetic=False)
        service, _, events = self.service(Backend([{}]))
        self.assertIsNone((await self.ask(service))[0])
        receipt = events[-1]
        self.assertEqual(receipt['event'], 'fast_decisions:judge_usage')
        self.assertEqual(receipt['data']['input_tokens'], 123)
        self.assertEqual(receipt['data']['model'], 'jev-1.13.0')
        self.assertNotIn('private', str(receipt))

    async def test_error_is_unknown_not_free(self):
        service, _, events = self.service(FakeJudgeBackend([None]))
        await self.ask(service)
        self.assertEqual(events[-1]['data']['status'], 'error')
        self.assertIsNone(events[-1]['data']['input_tokens'])

    async def test_policy_block_has_no_attempt(self):
        backend = FakeJudgeBackend([{}], external=True)
        service, _, events = setup_service(policy=Policy(allow_external_state=False), backend=backend)
        await self.ask(service)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(events, [])

    async def test_cancellation_propagates_with_unknown_usage(self):
        class Backend(FakeJudgeBackend):
            async def ask(self, request):
                raise asyncio.CancelledError()
        service, _, events = self.service(Backend([{}]))
        with self.assertRaises(asyncio.CancelledError):
            await self.ask(service)
        self.assertEqual(events[-1]['data']['status'], 'cancelled')
        self.assertIsNone(events[-1]['data']['input_tokens'])

    async def test_batch_records_one_attempt(self):
        service, _, events = self.service(FakeJudgeBackend([{}]))
        questions = [Question(name=n, type='choice', instructions='private', criteria={'yes':'private', 'no':'private'})
                     for n in ('phase', 'escalation')]
        await _ask_judges_many(service, questions=questions, state={})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['data']['question_ids'], ['phase', 'escalation'])
