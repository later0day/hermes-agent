"""A managed turn whose worker acknowledged its durable finish keeps its answer even when the
worker's interpreter is slow to exit or exits non-zero afterwards (interpreter teardown,
a lingering non-daemon thread, a Windows launcher): that is close()'s cleanup, not a lost
execution. Real child process, real SQLite authority."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import threading
from types import MethodType, SimpleNamespace

import pytest

from gateway.session_contract import SessionActor, SessionRef

CHILD = """import json, os, signal, sys, time, psutil
from hermes_cli.process_identity import worker_birth
if sys.argv[1] == 'lingering':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
me = psutil.Process()
out = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
def send(frame):
    out.write((json.dumps(frame) + '\\n').encode())
send({'type': 'hello', 'pid': me.pid, 'birth': worker_birth(me), 'ancestors': [p.pid for p in me.parents()[:3]]})
for line in sys.stdin.buffer:
    frame = json.loads(line)
    if 'version' in frame:
        send({'type': 'result', 'result': {'final_response': 'FINISHED_ANSWER', 'model': 'm', 'failure_reason': 'billing',
              'billing_block': {'provider': 'custom', 'provider_label': 'Custom', 'model': 'm', 'billing_url': None,
                                'is_nous': False, 'message': 'Add credits'}}})
    elif frame == {'type': 'finish'}:
        send({'type': 'finished'})
        break
if sys.argv[1] == 'nonzero':
    os._exit(3)
