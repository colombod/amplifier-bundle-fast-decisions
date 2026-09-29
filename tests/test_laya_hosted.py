import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from amplifier_fast_decisions.laya_hosted import create_app, validate

READY = importlib.util.find_spec('fastapi') and importlib.util.find_spec('httpx')
PAYLOAD = {'state': 'public fixture', 'questions': {'relevant': {'type': 'noul', 'instructions': 'Relevant?'}}}


class Agent:
    device = 'cuda:0'
    def __init__(self):
        self.calls = 0
    def predict(self, state, questions):
        self.calls += 1
        return {'model': 'laya-test', 'answers': {'relevant': {'type': 'noul', 'noul': .9}}}


@unittest.skipUnless(READY, 'requires serve and local extras')
class HostedTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def service(self, agent=None, **kwargs):
        import httpx
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clients.json'
            path.write_text(json.dumps({name: hashlib.sha256(name.encode()).hexdigest() for name in ['alice', 'bob']}))
            model = agent or Agent()
            app = create_app(clients_file=path, _agent=model, **kwargs)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    yield client, path, model

    async def test_auth_required_before_prediction_and_keys_revoke(self):
        async with self.service() as (client, path, agent):
            for headers in [{}, {'Authorization': 'Bearer wrong'}]:
                self.assertEqual((await client.post('/v1/decide', json=PAYLOAD, headers=headers)).status_code, 401)
            self.assertEqual(agent.calls, 0)
            response = await client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':'Bearer alice'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['model'], 'laya-test')
            self.assertIn('checkpoint_revision', response.json())
            path.write_text(json.dumps({'bob': hashlib.sha256(b'bob').hexdigest()}))
            self.assertEqual((await client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':'Bearer alice'})).status_code, 401)
            self.assertEqual((await client.post('/v1/systemone', json=PAYLOAD, headers={'Authorization':'Bearer bob'})).status_code, 200)

    async def test_client_rate_limits_are_independent(self):
        async with self.service(requests_per_minute=1) as (client, _, agent):
            for name, expected in [('alice',200),('alice',429),('bob',200)]:
                response = await client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':f'Bearer {name}'})
                self.assertEqual(response.status_code, expected)
            self.assertEqual(agent.calls, 2)

    async def test_bad_or_large_inputs_never_reach_model(self):
        async with self.service() as (client, _, agent):
            for content, status in [('[]',400),('x'*65537,413),('{',400),
                                    (json.dumps({**PAYLOAD,'state':'x'*3001}),400),
                                    (json.dumps({**PAYLOAD,'max_len':999999}),400)]:
                response = await client.post('/v1/decide', content=content, headers={'Authorization':'Bearer alice'})
                self.assertEqual(response.status_code, status)
            self.assertEqual(agent.calls, 0)

    async def test_timeout_keeps_running_slot_and_busy_fails_fast(self):
        started, release = threading.Event(), threading.Event()
        class Blocking(Agent):
            def predict(self, *args):
                started.set()
                release.wait(2)
                return super().predict(*args)
        try:
            async with self.service(Blocking(), max_inflight=1, deadline_s=.03) as (client, _, agent):
                response = await client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':'Bearer alice'})
                self.assertTrue(started.is_set())
                self.assertEqual(response.status_code, 504)
                self.assertEqual((await client.get('/health')).json()['inflight'], 1)
                response = await client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':'Bearer bob'})
                self.assertEqual(response.status_code, 503)
                release.set()
                for _ in range(30):
                    if (await client.get('/health')).json()['inflight'] == 0:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual((await client.get('/health')).json()['inflight'], 0)
        finally:
            release.set()

    async def test_errors_do_not_echo_source_or_exception(self):
        class Broken(Agent):
            def predict(self, *args):
                raise RuntimeError('PRIVATE_PAYLOAD_OR_KEY')
        async with self.service(Broken()) as (client, _, agent):
            response = await client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':'Bearer alice'})
            self.assertEqual(response.status_code, 500)
            self.assertNotIn('PRIVATE', response.text)

    async def test_corrupted_key_file_fails_closed(self):
        async with self.service() as (client, path, agent):
            path.write_text('{}')
            response = await client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':'Bearer alice'})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(agent.calls, 0)

    async def test_cuda_fallback_is_not_reported_as_success(self):
        class Fallback(Agent):
            cpu_fallback_count = 0
            def predict(self, *args):
                self.cpu_fallback_count += 1
                return super().predict(*args)
        async with self.service(Fallback()) as (client, _, agent):
            response = await client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':'Bearer alice'})
            self.assertEqual(response.status_code, 500)

    async def test_cancelled_caller_keeps_slot_and_late_exception_is_consumed(self):
        started, release = threading.Event(), threading.Event()
        class Blocking(Agent):
            def predict(self, *args):
                started.set()
                release.wait(2)
                raise RuntimeError('PRIVATE_LATE_EXCEPTION')
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        unhandled = []
        loop.set_exception_handler(lambda _, context: unhandled.append(context))
        try:
            async with self.service(Blocking(), max_inflight=1) as (client, _, agent):
                task = asyncio.create_task(client.post('/v1/decide', json=PAYLOAD, headers={'Authorization':'Bearer alice'}))
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(started.is_set())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual((await client.get('/health')).json()['inflight'], 1)
                release.set()
                for _ in range(100):
                    if (await client.get('/health')).json()['inflight'] == 0:
                        break
                    await asyncio.sleep(.01)
                await asyncio.sleep(.02)
                self.assertEqual((await client.get('/health')).json()['inflight'], 0)
                self.assertEqual(unhandled, [])
        finally:
            release.set()
            loop.set_exception_handler(previous)

    def test_no_anonymous_startup_or_mutable_revision(self):
        with self.assertRaises(FileNotFoundError):
            create_app(clients_file='/nonexistent/no-keys')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clients.json'
            path.write_text(json.dumps({'alice': hashlib.sha256(b'alice').hexdigest()}))
            with self.assertRaises(ValueError):
                create_app(clients_file=path, revision='main')


class BoundsTests(unittest.TestCase):
    def test_question_bounds(self):
        for questions in [{}, {'q': {'type':'choice','instructions':'Pick','criteria':{'one':'Only'}}},
                          {'q':{'type':'noul','instructions':'x'*513}},
                          {'q':{'type':'noul','instructions':'Pick','extra':'bad'}}]:
            with self.assertRaises(ValueError):
                validate({'state':'fixture','questions':questions})
