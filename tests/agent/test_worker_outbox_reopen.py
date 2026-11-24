"""Lost worker replies reconcile by exact receipt without duplicating a write."""
import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


def test_reopen_reconciles_pending_receipt_before_new_write(tmp_path):
    calls = []
    def lost_reply(method, **params):
        calls.append(params)
        raise TimeoutError('lost reply')
    scope = {'session_id': 's', 'execution_id': 'w', 'epoch': 1}
    store = RuntimeSessionStore(lost_reply, scope, tmp_path)
    with pytest.raises(TimeoutError):
        store.queue_token_counts('s', input_tokens=10)
    store._outbox_owner.close()  # process exit left its durable pending journal
    restored_calls = []
    def owner(method, **params):
        restored_calls.append(params)
        return {'ok': True}
    reopened = RuntimeSessionStore(owner, scope, tmp_path)
    try:
        reopened.queue_token_counts('s', input_tokens=20)
        assert restored_calls[0] == calls[0]
        assert [row['sequence'] for row in restored_calls] == [1, 2]
        assert [row['payload']['input_tokens'] for row in restored_calls] == [10, 20]
    finally:
        reopened.close()


def test_lost_receipt_reply_replays_exact_write_before_new_write(tmp_path):
    calls = []
    receipts = {}

    def owner(method, **params):
        assert method == 'worker.persist'
        calls.append(params)
        key = (params['execution_id'], params['sequence'])
        signature = (params['operation'], params['payload'])
        if key in receipts:
            assert receipts[key][0] == signature
        else:
            receipts[key] = (signature, {'value': params['sequence']})
        if len(calls) == 1:
            raise TimeoutError('worker_receipt_timeout')
        return receipts[key][1]

    scope = {'session_id': 's', 'execution_id': 'w', 'epoch': 1}
    store = RuntimeSessionStore(owner, scope, tmp_path)
    try:
        assert store._apply('usage.main', {'input_tokens': 10}) == {'value': 1}
        assert store._apply('usage.main', {'input_tokens': 20}) == {'value': 2}
        assert calls[0] == calls[1]
        assert [row['sequence'] for row in calls] == [1, 1, 2]
        assert len(receipts) == 2
        assert store.journal['pending'] == []
        assert store.failure is None
    finally:
        store.close()


def test_repeated_receipt_timeouts_stay_bounded_and_sticky(tmp_path):
    calls = []

    def owner(method, **params):
        calls.append(params)
        raise TimeoutError('worker_receipt_timeout')

    scope = {'session_id': 's', 'execution_id': 'w', 'epoch': 1}
    store = RuntimeSessionStore(owner, scope, tmp_path)
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.queue_token_counts('s', input_tokens=10)
        assert len(calls) == 4
        assert all(row == calls[0] for row in calls)
        assert store.journal['pending'][0]['sequence'] == 1
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store.queue_token_counts('s', input_tokens=20)
        assert len(calls) == 4
    finally:
        store._outbox_owner.close()