time.sleep(60)  # interpreter teardown that outlives the owner's exit wait
"""


@pytest.mark.platforms("linux")
@pytest.mark.parametrize(('exit_shape', 'rotate'), [('nonzero', False), ('lingering', False), ('nonzero', True)])
def test_finished_worker_keeps_its_result_on_slow_or_nonzero_exit(tmp_path, monkeypatch, exit_shape, rotate):
    from gateway import session_managed_worker as managed
    from gateway.run import GatewayRunner
    from gateway.session_authority import LiveSession, SessionAuthority, admission_row
    from gateway.session_contract import Submission
    from gateway.session_policy import build_policy
    from persistence.conversation.usage import BillingRoute, TokenCounts, UsageDelta
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_holder import raw_holder
    from tests.fixtures.storage_seed import seed_session

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = open_persistence(profile_scope(home=tmp_path))
    seed_session(db, 's', 'test')
    # The worker's per-call usage writes, already committed by the time it reports 'finished'.
    db.usage.queue_usage('s', UsageDelta(tokens=TokenCounts(input_tokens=200, output_tokens=50, cache_read_tokens=800),
                                         api_call_count=3, route=BillingRoute(model='m')))
    db.usage.flush_usage()
    runner = SimpleNamespace(_draining=False, _cached_agent_for=lambda route: None,
                             _adapter_for_source=lambda source: None)
    runner._run_in_executor_with_context = MethodType(GatewayRunner._run_in_executor_with_context, runner)
    runner._get_executor = MethodType(GatewayRunner._get_executor, runner)
    authority = SessionAuthority(runner, profile_id='owned', instance_id='i',
                                 epoch=db.runtime.begin_epoch(instance_id='i'),
                                 persistence=open_persistence(profile_scope(home=tmp_path)))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='human', chat_id='c'), 'route')
    actor = SessionActor('human', 'owned', frozenset({'session:submit', 'session:control'}), 'cli')
    ref = SessionRef('owned', 's')
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    spawned = []
    lifecycle = []

    class Child(subprocess.Popen):
        def __init__(self, args, **kw):
            super().__init__([sys.executable, '-c', CHILD, exit_shape], **kw)
            spawned.append(self)
            lifecycle.append(('spawn', self))
    monkeypatch.setattr(managed.subprocess, 'Popen', Child)
    close_started = threading.Event()
    release_close = threading.Event()
    real_close = managed.ManagedWorker.close

    def gated_close(worker):
        close_started.set()
        if exit_shape == 'nonzero':
            assert release_close.wait(30)
        real_close(worker)
        lifecycle.append(('close', worker.process))

    monkeypatch.setattr(managed.ManagedWorker, 'close', gated_close)
    if rotate:
        reserve = managed.reserve_admission_worker

        def reserve_then_compress(authority, **kwargs):
            # A worker compression mid-turn: the transcript continues in a child session and the owner's
            # execution row follows it in the same transaction (the worker compression publish).
            scope = reserve(authority, **kwargs)
            db.sessions.end('s', 'compression')
            seed_session(db, 's-c1', 'test', parent_session_id='s')
            raw_holder(db)._execute_write(lambda conn: conn.execute(
                'UPDATE worker_executions SET session_id=? WHERE execution_id=?', ('s-c1', scope['execution_id'])))
            db.usage.queue_usage('s-c1', UsageDelta(tokens=TokenCounts(input_tokens=100, output_tokens=10),
                                                    api_call_count=1, route=BillingRoute(model='m')))
            db.usage.flush_usage()
            return scope
        monkeypatch.setattr(managed, 'reserve_admission_worker', reserve_then_compress)
    policy = build_policy({'cwd': str(tmp_path), 'model': 'm'}, {}, profile_terminal=False)
    release_default = threading.Event()
    result_accepted = threading.Event()
    default_occupied = []
    real_accept_result = managed.accept_result

    def accept_with_busy_default_executor(result):
        result_accepted.set()
        loop = asyncio.get_running_loop()
        default_occupied.append(loop.run_in_executor(None, release_default.wait))
        return real_accept_result(result)

    monkeypatch.setattr(managed, 'accept_result', accept_with_busy_default_executor)

    async def scenario():
        from tests.waiting import async_wait_until

        pool = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        original_executor = loop._default_executor
        loop.set_default_executor(pool)
        execution = None
        try:
            await authority.submit(actor, Submission('turn', ref, {'text': 'go'}, 'queue'))
            row = admission_row(db.admissions.claim(epoch=authority.epoch, session_id='s'))
            authority._stamp_claim(ref, authority.sessions['s'], row)  # the drain's claim stamp
            async with asyncio.timeout(40):
                from gateway.session_runtime_workers import mutation_tasks
                execution = asyncio.create_task(managed.execute_managed(authority, ref, row, policy))
                try:
                    await async_wait_until(result_accepted.is_set, timeout=30, what='the managed worker result to be accepted')
                    await async_wait_until(close_started.is_set, timeout=2,
                                           what='the durable worker cleanup to start without waiting for interpreter teardown')
                    response = await execution
                    if exit_shape == 'lingering':
                        await asyncio.wait_for(asyncio.gather(*mutation_tasks(authority)), timeout=2)
                        assert spawned[0].poll() is not None, 'durable cleanup honored the lingering interpreter SIGTERM grace'
                    if exit_shape == 'nonzero' and not rotate:
                        second_worker = None
                        second_spawn = asyncio.create_task(managed._spawn_registered_worker(
                            authority, ref, row | {'generation': row['generation'] + 1}))
                        try:
                            try:
                                await async_wait_until(lambda: len(spawned) == 2, timeout=2,
                                                       what='the next generation to spawn before the prior close')
                            except AssertionError:
                                pass
                            finally:
                                release_close.set()
                            second_worker, _ = await second_spawn
                        finally:
                            if second_worker is not None:
                                await managed._release_managed_worker(
                                    authority, second_worker, second_worker.process,
                                    session_id=ref.session_id, durable_finished=False)
                            else:
                                second_spawn.cancel()
                                await asyncio.gather(second_spawn, return_exceptions=True)
                        assert lifecycle.index(('close', spawned[0])) < lifecycle.index(('spawn', spawned[1])), \
                            'the next interpreter started before the prior durable cleanup reaped its process'
                finally:
                    release_close.set()
                    release_default.set()
                    if not execution.done():
                        execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                    await asyncio.gather(*mutation_tasks(authority))
                    GatewayRunner._shutdown_executor(runner, drain_timeout=2)
                return row, response
        finally:
            release_close.set()
            release_default.set()
            if default_occupied:
                await asyncio.gather(*default_occupied)
            loop._default_executor = original_executor
            pool.shutdown(wait=True)
    with db, authority.persistence:
        row, response = asyncio.run(scenario())
        status = db.admissions.get(admission_id=row['admission_id']).status
    assert response == 'FINISHED_ANSWER'
    captured = authority.pending_results[row['admission_id']]
    assert captured['result']['final_response'] == 'FINISHED_ANSWER'
    assert status == 'started', 'the finished turn was marked unknown instead of reaching settlement'
    assert spawned[0].poll() is not None, "close() did not reap the worker"
    # A local viewer's message.complete gets the billing banner and turn-end usage, as in process.
    from gateway.session_settlement_recovery import completion_payload
    complete = completion_payload(row, {'outcome': 'completed'}, response, captured)
    assert complete['billing']['message'] == 'Add credits' and complete['failure_reason'] == 'billing'
    if rotate:
        assert complete['usage'] == {'model': 'm', 'input': 300, 'output': 60, 'reasoning': 0, 'prompt': 1100,
                                     'completion': 60, 'total': 1160, 'calls': 4, 'cache_hit_pct': 73}
    else:
        assert complete['usage'] == {'model': 'm', 'input': 200, 'output': 50, 'reasoning': 0, 'prompt': 1000,
                                     'completion': 50, 'total': 1050, 'calls': 3, 'cache_hit_pct': 80}


def test_finished_cleanup_does_not_block_default_executor():
    from concurrent.futures import ThreadPoolExecutor

    from gateway import session_managed_worker as managed
    from gateway.run import GatewayRunner
    from gateway.session_runtime_workers import mutation_tasks
    from tests.waiting import async_wait_until

    close_started = threading.Event()
    release_close = threading.Event()
    probe_ran = threading.Event()

    class Worker:
        def close(self):
            close_started.set()
            release_close.wait(5)

    runner = SimpleNamespace(_persist_active_agents=lambda: None)
    runner._run_in_executor_with_context = MethodType(GatewayRunner._run_in_executor_with_context, runner)
    runner._get_executor = MethodType(GatewayRunner._get_executor, runner)
    authority = SimpleNamespace(runner=runner)
    process = SimpleNamespace(wait=lambda _timeout: None, poll=lambda: 0)

    async def scenario():
        await managed._release_managed_worker(authority, Worker(), process, session_id='s', durable_finished=True)
        await async_wait_until(close_started.is_set, timeout=2, what='finished-worker cleanup to start')
        probe = asyncio.create_task(asyncio.to_thread(probe_ran.set))
        completed = True
        try:
            await asyncio.wait_for(probe, timeout=2)
        except TimeoutError:
            completed = False
        finally:
            release_close.set()
            await asyncio.gather(*mutation_tasks(authority))
            if not probe.done():
                await probe
            GatewayRunner._shutdown_executor(runner, drain_timeout=2)
        assert completed, 'finished-worker cleanup occupied the default executor'
        assert probe_ran.is_set()

    loop = asyncio.new_event_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    try:
        loop.run_until_complete(scenario())
    finally:
        release_close.set()
        loop.close()
        pool.shutdown(wait=True)
